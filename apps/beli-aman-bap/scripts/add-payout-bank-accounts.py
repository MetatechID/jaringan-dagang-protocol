"""Idempotent migration: create ``payout_bank_accounts`` + backfill.

Part 1 of the unified-rekening migration (part 2 is
``drop-per-pg-bank-columns.py``, run AFTER the new code is live).

- Creates the ``payout_bank_accounts`` table (id, brand_id, bank_code,
  account_number, holder_name, is_active, timestamps) plus the partial
  unique index ``uq_payout_bank_accounts_active`` enforcing "at most one
  active account per brand".
- Backfills: for every brand, one row per populated per-PG triplet
  (``{xendit,sento,dipay}_disbursement_bank_{code,account,holder_name}``).
  Per-PG codes are translated to the canonical BI numeric code
  (Xendit's alphabetical codes via the reverse map; unknown codes are
  stored verbatim with a warning). The active row is the one whose
  origin provider matches ``brands.payment_provider``; if none matches,
  the first row becomes active. Brands with no bank data get no rows.

Safe to re-run: ``CREATE TABLE IF NOT EXISTS``, the backfill skips
brands that already have rows, and the INSERTs use per-row existence
checks (brand + account_number + bank_code).

Usage
-----

    # Dry run (prints SQL + backfill plan):
    python apps/beli-aman-bap/scripts/add-payout-bank-accounts.py

    # Apply against live DB:
    DATABASE_URL=postgresql+asyncpg://... \\
        python apps/beli-aman-bap/scripts/add-payout-bank-accounts.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid

# Canonical BI numeric codes — must match services/bank_codes.py.
# Xendit alphabetical code → canonical numeric code.
_XENDIT_TO_CANONICAL = {
    "BCA": "014",
    "MANDIRI": "008",
    "BNI": "009",
    "BRI": "002",
    "PERMATA": "013",
    "CIMB": "022",
    "BSI": "451",
}

DDL_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS payout_bank_accounts (
        id VARCHAR(36) PRIMARY KEY,
        brand_id VARCHAR(36) NOT NULL REFERENCES brands(id),
        bank_code VARCHAR(16) NOT NULL,
        account_number VARCHAR(64) NOT NULL,
        holder_name VARCHAR(255) NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_payout_bank_accounts_brand_id ON payout_bank_accounts(brand_id);",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_payout_bank_accounts_active "
    "ON payout_bank_accounts(brand_id) WHERE is_active;",
]


def _canonical_code(provider: str, code: str | None) -> tuple[str, bool]:
    """Map a per-PG bank code to the canonical numeric code.

    Returns ``(code, known)`` — known=False means stored verbatim
    (warning for ops). Sento/Dipay codes are already canonical.
    """
    code = (code or "").strip()
    if not code:
        return "", False
    if provider == "xendit":
        canon = _XENDIT_TO_CANONICAL.get(code.upper())
        return (canon, True) if canon else (code, False)
    return code, True


def _brand_rows(brand: dict) -> tuple[list[tuple], list[str]]:
    """Build (uuid, brand_id, bank_code, account_number, holder_name,
    is_active, origin_provider) tuples + warnings for one brand."""
    warnings: list[str] = []
    candidates: list[tuple] = []
    for provider in ("xendit", "sento", "dipay"):
        code = brand.get(f"{provider}_disbursement_bank_code")
        account = (brand.get(f"{provider}_disbursement_bank_account") or "").strip()
        holder = (
            brand.get(f"{provider}_disbursement_holder_name") or brand.get("name") or ""
        ).strip()
        if not account:
            continue
        canon, known = _canonical_code(provider, code)
        if not known:
            warnings.append(
                f"brand {brand['slug']}: {provider} bank code {code!r} has no "
                f"canonical mapping — stored verbatim"
            )
        candidates.append(
            (str(uuid.uuid4()), brand["id"], canon, account, holder, provider)
        )

    active_provider = (brand.get("payment_provider") or "").strip().lower()
    rows: list[tuple] = []
    for i, (aid, brand_id, code, account, holder, provider) in enumerate(candidates):
        is_active = provider == active_provider or (
            active_provider not in {c[5] for c in candidates} and i == 0
        )
        rows.append((aid, brand_id, code, account, holder, is_active))
    return rows, warnings


def _backfill_sql(rows: list[tuple]) -> list[str]:
    """Idempotent INSERTs — skip rows that already exist (same brand +
    bank_code + account_number), then enforce one active per brand."""
    stmts: list[str] = []
    for aid, brand_id, code, account, holder, is_active in rows:
        stmts.append(
            "INSERT INTO payout_bank_accounts (id, brand_id, bank_code, "
            "account_number, holder_name, is_active) "
            f"SELECT '{aid}', '{brand_id}', '{code}', '{_sql_escape(account)}', "
            f"'{_sql_escape(holder)}', {str(is_active).lower()} "
            "WHERE NOT EXISTS ("
            f"SELECT 1 FROM payout_bank_accounts WHERE brand_id = '{brand_id}' "
            f"AND bank_code = '{code}' AND account_number = '{_sql_escape(account)}');"
        )
    # One active per brand: keep the newest active row, deactivate others.
    stmts.append(
        "UPDATE payout_bank_accounts SET is_active = FALSE WHERE id NOT IN ("
        "SELECT DISTINCT ON (brand_id) id FROM payout_bank_accounts "
        "WHERE is_active ORDER BY brand_id, updated_at DESC);"
    )
    return stmts


def _sql_escape(value: str) -> str:
    return value.replace("'", "''")


def _fetch_brands(database_url: str) -> list[dict]:
    """Read brands + their per-PG bank triplets (sync driver for the script)."""
    import re

    from sqlalchemy import create_engine, text

    sync_url = database_url
    m = re.match(r"postgresql\+asyncpg://(.*)", database_url)
    if m:
        sync_url = f"postgresql://{m.group(1)}"
    engine = create_engine(sync_url)
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT id, slug, name, payment_provider, "
                "xendit_disbursement_bank_code, xendit_disbursement_bank_account, "
                "xendit_disbursement_holder_name, "
                "sento_disbursement_bank_code, sento_disbursement_bank_account, "
                "sento_disbursement_holder_name, "
                "dipay_disbursement_bank_code, dipay_disbursement_bank_account, "
                "dipay_disbursement_holder_name FROM brands;"
            )
        )
        brands = [dict(r._mapping) for r in result]
    engine.dispose()
    return brands


def print_dry_run_sql(rows: list[tuple], warnings: list[str]) -> None:
    print("-- add-payout-bank-accounts.py (dry-run)")
    print("BEGIN;")
    for stmt in DDL_STATEMENTS:
        s = " ".join(stmt.split())
        if s:
            print(s)
    print("-- backfill:")
    for stmt in _backfill_sql(rows):
        print(stmt)
    print("COMMIT;")
    if warnings:
        print("\n-- warnings:")
        for w in warnings:
            print(f"-- {w}")


async def apply_migration(database_url: str, rows: list[tuple]) -> int:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url)
    count = 0
    async with engine.begin() as conn:
        for stmt in DDL_STATEMENTS + _backfill_sql(rows):
            s = " ".join(stmt.split())
            if not s:
                continue
            await conn.execute(text(s))
            count += 1
    await engine.dispose()
    return count


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Idempotent payout_bank_accounts migration (create + backfill). "
            "Default is dry-run."
        )
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL is required (dry-run backfill plan too).", file=sys.stderr)
        return 2

    brands = _fetch_brands(db_url)
    all_rows: list[tuple] = []
    all_warnings: list[str] = []
    for brand in brands:
        rows, warnings = _brand_rows(brand)
        all_rows.extend(rows)
        all_warnings.extend(warnings)

    if not args.apply:
        print_dry_run_sql(all_rows, all_warnings)
        return 0

    import asyncio

    print(f"Applying DDL + backfill ({len(all_rows)} rows) against {db_url[:40]}...")
    count = asyncio.run(apply_migration(db_url, all_rows))
    print(f"done. statements executed: {count}.")
    for w in all_warnings:
        print(f"WARNING: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
