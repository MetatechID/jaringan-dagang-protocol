"""Idempotently add the orders.attribution JSONB column.

Holds the ad-attribution snapshot (fbc/fbp/fbclid/ctwa_clid/user_agent/ip/
landing_url) captured by the storefront on order creation, read by
services/fb_capi.py to fire a server-side Meta Conversions API Purchase
event when the order transitions to ESCROW_HELD.

Usage
-----

    # Dry run (safe, prints SQL):
    python apps/beli-aman-bap/scripts/add-order-attribution.py

    # Apply against live DB:
    DATABASE_URL=postgresql+asyncpg://... \\
        python apps/beli-aman-bap/scripts/add-order-attribution.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys


DDL_STATEMENTS: list[str] = [
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS attribution JSONB;",
]


def print_dry_run_sql() -> None:
    print("-- add-order-attribution.py (dry-run)")
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
        description="Idempotently add orders.attribution JSONB. Default is dry-run."
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
