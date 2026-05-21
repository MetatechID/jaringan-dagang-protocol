"""Idempotently add Xendit + Biteship columns to brands / orders / escrow_ledger
/ bot_carts.

Usage
-----

    # Dry run (safe, prints SQL):
    python apps/beli-aman-bap/scripts/add-xendit-biteship-columns.py

    # Apply against live DB:
    DATABASE_URL=postgresql+asyncpg://... \\
        python apps/beli-aman-bap/scripts/add-xendit-biteship-columns.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys


DDL_STATEMENTS: list[str] = [
    # --- brands: Xendit sub-account routing + Biteship origin ---
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS xendit_sub_account_id VARCHAR(64);",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS xendit_disbursement_bank_code VARCHAR(32);",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS xendit_disbursement_bank_account VARCHAR(64);",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS xendit_disbursement_holder_name VARCHAR(255);",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS biteship_origin_address JSONB;",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS biteship_default_courier VARCHAR(64);",

    # --- orders: fulfillment-timeline rename + Biteship id ---
    # Rename simulated → real if the old columns exist; idempotent.
    """
    DO $$
    BEGIN
      IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='orders' AND column_name='shipped_simulated_at'
      ) THEN
        ALTER TABLE orders RENAME COLUMN shipped_simulated_at TO shipped_at;
      END IF;
      IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='orders' AND column_name='delivered_simulated_at'
      ) THEN
        ALTER TABLE orders RENAME COLUMN delivered_simulated_at TO delivered_at;
      END IF;
    END $$;
    """,
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipped_at TIMESTAMP WITH TIME ZONE;",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMP WITH TIME ZONE;",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS biteship_order_id VARCHAR(64);",
    "CREATE INDEX IF NOT EXISTS ix_orders_biteship_order_id ON orders (biteship_order_id);",

    # --- orders: fulfillment columns synced from seller BPP / Biteship webhook ---
    # These were declared in models/order.py since Task A6/B but never had an
    # explicit migration — they only existed where Base.metadata.create_all()
    # had run. SKIP_CREATE_ALL=true in serverless / VM rendered them invisible.
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS fulfillment_status VARCHAR(32);",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS fulfillment_awb VARCHAR(100);",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS fulfillment_tracking_url VARCHAR(1024);",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS fulfillment_last_event_at TIMESTAMP WITH TIME ZONE;",
    "CREATE INDEX IF NOT EXISTS ix_orders_fulfillment_status ON orders (fulfillment_status);",

    # --- orders: ONDC RSP settlement columns (Task A6) — same root cause ---
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS settlement_status VARCHAR(16);",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS settlement_basis VARCHAR(16);",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS settlement_window VARCHAR(8);",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS settlement_reference VARCHAR(255);",
    "CREATE INDEX IF NOT EXISTS ix_orders_settlement_reference ON orders (settlement_reference);",

    # --- escrow_ledger: status + external_ref ---
    # Enum: PENDING | COMPLETED | FAILED. Default COMPLETED for backfill
    # (legacy mock rows are all settled).
    """
    DO $$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='escrow_entry_status') THEN
        CREATE TYPE escrow_entry_status AS ENUM ('PENDING', 'COMPLETED', 'FAILED');
      END IF;
    END $$;
    """,
    "ALTER TABLE escrow_ledger ADD COLUMN IF NOT EXISTS status escrow_entry_status NOT NULL DEFAULT 'COMPLETED';",
    "ALTER TABLE escrow_ledger ADD COLUMN IF NOT EXISTS external_ref VARCHAR(128);",
    "CREATE INDEX IF NOT EXISTS ix_escrow_ledger_external_ref ON escrow_ledger (external_ref);",

    # --- bot_carts: Xendit invoice id (links cart ↔ PAID webhook) ---
    "ALTER TABLE bot_carts ADD COLUMN IF NOT EXISTS xendit_invoice_id VARCHAR(64);",
    "CREATE INDEX IF NOT EXISTS ix_bot_carts_xendit_invoice_id ON bot_carts (xendit_invoice_id);",
]


def print_dry_run_sql() -> None:
    print("-- add-xendit-biteship-columns.py (dry-run)")
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
        description="Idempotently add Xendit + Biteship columns. Default is dry-run."
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
