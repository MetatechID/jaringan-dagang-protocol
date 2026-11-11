"""Partner exchange log + BAP spans migration. Idempotent.

Renames ``webhook_audits`` to ``partner_exchanges`` (keeping its rows), adds
the correlation columns, and creates ``obs_spans``. Dry-run by default;
``--apply`` needs DATABASE_URL.
"""
import argparse
import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

OLD_INDEX_FIELDS = ("started_at", "delivery_id", "brand_id", "partner_order_id", "external_order_id", "dipay_reference")

RENAME = [
    """DO $$ BEGIN
 IF to_regclass('public.webhook_audits') IS NOT NULL AND to_regclass('public.partner_exchanges') IS NULL THEN
  ALTER TABLE webhook_audits RENAME TO partner_exchanges;
  ALTER INDEX IF EXISTS webhook_audits_pkey RENAME TO partner_exchanges_pkey;
 END IF;
END $$;""",
    *[f"ALTER INDEX IF EXISTS ix_webhook_audits_{f} RENAME TO ix_partner_exchanges_{f};" for f in OLD_INDEX_FIELDS],
]
DDL = """CREATE TABLE IF NOT EXISTS partner_exchanges (
 id VARCHAR(36) PRIMARY KEY,
 started_at TIMESTAMPTZ NOT NULL, completed_at TIMESTAMPTZ, duration_ms INTEGER,
 direction VARCHAR(16) NOT NULL, integration VARCHAR(32) NOT NULL,
 operation VARCHAR(64) NOT NULL, method VARCHAR(16) NOT NULL,
 endpoint VARCHAR(2048) NOT NULL, status INTEGER, outcome VARCHAR(32) NOT NULL,
 error VARCHAR(64), attempt INTEGER, delivery_id VARCHAR(36),
 brand_id VARCHAR(36), partner_order_id VARCHAR(36), external_order_id VARCHAR(255),
 dipay_reference VARCHAR(255), request JSONB NOT NULL, response JSONB NOT NULL
);"""
NEW_COLUMNS = {
    "request_body_raw": "BYTEA", "response_body_raw": "BYTEA",
    "request_body_json": "JSONB", "response_body_json": "JSONB",
    "error_class": "VARCHAR(32)", "partner_code": "VARCHAR(64)",
    "trace_id": "VARCHAR(32)", "span_id": "VARCHAR(16)", "job_id": "VARCHAR(36)",
    "order_id": "VARCHAR(36)", "cart_id": "VARCHAR(36)", "provider_ref": "VARCHAR(255)",
    "redacted": "BOOLEAN NOT NULL DEFAULT false",
}
COLUMNS = [f"ALTER TABLE partner_exchanges ADD COLUMN IF NOT EXISTS {name} {sql};"
           for name, sql in NEW_COLUMNS.items()]
INDEXES = [f"CREATE INDEX IF NOT EXISTS ix_partner_exchanges_{field} ON partner_exchanges ({field});"
           for field in (*OLD_INDEX_FIELDS, "error_class", "trace_id", "job_id", "order_id", "cart_id", "provider_ref")]
SPANS = [
    """CREATE TABLE IF NOT EXISTS obs_spans (
 id VARCHAR(36) PRIMARY KEY,
 trace_id VARCHAR(32) NOT NULL, span_id VARCHAR(16) NOT NULL, parent_span_id VARCHAR(16),
 name VARCHAR(255) NOT NULL, kind VARCHAR(16) NOT NULL, service VARCHAR(32) NOT NULL,
 start_time TIMESTAMPTZ NOT NULL, end_time TIMESTAMPTZ, duration_ms INTEGER,
 status VARCHAR(16) NOT NULL, error TEXT,
 order_id VARCHAR(36), cart_id VARCHAR(36), partner_order_id VARCHAR(36), brand_id VARCHAR(36),
 provider_ref VARCHAR(255), job_id VARCHAR(36), attributes JSONB NOT NULL
);""",
    *[f"CREATE INDEX IF NOT EXISTS ix_obs_spans_{field} ON obs_spans ({field});"
      for field in ("trace_id", "start_time", "order_id", "partner_order_id", "brand_id")],
]
STATEMENTS = [*RENAME, DDL, *COLUMNS, *INDEXES, *SPANS]


async def apply_migration(url):
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            for statement in STATEMENTS:
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
        print("partner_exchanges + obs_spans schema applied.")
    else:
        print("BEGIN;\n" + "\n".join(STATEMENTS) + "\nCOMMIT;")
