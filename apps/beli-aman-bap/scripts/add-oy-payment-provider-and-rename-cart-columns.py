"""Idempotent migration: add OY payment_provider + oy_* columns to Brand,
rename bot_carts.xendit_invoice_id → invoice_id + add invoice_provider.

Usage
-----

    # Dry run (prints SQL):
    python apps/beli-aman-bap/scripts/add-oy-payment-provider-and-rename-cart-columns.py

    # Apply against live DB:
    DATABASE_URL=postgresql+asyncpg://... \\
        python apps/beli-aman-bap/scripts/add-oy-payment-provider-and-rename-cart-columns.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys


DDL_STATEMENTS: list[str] = [
    # --- brands: payment_provider gate + OY credentials ---
    # payment_provider defaults to "xendit" so legacy rows keep working.
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS payment_provider VARCHAR(16) NOT NULL DEFAULT 'xendit';",
    "CREATE INDEX IF NOT EXISTS ix_brands_payment_provider ON brands (payment_provider);",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS oy_api_key VARCHAR(255);",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS oy_username VARCHAR(128);",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS oy_callback_secret VARCHAR(128);",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS oy_store_id VARCHAR(64);",

    # --- bot_carts: rename xendit_invoice_id → invoice_id ---
    # Old xendit_invoice_id (created by add-xendit-biteship-columns.py) and
    # new invoice_id share semantics; the rename keeps the existing data.
    """
    DO $$
    BEGIN
      IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='bot_carts' AND column_name='xendit_invoice_id'
      ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='bot_carts' AND column_name='invoice_id'
      ) THEN
        ALTER TABLE bot_carts RENAME COLUMN xendit_invoice_id TO invoice_id;
      END IF;
    END $$;
    """,
    # If the new column doesn't exist yet (fresh dev DB with the model
    # already declaring it), add it instead.
    "ALTER TABLE bot_carts ADD COLUMN IF NOT EXISTS invoice_id VARCHAR(64);",
    "ALTER TABLE bot_carts ADD COLUMN IF NOT EXISTS invoice_provider VARCHAR(16);",
    # Backfill invoice_provider for legacy Xendit rows so the OY webhook
    # router can skip rows it shouldn't touch. Best-effort — real prod
    # backfill inspect-by-hand is also acceptable per the plan.
    "UPDATE bot_carts SET invoice_provider = 'xendit' "
    "WHERE invoice_provider IS NULL AND invoice_id IS NOT NULL;",
    # Composite lookup index: webhook receivers look up by (provider, id)
    # when the path doesn't carry the external_id prefix.
    "CREATE INDEX IF NOT EXISTS ix_bot_carts_invoice_provider_id "
    "ON bot_carts (invoice_provider, invoice_id);",
]


def print_dry_run_sql() -> None:
    print("-- add-oy-payment-provider-and-rename-cart-columns.py (dry-run)")
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
        description="Idempotent OY + cart rename migration. Default is dry-run."
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
