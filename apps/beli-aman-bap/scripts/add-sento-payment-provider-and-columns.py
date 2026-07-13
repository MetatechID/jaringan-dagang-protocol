"""Idempotent migration: add ``sento_*`` columns to Brand.

Companion to ``add-oy-payment-provider-and-rename-cart-columns.py`` —
which already adds the ``payment_provider`` column with default
``'xendit'`` and an index on it. We do NOT re-add it here; flipping
``payment_provider='sento'`` on a Brand row is what opts a tenant into
this gateway.

This script only adds the three per-Brand credential columns Sento needs.
No cart-side changes — Sento reuses ``invoice_id`` / ``invoice_provider`` /
``qr_image_url`` / ``payment_method_snapshot`` the same way OY does.

Usage
-----

    # Dry run (prints SQL):
    python apps/beli-aman-bap/scripts/add-sento-payment-provider-and-columns.py

    # Apply against live DB:
    DATABASE_URL=postgresql+asyncpg://... \\
        python apps/beli-aman-bap/scripts/add-sento-payment-provider-and-columns.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys


DDL_STATEMENTS: list[str] = [
    # --- brands: per-Brand Sento credentials ---
    # Mirrors the oy_* column block. ``sento_api_key`` + ``sento_username``
    # are the headers Sento requires (``x-api-key`` + ``x-username``); the
    # ``sento_callback_secret`` column is reserved for a future shared-
    # secret scheme (Sento's docs don't currently document one — we verify
    # payment state via the status API instead). Plaintext v1.
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS sento_api_key VARCHAR(255);",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS sento_username VARCHAR(128);",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS sento_callback_secret VARCHAR(128);",
]


def print_dry_run_sql() -> None:
    print("-- add-sento-payment-provider-and-columns.py (dry-run)")
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
        description="Idempotent Sento credentials migration. Default is dry-run."
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