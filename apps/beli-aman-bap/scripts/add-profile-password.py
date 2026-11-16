"""Add ``profiles.password_hash`` for operator-created email + password buyers.

Idempotent. Dry-run by default; pass ``--apply`` to mutate the DB.

    python apps/beli-aman-bap/scripts/add-profile-password.py
    DATABASE_URL=postgresql+asyncpg://... \\
        python apps/beli-aman-bap/scripts/add-profile-password.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

DDL_STATEMENTS: list[str] = [
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS password_hash TEXT;",
]


async def apply_async(database_url: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            for stmt in DDL_STATEMENTS:
                await conn.execute(text(stmt))
        print("Applied: profiles.password_hash ensured.")
    finally:
        await engine.dispose()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="mutate the DB (default: dry-run)")
    args = p.parse_args()
    if not args.apply:
        print("-- add-profile-password.py (dry-run)")
        print("\n".join(DDL_STATEMENTS))
        return
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("error: DATABASE_URL env var required for --apply", file=sys.stderr)
        sys.exit(2)
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    asyncio.run(apply_async(url))


if __name__ == "__main__":
    main()
