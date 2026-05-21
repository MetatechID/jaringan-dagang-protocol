"""Task B3a — idempotently create the bot-facing REST tables in the BAP DB.

Same convention as ``add-mirror-tables.py``: dry-run by default, ``--apply``
required to actually mutate the live DB. Postgres ``CREATE TABLE IF NOT
EXISTS`` makes it safe to rerun.

What it creates
---------------
- ``bot_search_sessions`` — one row per ``POST /api/v1/search`` call.
- ``bot_carts``          — one row per ``POST /api/v1/cart/select`` call.

These tables back the new search / cart / checkout REST surface that the
B3 jd-sell MCP server calls. They do NOT replace any existing buyer-side
tables; the storefront's Order flow is untouched.

Usage
-----

    # Dry run (safe, prints SQL):
    python apps/beli-aman-bap/scripts/add-bot-rest-tables.py

    # Apply against live DB:
    DATABASE_URL=postgresql+asyncpg://... \\
        python apps/beli-aman-bap/scripts/add-bot-rest-tables.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys


DDL_STATEMENTS: list[str] = [
    # --- bot_search_sessions ---
    """
    CREATE TABLE IF NOT EXISTS bot_search_sessions (
        id              VARCHAR(36) PRIMARY KEY,
        customer_id     VARCHAR(36),
        query           VARCHAR(500) NOT NULL,
        category        VARCHAR(100),
        city            VARCHAR(50) NOT NULL DEFAULT 'std:021',
        status          VARCHAR(20) NOT NULL DEFAULT 'pending',
        transaction_id  VARCHAR(64) NOT NULL,
        bpp_id          VARCHAR(255),
        results_json    JSON,
        created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
        expires_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT (NOW() + INTERVAL '30 minutes')
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_bot_search_sessions_customer_id ON bot_search_sessions (customer_id);",
    "CREATE INDEX IF NOT EXISTS ix_bot_search_sessions_transaction_id ON bot_search_sessions (transaction_id);",
    "CREATE INDEX IF NOT EXISTS ix_bot_search_sessions_status ON bot_search_sessions (status);",

    # --- bot_carts ---
    # ``order_id`` is a synthetic bot-side identifier — no FK to ``orders.id``
    # because the bot doesn't materialize a real Order row (no Firebase
    # profile_id). The trailing ALTER TABLE handles legacy environments where
    # a prior run of this script created the FK; on fresh installs the
    # IF EXISTS guard turns it into a no-op.
    """
    CREATE TABLE IF NOT EXISTS bot_carts (
        id                VARCHAR(36) PRIMARY KEY,
        customer_id       VARCHAR(36),
        search_session_id VARCHAR(36)
            REFERENCES bot_search_sessions(id) ON DELETE SET NULL,
        bpp_id            VARCHAR(255) NOT NULL,
        bpp_uri           VARCHAR(512),
        provider_id       VARCHAR(255),
        items_json        JSON NOT NULL,
        quote_json        JSON,
        quote_token       VARCHAR(255),
        status            VARCHAR(20) NOT NULL DEFAULT 'open',
        transaction_id    VARCHAR(64) NOT NULL,
        billing_json      JSON,
        shipping_json     JSON,
        order_id          VARCHAR(36),
        qr_image_url      VARCHAR(1024),
        payment_state     VARCHAR(20) NOT NULL DEFAULT 'pending',
        created_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
        updated_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
        expires_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT (NOW() + INTERVAL '30 minutes')
    );
    """,
    "ALTER TABLE bot_carts DROP CONSTRAINT IF EXISTS bot_carts_order_id_fkey;",
    "CREATE INDEX IF NOT EXISTS ix_bot_carts_customer_id ON bot_carts (customer_id);",
    "CREATE INDEX IF NOT EXISTS ix_bot_carts_search_session_id ON bot_carts (search_session_id);",
    "CREATE INDEX IF NOT EXISTS ix_bot_carts_transaction_id ON bot_carts (transaction_id);",
    "CREATE INDEX IF NOT EXISTS ix_bot_carts_status ON bot_carts (status);",
    "CREATE INDEX IF NOT EXISTS ix_bot_carts_order_id ON bot_carts (order_id);",
]


def print_dry_run_sql() -> None:
    """Print the SQL that --apply would execute. Safe."""
    print("-- jaringan-dagang-buyer / add-bot-rest-tables.py (dry-run)")
    print("-- Task B3a — idempotently create bot_search_sessions + bot_carts.")
    print()
    print("BEGIN;")
    for stmt in DDL_STATEMENTS:
        s = stmt.strip()
        if s:
            print(s)
    print("COMMIT;")
    print()
    print("-- Re-running is safe:")
    print("--   * CREATE TABLE uses IF NOT EXISTS.")
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
            "Idempotently create bot_search_sessions and bot_carts in the BAP "
            "DB. Default is dry-run (print SQL only)."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute the DDL against $DATABASE_URL. Without this flag, only SQL is printed.",
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

    print(f"Applying bot-REST DDL against {db_url[:40]}...")
    count = asyncio.run(apply_migration(db_url))
    print(f"done. statements executed: {count}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
