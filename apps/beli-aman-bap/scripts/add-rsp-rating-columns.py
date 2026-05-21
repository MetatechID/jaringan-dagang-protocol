"""Task A6 — idempotently add RSP columns to ``orders`` + create ``order_ratings``.

Why this script exists
----------------------
Task A6 ("ONDC RSP + Score + Rating, narrow") extends the BAP schema:

  1. Adds four settlement columns to ``orders``:
     - ``settlement_status``    VARCHAR(16)
     - ``settlement_basis``     VARCHAR(16)
     - ``settlement_window``    VARCHAR(8)
     - ``settlement_reference`` VARCHAR(255)  (indexed)

  2. Creates the ``order_ratings`` table — one row per order capturing
     the buyer's post-fulfillment rating set + BPP /on_rating ack flag.

The BAP has no Alembic in active use; create_all picks up new tables
automatically but ``ALTER TABLE`` for new columns must be done by hand.
Postgres 9.6+ ``ADD COLUMN IF NOT EXISTS`` and ``CREATE TABLE IF NOT
EXISTS`` make this script safe to re-run.

Behaviour
---------
Default = **dry-run**: prints the SQL it would execute. No DB connection
needed, no env vars required.

``--apply`` requires ``DATABASE_URL`` to be set and runs the DDL.

Usage
-----

    # Dry run (safe, prints SQL):
    python apps/beli-aman-bap/scripts/add-rsp-rating-columns.py

    # Actually apply against live DB:
    DATABASE_URL=postgresql+asyncpg://... \\
        python apps/beli-aman-bap/scripts/add-rsp-rating-columns.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys


# 1. Settlement columns on orders. Each ALTER is gated by IF NOT EXISTS.
ALTER_ORDERS_SETTLEMENT_STATUS = (
    "ALTER TABLE orders "
    "ADD COLUMN IF NOT EXISTS settlement_status VARCHAR(16);"
)
ALTER_ORDERS_SETTLEMENT_BASIS = (
    "ALTER TABLE orders "
    "ADD COLUMN IF NOT EXISTS settlement_basis VARCHAR(16);"
)
ALTER_ORDERS_SETTLEMENT_WINDOW = (
    "ALTER TABLE orders "
    "ADD COLUMN IF NOT EXISTS settlement_window VARCHAR(8);"
)
ALTER_ORDERS_SETTLEMENT_REFERENCE = (
    "ALTER TABLE orders "
    "ADD COLUMN IF NOT EXISTS settlement_reference VARCHAR(255);"
)
CREATE_INDEX_ORDERS_SETTLEMENT_REFERENCE = (
    "CREATE INDEX IF NOT EXISTS ix_orders_settlement_reference "
    "ON orders (settlement_reference);"
)

# 2. order_ratings table — one row per order, JSONB ratings array.
CREATE_TABLE_ORDER_RATINGS = """
CREATE TABLE IF NOT EXISTS order_ratings (
    id VARCHAR(36) PRIMARY KEY,
    order_id VARCHAR(36) NOT NULL UNIQUE REFERENCES orders(id),
    ratings JSONB NOT NULL,
    acknowledged BOOLEAN NOT NULL DEFAULT FALSE,
    acknowledged_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
"""
CREATE_INDEX_ORDER_RATINGS_ORDER_ID = (
    "CREATE INDEX IF NOT EXISTS ix_order_ratings_order_id "
    "ON order_ratings (order_id);"
)


DDL_STATEMENTS: list[str] = [
    ALTER_ORDERS_SETTLEMENT_STATUS,
    ALTER_ORDERS_SETTLEMENT_BASIS,
    ALTER_ORDERS_SETTLEMENT_WINDOW,
    ALTER_ORDERS_SETTLEMENT_REFERENCE,
    CREATE_INDEX_ORDERS_SETTLEMENT_REFERENCE,
    CREATE_TABLE_ORDER_RATINGS,
    CREATE_INDEX_ORDER_RATINGS_ORDER_ID,
]


def print_dry_run_sql() -> None:
    """Print the SQL that --apply would execute. Safe."""
    print("-- jaringan-dagang-buyer / add-rsp-rating-columns.py (dry-run)")
    print("-- Task A6 — settlement columns on orders + order_ratings table.")
    print()
    print("BEGIN;")
    for stmt in DDL_STATEMENTS:
        s = stmt.strip()
        if s:
            print(s)
    print("COMMIT;")
    print()
    print("-- Re-running is safe:")
    print("--   * ALTER uses 'ADD COLUMN IF NOT EXISTS'.")
    print("--   * CREATE TABLE / INDEX use 'IF NOT EXISTS'.")


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
            "Idempotently add orders.settlement_* columns + order_ratings "
            "table. Default is dry-run (print SQL only)."
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

    print(f"Applying RSP + rating DDL against {db_url[:40]}...")
    count = asyncio.run(apply_migration(db_url))
    print(f"done. statements executed: {count}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
