"""Idempotently create buyer-feature tables: wishlist, loyalty, coupons.

Run on the VM as postgres:
    sudo -u postgres psql -d beli_aman -f <(python scripts/add-buyer-features.py)
or with --apply against DATABASE_URL.
"""

from __future__ import annotations

import argparse
import os
import sys

DDL_STATEMENTS: list[str] = [
    # --- wishlist ---
    """CREATE TABLE IF NOT EXISTS wishlist_items (
        id VARCHAR(36) PRIMARY KEY,
        profile_id VARCHAR(36) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        brand_slug VARCHAR(120) NOT NULL,
        sku VARCHAR(120) NOT NULL,
        name VARCHAR(512),
        price_idr INTEGER,
        image VARCHAR(1024),
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT uq_wishlist_items_profile_sku UNIQUE (profile_id, sku)
    );""",
    "CREATE INDEX IF NOT EXISTS ix_wishlist_items_profile_id ON wishlist_items (profile_id);",
    # --- loyalty ---
    """CREATE TABLE IF NOT EXISTS loyalty_transactions (
        id VARCHAR(36) PRIMARY KEY,
        profile_id VARCHAR(36) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        order_id VARCHAR(36),
        points INTEGER NOT NULL,
        kind VARCHAR(16) NOT NULL,
        description VARCHAR(255),
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL
    );""",
    "CREATE INDEX IF NOT EXISTS ix_loyalty_transactions_profile_id ON loyalty_transactions (profile_id);",
    "CREATE INDEX IF NOT EXISTS ix_loyalty_transactions_order_id ON loyalty_transactions (order_id);",
    # --- coupons ---
    """CREATE TABLE IF NOT EXISTS coupons (
        id VARCHAR(36) PRIMARY KEY,
        code VARCHAR(40) NOT NULL,
        title VARCHAR(160) NOT NULL,
        description VARCHAR(512),
        discount_type VARCHAR(16) NOT NULL,
        discount_value INTEGER NOT NULL,
        min_spend_idr INTEGER NOT NULL DEFAULT 0,
        brand_slug VARCHAR(120),
        valid_from TIMESTAMP WITH TIME ZONE,
        valid_until TIMESTAMP WITH TIME ZONE,
        max_uses INTEGER,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL
    );""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_coupons_code ON coupons (code);",
    """CREATE TABLE IF NOT EXISTS buyer_coupons (
        id VARCHAR(36) PRIMARY KEY,
        profile_id VARCHAR(36) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        coupon_id VARCHAR(36) NOT NULL REFERENCES coupons(id) ON DELETE CASCADE,
        claimed_at TIMESTAMP WITH TIME ZONE,
        used_at TIMESTAMP WITH TIME ZONE,
        order_id VARCHAR(36),
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT uq_buyer_coupons_profile_coupon UNIQUE (profile_id, coupon_id)
    );""",
    "CREATE INDEX IF NOT EXISTS ix_buyer_coupons_profile_id ON buyer_coupons (profile_id);",
    "CREATE INDEX IF NOT EXISTS ix_buyer_coupons_coupon_id ON buyer_coupons (coupon_id);",
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
    parser = argparse.ArgumentParser(description="Create buyer-feature tables. Default dry-run.")
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
