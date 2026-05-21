"""Task A5 — idempotently add IGM-tracking columns to ``disputes``.

Why this script exists
----------------------
Task A5 ("ONDC IGM, narrow") extends the existing buyer ``Dispute`` row
with three columns so a dispute raised via the new
``POST /api/v1/orders/{order_id}/issue`` REST endpoint (ONDC IGM /issue)
can be correlated with the BPP's /on_issue response:

- ``bpp_issue_id``         — the IGM Issue UUID we sent at /issue time.
- ``bpp_resolution_note``  — the BPP's resolution short_desc / long_desc
                              recorded on /on_issue (RESOLVED / REJECTED).
- ``resolved_at``          — ISO 8601 timestamp the BPP reached a
                              terminal state.

The BAP has no Alembic in active use; we add columns by ``ALTER TABLE
disputes ADD COLUMN IF NOT EXISTS``. Postgres supports the IF NOT EXISTS
clause on ALTER COLUMN, so this is safe to re-run.

Behaviour
---------
Default = **dry-run**: prints the SQL it would execute. No DB connection
needed, no env vars required.

``--apply`` requires ``DATABASE_URL`` to be set and runs the DDL.

Usage
-----

    # Dry run (safe, prints SQL):
    python apps/beli-aman-bap/scripts/add-dispute-issue-columns.py

    # Actually apply against live DB:
    DATABASE_URL=postgresql+asyncpg://... \\
        python apps/beli-aman-bap/scripts/add-dispute-issue-columns.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys


DDL_STATEMENTS: list[str] = [
    # bpp_issue_id — the IGM Issue id we mint at /issue time so /on_issue
    # responses can be reconciled back to the local Dispute row.
    "ALTER TABLE disputes ADD COLUMN IF NOT EXISTS bpp_issue_id VARCHAR(64);",
    "CREATE INDEX IF NOT EXISTS ix_disputes_bpp_issue_id ON disputes (bpp_issue_id);",
    # bpp_resolution_note — BPP's short_desc/long_desc on RESOLVED/REJECTED.
    "ALTER TABLE disputes ADD COLUMN IF NOT EXISTS bpp_resolution_note TEXT;",
    # resolved_at — ISO 8601 string of when /on_issue reached a terminal state.
    "ALTER TABLE disputes ADD COLUMN IF NOT EXISTS resolved_at VARCHAR(40);",
]


def print_dry_run_sql() -> None:
    """Print the SQL that --apply would execute. Safe."""
    print("-- jaringan-dagang-buyer / add-dispute-issue-columns.py (dry-run)")
    print("-- Task A5 — idempotently add IGM-tracking columns to disputes.")
    print()
    print("BEGIN;")
    for stmt in DDL_STATEMENTS:
        s = stmt.strip()
        if s:
            print(s)
    print("COMMIT;")
    print()
    print("-- Re-running is safe:")
    print("--   * ALTER TABLE uses 'ADD COLUMN IF NOT EXISTS'.")
    print("--   * CREATE INDEX uses IF NOT EXISTS.")


async def apply_migration(database_url: str) -> int:
    """Run all DDL statements transactionally. Returns # of statements run."""
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
        description=(
            "Idempotently add IGM-tracking columns "
            "(bpp_issue_id, bpp_resolution_note, resolved_at) to the "
            "BAP's disputes table. Default is dry-run (print SQL only)."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Execute the DDL against $DATABASE_URL. Without this flag, "
            "only SQL is printed."
        ),
    )
    args = parser.parse_args()

    if not args.apply:
        print_dry_run_sql()
        return 0

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print(
            "ERROR: --apply requires DATABASE_URL to be set in the environment.",
            file=sys.stderr,
        )
        return 2

    import asyncio

    print(f"Applying dispute-issue DDL against {db_url[:40]}...")
    count = asyncio.run(apply_migration(db_url))
    print(f"done. statements executed: {count}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
