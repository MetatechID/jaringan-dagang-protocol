"""Idempotent migration: partner API + payment callback for external merchants.

Creates the ``partner_orders`` table and adds the per-Brand partner
credential columns (``partner_api_key``, ``partner_callback_url``,
``partner_callback_secret``) — same per-Brand pattern as the ``oy_*`` /
``sento_*`` blocks.

Companion to ``models/partner_order.py`` +
``routers/partner.py``.

Usage
-----

    # Dry run (prints SQL):
    python apps/beli-aman-bap/scripts/add-partner-integration.py

    # Apply against live DB:
    DATABASE_URL=postgresql+asyncpg://... \\
        python apps/beli-aman-bap/scripts/add-partner-integration.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys


DDL_STATEMENTS: list[str] = [
    # --- partner_orders: transactions registered by external merchants ---
    """CREATE TABLE IF NOT EXISTS partner_orders (
        id VARCHAR(36) PRIMARY KEY,
        brand_id VARCHAR(36) NOT NULL REFERENCES brands(id),
        external_order_id VARCHAR(255) NOT NULL,
        amount_idr INTEGER NOT NULL,
        description TEXT NOT NULL,
        buyer JSONB,
        items JSONB,
        status VARCHAR(16) NOT NULL DEFAULT 'pending',
        invoice_id VARCHAR(255),
        invoice_provider VARCHAR(16),
        payment_url TEXT,
        success_url TEXT,
        paid_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );""",
    "CREATE INDEX IF NOT EXISTS ix_partner_orders_brand_id ON partner_orders (brand_id);",
    "CREATE INDEX IF NOT EXISTS ix_partner_orders_external_order_id ON partner_orders (external_order_id);",
    "CREATE INDEX IF NOT EXISTS ix_partner_orders_status ON partner_orders (status);",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_partner_orders_brand_external "
    "ON partner_orders (brand_id, external_order_id);",
    "CREATE INDEX IF NOT EXISTS ix_partner_orders_invoice_id ON partner_orders (invoice_id);",
    # --- brands: partner API credentials (mirrors the oy_*/sento_* blocks) ---
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS partner_api_key VARCHAR(255);",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS partner_callback_url TEXT;",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS partner_callback_secret VARCHAR(255);",
]


def print_dry_run_sql() -> None:
    print("-- add-partner-integration.py (dry-run)")
    print("BEGIN;")
    for stmt in DDL_STATEMENTS:
        s = stmt.strip()
        if s:
            print(s)
    print("COMMIT;")


async def apply_migration(database_url: str) -> int:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url)
    count = 0
    async with engine.begin() as conn:
        for stmt in DDL_STATEMENTS:
            s = stmt.strip()
            if not s:
                continue
            await conn.execute(text(s))
            count += 1
    await engine.dispose()
    return count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Idempotent partner-integration migration. Default is dry-run."
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if not args.apply:
        print_dry_run_sql()
        return 0

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: --apply requires DATABASE_URL.", file=sys.stderr)
        return 2

    import asyncio

    print(f"Applying DDL against {db_url[:40]}...")
    count = asyncio.run(apply_migration(db_url))
    print(f"done. statements executed: {count}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
