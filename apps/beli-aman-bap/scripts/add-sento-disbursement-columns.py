"""Idempotent migration: add Sento disbursement ("remit") columns to Brand.

Companion to ``add-sento-payment-provider-and-columns.py`` (which added the
``sento_api_key`` / ``sento_username`` / ``sento_callback_secret`` credential
columns). This one adds the three per-Brand *payout target* columns the release
leg reads when ``payment_provider == "sento"``:

- ``sento_disbursement_bank_code`` — Sento NUMERIC bank code (e.g. "014" BCA),
  not Xendit's string codes.
- ``sento_disbursement_bank_account`` — recipient account number, digits only.
- ``sento_disbursement_holder_name`` — record/UI parity only; Sento's
  create-disbursement does not accept a recipient name (returned in the
  callback instead).

Funds custody stays with Sento's partner balance (no per-brand sub-account);
``services/sento_disbursements.disburse_to_seller`` disburses from that balance
to the bank below on escrow release. Plaintext v1 — encrypt at rest when KMS
lands, matching the Xendit/OY columns.

Usage
-----

    # Dry run (prints SQL):
    python apps/beli-aman-bap/scripts/add-sento-disbursement-columns.py

    # Apply against live DB:
    DATABASE_URL=postgresql+asyncpg://... \\
        python apps/beli-aman-bap/scripts/add-sento-disbursement-columns.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys


DDL_STATEMENTS: list[str] = [
    # --- brands: per-Brand Sento disbursement (remit) target ---
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS sento_disbursement_bank_code VARCHAR(16);",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS sento_disbursement_bank_account VARCHAR(64);",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS sento_disbursement_holder_name VARCHAR(255);",
]


def print_dry_run_sql() -> None:
    print("-- add-sento-disbursement-columns.py (dry-run)")
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
        description="Idempotent Sento disbursement columns migration. Default is dry-run."
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
