"""Idempotently add Jubelio carrier columns to brands / orders.

Mirrors the add-xendit-biteship-columns.py pattern: dry-run by default,
``--apply`` to mutate. Run on the VM as the postgres superuser since the BAP
tables are owned by ``postgres``:

Usage
-----

    # Dry run (safe, prints SQL):
    python apps/beli-aman-bap/scripts/add-jubelio-columns.py

    # Apply against live DB:
    DATABASE_URL=postgresql+asyncpg://... \\
        python apps/beli-aman-bap/scripts/add-jubelio-columns.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys


DDL_STATEMENTS: list[str] = [
    # --- brands: per-brand Jubelio toggle + pickup origin ---
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS jubelio_enabled BOOLEAN NOT NULL DEFAULT FALSE;",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS jubelio_origin_address JSONB;",

    # --- orders: generic carrier marker + Jubelio shipment id ---
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS carrier VARCHAR(16);",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS jubelio_shipment_id VARCHAR(64);",
    "CREATE INDEX IF NOT EXISTS ix_orders_jubelio_shipment_id ON orders (jubelio_shipment_id);",
]


def print_dry_run_sql() -> None:
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
        description="Idempotently add Jubelio carrier columns. Default is dry-run."
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
