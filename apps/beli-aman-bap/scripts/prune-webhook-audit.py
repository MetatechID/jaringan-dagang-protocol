"""Delete audit evidence strictly older than 30 days, in bounded transactions."""
import argparse
import asyncio
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DELETE_BATCH = text("""WITH expired AS (
 SELECT id FROM webhook_audits WHERE started_at < :cutoff
 ORDER BY started_at LIMIT :batch FOR UPDATE SKIP LOCKED
) DELETE FROM webhook_audits WHERE id IN (SELECT id FROM expired) RETURNING id""")


async def prune(url, batch=500, max_batches=1000):
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    engine = create_async_engine(url)
    total = 0
    try:
        for _ in range(max_batches):
            async with engine.begin() as conn:
                count = len((await conn.execute(DELETE_BATCH, {"cutoff": cutoff, "batch": batch})).all())
            total += count
            if count < batch:
                break
        return total
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--max-batches", type=int, default=1000)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 5000 or not 1 <= args.max_batches <= 10000:
        parser.error("invalid batch bounds")
    if not os.getenv("DATABASE_URL"):
        parser.error("DATABASE_URL required")
    print(f"Deleted {asyncio.run(prune(os.environ['DATABASE_URL'], args.batch_size, args.max_batches))} audit rows")
