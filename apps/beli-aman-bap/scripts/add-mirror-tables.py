"""Task A4 — idempotently create the Beckn catalog mirror tables and the
new ``mirror_stores.image_base_url`` column in the BAP's Postgres.

Why this script exists
----------------------
The BAP has no Alembic in active use. Table creation goes through
``Base.metadata.create_all`` in ``main.py`` lifespan startup; that works
for fresh deploys but is a no-op for live DBs that already have *some* of
these tables (or are missing the latest column added to an existing one).

Postgres supports ``CREATE TABLE IF NOT EXISTS`` + ``ADD COLUMN IF NOT
EXISTS``, so this script is idempotent and rerun-safe.

What it creates / alters
------------------------
- ``mirror_stores`` — one row per BPP storefront.
- ``mirror_products`` — products synced from each store.
- ``mirror_skus`` — variant rows under each product.
- ``mirror_product_images``, ``mirror_sku_images`` — image catalogs.
- ``mirror_stores.image_base_url`` — new column (added Task A4) for the
  per-store CDN/origin base, paralleling the seller's ``Store.image_base_url``
  (Task A7).

Behaviour
---------
Default = **dry-run**: prints the SQL it would execute. No DB connection
needed, no env vars required.

``--apply`` requires ``DATABASE_URL`` to be set and runs the DDL.

Usage
-----

    # Dry run (safe, prints SQL):
    python apps/beli-aman-bap/scripts/add-mirror-tables.py

    # Actually apply against live DB:
    DATABASE_URL=postgresql+asyncpg://... \\
        python apps/beli-aman-bap/scripts/add-mirror-tables.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys


DDL_STATEMENTS: list[str] = [
    # --- mirror_stores ---
    """
    CREATE TABLE IF NOT EXISTS mirror_stores (
        id              VARCHAR(36) PRIMARY KEY,
        bpp_id          VARCHAR(255) NOT NULL UNIQUE,
        slug            VARCHAR(100) NOT NULL UNIQUE,
        name            VARCHAR(255) NOT NULL,
        logo_url        VARCHAR(512),
        domain          VARCHAR(100),
        city            VARCHAR(50),
        bpp_uri         VARCHAR(512),
        image_base_url  VARCHAR(512),
        last_pushed_at  TIMESTAMP WITH TIME ZONE,
        last_pulled_at  TIMESTAMP WITH TIME ZONE,
        catalog_version VARCHAR(64),
        created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_mirror_stores_bpp_id ON mirror_stores (bpp_id);",
    "CREATE INDEX IF NOT EXISTS ix_mirror_stores_slug ON mirror_stores (slug);",
    # New column for the per-store image origin (Task A4 / A7 parity).
    # Safe to run even if the table was created by a prior CREATE TABLE
    # that didn't include the column.
    "ALTER TABLE mirror_stores ADD COLUMN IF NOT EXISTS image_base_url VARCHAR(512);",

    # --- mirror_products ---
    """
    CREATE TABLE IF NOT EXISTS mirror_products (
        id              VARCHAR(36) PRIMARY KEY,
        store_id        VARCHAR(36) NOT NULL
            REFERENCES mirror_stores(id) ON DELETE CASCADE,
        bpp_product_id  VARCHAR(100) NOT NULL,
        sku             VARCHAR(100) NOT NULL,
        name            VARCHAR(500) NOT NULL,
        description     TEXT,
        status          VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
        attributes      JSON,
        last_synced_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_mirror_products_store_id ON mirror_products (store_id);",
    "CREATE INDEX IF NOT EXISTS ix_mirror_products_bpp_product_id ON mirror_products (bpp_product_id);",

    # --- mirror_skus ---
    """
    CREATE TABLE IF NOT EXISTS mirror_skus (
        id              VARCHAR(36) PRIMARY KEY,
        product_id      VARCHAR(36) NOT NULL
            REFERENCES mirror_products(id) ON DELETE CASCADE,
        bpp_sku_id      VARCHAR(100) NOT NULL,
        variant_name    VARCHAR(100),
        variant_value   VARCHAR(255),
        sku_code        VARCHAR(100) NOT NULL,
        price           DOUBLE PRECISION NOT NULL,
        original_price  DOUBLE PRECISION,
        stock           INTEGER NOT NULL DEFAULT 0,
        weight_grams    INTEGER,
        last_synced_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_mirror_skus_product_id ON mirror_skus (product_id);",
    "CREATE INDEX IF NOT EXISTS ix_mirror_skus_bpp_sku_id ON mirror_skus (bpp_sku_id);",
    "CREATE INDEX IF NOT EXISTS ix_mirror_skus_sku_code ON mirror_skus (sku_code);",

    # --- mirror_product_images ---
    """
    CREATE TABLE IF NOT EXISTS mirror_product_images (
        id          VARCHAR(36) PRIMARY KEY,
        product_id  VARCHAR(36) NOT NULL
            REFERENCES mirror_products(id) ON DELETE CASCADE,
        url         VARCHAR(1024) NOT NULL,
        position    INTEGER NOT NULL DEFAULT 0,
        is_primary  BOOLEAN NOT NULL DEFAULT FALSE
    );
    """,

    # --- mirror_sku_images ---
    """
    CREATE TABLE IF NOT EXISTS mirror_sku_images (
        id          VARCHAR(36) PRIMARY KEY,
        sku_id      VARCHAR(36) NOT NULL
            REFERENCES mirror_skus(id) ON DELETE CASCADE,
        url         VARCHAR(1024) NOT NULL,
        position    INTEGER NOT NULL DEFAULT 0,
        is_primary  BOOLEAN NOT NULL DEFAULT FALSE
    );
    """,
]


def print_dry_run_sql() -> None:
    """Print the SQL that --apply would execute. Safe."""
    print("-- jaringan-dagang-buyer / add-mirror-tables.py (dry-run)")
    print("-- Task A4 — idempotently create mirror_* tables + image_base_url.")
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
            "Idempotently create mirror_* tables and the new "
            "mirror_stores.image_base_url column in the BAP DB. Default is "
            "dry-run (print SQL only)."
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

    print(f"Applying mirror DDL against {db_url[:40]}...")
    count = asyncio.run(apply_migration(db_url))
    print(f"done. statements executed: {count}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
