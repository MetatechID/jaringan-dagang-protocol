"""Add OTP passwordless-login tables + relax the ``profiles`` schema.

Idempotent. Dry-run by default; pass ``--apply`` to mutate the live DB.

What this does
--------------
1. ``profiles`` was Google-SSO-only:
     - ``google_sub`` was UNIQUE NOT NULL
     - ``email``      was NOT NULL with a non-unique index
   After OTP login lands, any of {google_sub, email, phone_e164} may be
   the first/only identifier for a profile. We drop NOT NULL on the first
   two and add UNIQUE indexes on email and phone_e164 so the auto-merge
   resolver can rely on them.

2. Creates ``otp_codes`` (one active row per ``(channel, contact)``).

Usage
-----

    python apps/beli-aman-bap/scripts/add-otp-passwordless-login.py
    DATABASE_URL=postgresql+asyncpg://... \\
        python apps/beli-aman-bap/scripts/add-otp-passwordless-login.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


DDL_STATEMENTS: list[str] = [
    # --- 1) Relax profiles ---
    "ALTER TABLE profiles ALTER COLUMN google_sub DROP NOT NULL;",
    "ALTER TABLE profiles ALTER COLUMN email DROP NOT NULL;",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_profiles_email ON profiles (email);",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_profiles_phone_e164 ON profiles (phone_e164);",

    # --- 2) otp_codes ---
    """
    CREATE TABLE IF NOT EXISTS otp_codes (
        id          VARCHAR(36) PRIMARY KEY,
        channel     VARCHAR(16)  NOT NULL,
        contact     VARCHAR(255) NOT NULL,
        code_hash   VARCHAR(64)  NOT NULL,
        attempts    INTEGER      NOT NULL DEFAULT 0,
        issued_at   TIMESTAMP WITH TIME ZONE NOT NULL,
        expires_at  TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT uq_otp_codes_channel_contact UNIQUE (channel, contact)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_otp_codes_contact ON otp_codes (contact);",
    "CREATE INDEX IF NOT EXISTS ix_otp_codes_expires_at ON otp_codes (expires_at);",
]


def print_dry_run_sql() -> None:
    print("-- add-otp-passwordless-login.py (dry-run)")
    print("BEGIN;")
    for stmt in DDL_STATEMENTS:
        print(stmt.strip())
    print("COMMIT;")


async def apply_async(database_url: str) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            for stmt in DDL_STATEMENTS:
                await conn.execute(text(stmt))
        print("Applied: profiles relaxed + otp_codes table ensured.")
    finally:
        await engine.dispose()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="mutate the DB (default: dry-run)")
    args = p.parse_args()

    if not args.apply:
        print_dry_run_sql()
        return

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("error: DATABASE_URL env var required for --apply", file=sys.stderr)
        sys.exit(2)
    # Force asyncpg driver (script uses async engine).
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    asyncio.run(apply_async(url))


if __name__ == "__main__":
    main()
