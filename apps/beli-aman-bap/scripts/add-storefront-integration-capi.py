"""Idempotently add fb_capi_access_token + fb_capi_test_event_code columns
to storefront_integrations.

The access token is the server-side secret used by services/fb_capi.py to
sign Meta Conversions API Purchase events. test_event_code is an optional
debug helper that routes events to the FB Events Manager "Test events"
tab so we can verify integration before going live.

Usage
-----

    # Dry run:
    python apps/beli-aman-bap/scripts/add-storefront-integration-capi.py

    # Apply:
    DATABASE_URL=postgresql+asyncpg://... \\
        python apps/beli-aman-bap/scripts/add-storefront-integration-capi.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys


DDL_STATEMENTS: list[str] = [
    "ALTER TABLE storefront_integrations ADD COLUMN IF NOT EXISTS fb_capi_access_token VARCHAR(512);",
    "ALTER TABLE storefront_integrations ADD COLUMN IF NOT EXISTS fb_capi_test_event_code VARCHAR(64);",
]


def print_dry_run_sql() -> None:
    print("-- add-storefront-integration-capi.py (dry-run)")
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
        description="Idempotently add CAPI columns. Default is dry-run."
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
