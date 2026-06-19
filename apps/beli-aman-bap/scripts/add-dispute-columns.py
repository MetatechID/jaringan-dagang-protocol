"""Idempotently reconcile the disputes table with models/dispute.py.

The Dispute model gained columns (resolved_at, bpp_refund_request_id,
bpp_issue_id, bpp_resolution_note, …) that were never migrated into the
production `disputes` table — so GET /api/v1/orders/{id} (which now surfaces the
latest dispute) 500'd with `column disputes.resolved_at does not exist`. This
adds any missing columns.

Run on the VM as postgres (tables owned by postgres):
    sudo -u postgres psql -d beli_aman -f <(python scripts/add-dispute-columns.py)
or with --apply against DATABASE_URL.
"""

from __future__ import annotations

import argparse
import os
import sys

DDL_STATEMENTS: list[str] = [
    "ALTER TABLE disputes ADD COLUMN IF NOT EXISTS opened_by VARCHAR(64);",
    "ALTER TABLE disputes ADD COLUMN IF NOT EXISTS note TEXT;",
    "ALTER TABLE disputes ADD COLUMN IF NOT EXISTS evidence JSONB;",
    "ALTER TABLE disputes ADD COLUMN IF NOT EXISTS brand_response JSONB;",
    "ALTER TABLE disputes ADD COLUMN IF NOT EXISTS resolution VARCHAR(64);",
    "ALTER TABLE disputes ADD COLUMN IF NOT EXISTS resolved_at VARCHAR(40);",
    "ALTER TABLE disputes ADD COLUMN IF NOT EXISTS bpp_refund_request_id VARCHAR(64);",
    "ALTER TABLE disputes ADD COLUMN IF NOT EXISTS bpp_issue_id VARCHAR(64);",
    "ALTER TABLE disputes ADD COLUMN IF NOT EXISTS bpp_resolution_note TEXT;",
]


def print_dry_run_sql() -> None:
    print("BEGIN;")
    for s in DDL_STATEMENTS:
        print(s)
    print("COMMIT;")


async def apply_migration(database_url: str) -> int:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url)
    count = 0
    async with engine.begin() as conn:
        for s in DDL_STATEMENTS:
            await conn.execute(text(s))
            count += 1
    await engine.dispose()
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Add missing disputes columns. Default dry-run.")
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
    print(f"done. statements executed: {asyncio.run(apply_migration(db_url))}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
