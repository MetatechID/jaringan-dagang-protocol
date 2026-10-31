"""Explicit idempotent audit migration. Dry-run by default; --apply needs DATABASE_URL."""
import argparse
import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DDL = """CREATE TABLE IF NOT EXISTS webhook_audits (
 id VARCHAR(36) PRIMARY KEY,
 started_at TIMESTAMPTZ NOT NULL, completed_at TIMESTAMPTZ, duration_ms INTEGER,
 direction VARCHAR(16) NOT NULL, integration VARCHAR(32) NOT NULL,
 operation VARCHAR(64) NOT NULL, method VARCHAR(16) NOT NULL,
 endpoint VARCHAR(2048) NOT NULL, status INTEGER, outcome VARCHAR(32) NOT NULL,
 error VARCHAR(64), attempt INTEGER, delivery_id VARCHAR(36),
 brand_id VARCHAR(36), partner_order_id VARCHAR(36), external_order_id VARCHAR(255),
 dipay_reference VARCHAR(255), request JSONB NOT NULL, response JSONB NOT NULL
);"""
RAW_COLUMNS = [
    "ALTER TABLE webhook_audits ADD COLUMN IF NOT EXISTS request_body_raw BYTEA;",
    "ALTER TABLE webhook_audits ADD COLUMN IF NOT EXISTS response_body_raw BYTEA;",
    "ALTER TABLE webhook_audits ADD COLUMN IF NOT EXISTS request_body_json JSONB;",
    "ALTER TABLE webhook_audits ADD COLUMN IF NOT EXISTS response_body_json JSONB;",
]
INDEXES = [f"CREATE INDEX IF NOT EXISTS ix_webhook_audits_{field} ON webhook_audits ({field});"
           for field in ("started_at", "delivery_id", "brand_id", "partner_order_id", "external_order_id", "dipay_reference")]


async def apply_migration(url):
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            for statement in [DDL, *RAW_COLUMNS, *INDEXES]:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply:
        if not os.getenv("DATABASE_URL"):
            parser.error("--apply requires DATABASE_URL")
        asyncio.run(apply_migration(os.environ["DATABASE_URL"]))
        print("Audit schema applied.")
    else:
        print("BEGIN;\n" + "\n".join([DDL, *RAW_COLUMNS, *INDEXES]) + "\nCOMMIT;")
