"""Idempotent migration: add Dipay (SNAP v2.1) columns.

Companion to ``add-sento-disbursement-columns.py`` — this one adds the
per-Brand columns the Dipay integration reads/writes:

- ``dipay_client_key`` / ``dipay_client_secret`` / ``dipay_private_key_b64``
  / ``dipay_merchant_id`` — Dipay credentials (X-CLIENT-KEY / X-PARTNER-ID,
  HMAC-SHA512 secret, RSA signing key for the access-token call, and the
  QRIS merchant id). Plaintext v1 — encrypt at rest when KMS lands,
  matching the Xendit/OY/Sento columns.
- ``dipay_disbursement_bank_code`` / ``..._bank_account`` /
  ``..._holder_name`` — the payout target ``services/
  dipay_disbursements.disburse_to_seller`` transfers to on escrow release
  when ``payment_provider == "dipay"``.
- ``escrow_ledger.partner_ref`` — the SNAP ``partnerReferenceNo`` we
  minted (e.g. ``r-{order id}``), so webhook receivers can resolve the
  ledger row by the ref echoed back in Dipay callbacks.
- ``bot_carts.qris_image_url`` / ``bot_carts.qris_content`` — the
  buyer-facing QRIS PNG URL (our own renderer) and the raw EMVCo payload
  it renders from.

Usage
-----

    # Dry run (prints SQL):
    python apps/beli-aman-bap/scripts/add-dipay-columns.py

    # Apply against live DB:
    DATABASE_URL=postgresql+asyncpg://... \\
        python apps/beli-aman-bap/scripts/add-dipay-columns.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys


DDL_STATEMENTS: list[str] = [
    # --- brands: per-Brand Dipay (SNAP v2.1) credentials ---
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS dipay_client_key VARCHAR(255);",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS dipay_client_secret VARCHAR(255);",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS dipay_private_key_b64 TEXT;",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS dipay_merchant_id VARCHAR(64);",
    # --- brands: per-Brand Dipay disbursement (payout) target ---
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS dipay_disbursement_bank_code VARCHAR(16);",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS dipay_disbursement_bank_account VARCHAR(64);",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS dipay_disbursement_holder_name VARCHAR(255);",
    # --- escrow_ledger: SNAP correlation + payout reconciliation ---
    "ALTER TABLE escrow_ledger ADD COLUMN IF NOT EXISTS partner_ref VARCHAR(64);",
    "ALTER TABLE escrow_ledger ADD COLUMN IF NOT EXISTS gross_amount_idr BIGINT;",
    "ALTER TABLE escrow_ledger ADD COLUMN IF NOT EXISTS platform_fee_idr BIGINT;",
    "ALTER TABLE escrow_ledger ADD COLUMN IF NOT EXISTS provider_fee_idr BIGINT;",
    "ALTER TABLE escrow_ledger ADD COLUMN IF NOT EXISTS net_amount_idr BIGINT;",
    # Abort instead of silently dropping/re-keying callback identities if an
    # older deployment already minted duplicate non-null references.
    """
    DO $$
    BEGIN
      IF EXISTS (
        SELECT 1 FROM escrow_ledger
        WHERE partner_ref IS NOT NULL
        GROUP BY partner_ref HAVING COUNT(*) > 1
      ) THEN
        RAISE EXCEPTION 'duplicate escrow_ledger.partner_ref values exist';
      END IF;
    END $$;
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_escrow_ledger_partner_ref ON escrow_ledger(partner_ref);",
    # --- bot_carts: QRIS image URL + raw EMVCo payload ---
    "ALTER TABLE bot_carts ADD COLUMN IF NOT EXISTS qris_image_url VARCHAR(1024);",
    "ALTER TABLE bot_carts ADD COLUMN IF NOT EXISTS qris_content VARCHAR(1024);",
]


def print_dry_run_sql() -> None:
    print("-- add-dipay-columns.py (dry-run)")
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
        description="Idempotent Dipay columns migration. Default is dry-run."
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
