"""Apply evidence retention in bounded transactions.

- ``partner_exchanges``: bodies nulled when strictly older than 30 days;
  rows deleted when strictly older than 180 days.
- ``obs_spans``: deleted when strictly older than 30 days.
"""
import argparse
import asyncio
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

BODY_DAYS, ROW_DAYS, SPAN_DAYS = 30, 180, 30

STRIP_BODIES = text("""WITH expired AS (
 SELECT id FROM partner_exchanges WHERE started_at < :cutoff
  AND (request_body_raw IS NOT NULL OR response_body_raw IS NOT NULL
       OR request_body_json IS NOT NULL OR response_body_json IS NOT NULL)
 ORDER BY started_at LIMIT :batch FOR UPDATE SKIP LOCKED
) UPDATE partner_exchanges SET request_body_raw = NULL, response_body_raw = NULL,
  request_body_json = NULL, response_body_json = NULL
 WHERE id IN (SELECT id FROM expired) RETURNING id""")
DELETE_ROWS = text("""WITH expired AS (
 SELECT id FROM partner_exchanges WHERE started_at < :cutoff
 ORDER BY started_at LIMIT :batch FOR UPDATE SKIP LOCKED
) DELETE FROM partner_exchanges WHERE id IN (SELECT id FROM expired) RETURNING id""")
DELETE_SPANS = text("""WITH expired AS (
 SELECT id FROM obs_spans WHERE start_time < :cutoff
 ORDER BY start_time LIMIT :batch FOR UPDATE SKIP LOCKED
) DELETE FROM obs_spans WHERE id IN (SELECT id FROM expired) RETURNING id""")


async def _drain(engine, statement, cutoff, batch, max_batches):
    total = 0
    for _ in range(max_batches):
        async with engine.begin() as conn:
            count = len((await conn.execute(statement, {"cutoff": cutoff, "batch": batch})).all())
        total += count
        if count < batch:
            break
    return total


async def prune(url, batch=500, max_batches=1000, now=None):
    """``{"bodies_stripped", "rows_deleted", "spans_deleted"}``."""
    now = now or datetime.now(UTC)
    engine = create_async_engine(url)
    try:
        return {
            "rows_deleted": await _drain(engine, DELETE_ROWS, now - timedelta(days=ROW_DAYS), batch, max_batches),
            "bodies_stripped": await _drain(engine, STRIP_BODIES, now - timedelta(days=BODY_DAYS), batch, max_batches),
            "spans_deleted": await _drain(engine, DELETE_SPANS, now - timedelta(days=SPAN_DAYS), batch, max_batches),
        }
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
    result = asyncio.run(prune(os.environ["DATABASE_URL"], args.batch_size, args.max_batches))
    print(" ".join(f"{k}={v}" for k, v in result.items()))
