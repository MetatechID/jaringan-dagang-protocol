"""Idempotent migration: drop the per-PG bank triplet columns on ``brands``.

Part 2 of the unified-rekening migration — run ONLY after
``add-payout-bank-accounts.py`` has been applied AND the new BAP code
(reading ``payout_bank_accounts`` instead of these columns) is deployed
and verified. Dropping first leaves the old code failing with
UndefinedColumn on every disbursement.

Drops 9 columns: ``{xendit,sento,dipay}_disbursement_bank_{code,account,
holder_name}``. The Dipay CREDENTIALS columns (client_key/secret/
private_key_b64/merchant_id) and ``xendit_sub_account_id`` stay.

Usage
-----

    # Dry run (prints SQL):
    python apps/beli-aman-bap/scripts/drop-per-pg-bank-columns.py

    # Apply against live DB:
    DATABASE_URL=postgresql+asyncpg://... \\
        python apps/beli-aman-bap/scripts/drop-per-pg-bank-columns.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys

PROVIDERS = ("xendit", "sento", "dipay")
FIELDS = ("bank_code", "bank_account", "holder_name")

# DROP COLUMN IF EXISTS keeps re-runs idempotent. No guard script can
# fully prevent dropping while old code still runs — that's an ops
# discipline: apply add-payout-bank-accounts.py + deploy new code first.
DDL_STATEMENTS: list[str] = [
    f"ALTER TABLE brands DROP COLUMN IF EXISTS {p}_disbursement_{f};"
    for p in PROVIDERS
    for f in FIELDS
]


def print_dry_run_sql() -> None:
    print("-- drop-per-pg-bank-columns.py (dry-run)")
    print("BEGIN;")
    for stmt in DDL_STATEMENTS:
        print(stmt)
    print("COMMIT;")


async def apply_migration(database_url: str) -> int:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url)
    count = 0
    async with engine.begin() as conn:
        for stmt in DDL_STATEMENTS:
            await conn.execute(text(stmt))
            count += 1
    await engine.dispose()
    return count


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Drop per-PG disbursement bank columns on brands (unified "
            "payout_bank_accounts migration, part 2). Default is dry-run."
        )
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

    print(f"Dropping per-PG bank columns against {db_url[:40]}...")
    count = asyncio.run(apply_migration(db_url))
    print(f"done. statements executed: {count}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
