"""Idempotently create the storefront_integrations table.

Holds per-tenant analytics/marketing IDs (Google Analytics measurement ID,
Facebook Pixel ID) edited from the buyer-side Vibe admin and read by the
storefront layout to inject `<script>` tags.

Usage
-----

    # Dry run (safe, prints SQL):
    python apps/beli-aman-bap/scripts/add-storefront-integrations-table.py

    # Apply against live DB:
    DATABASE_URL=postgresql+asyncpg://... \\
        python apps/beli-aman-bap/scripts/add-storefront-integrations-table.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys


DDL_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS storefront_integrations (
        id                 VARCHAR(36) PRIMARY KEY,
        tenant_slug        VARCHAR(100) NOT NULL UNIQUE,
        ga_measurement_id  VARCHAR(64),
        fb_pixel_id        VARCHAR(64),
        created_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
        updated_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_storefront_integrations_tenant_slug ON storefront_integrations (tenant_slug);",
]


def print_dry_run_sql() -> None:
    print("-- add-storefront-integrations-table.py (dry-run)")
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
        description="Idempotently create storefront_integrations. Default is dry-run."
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
