"""Explicit Midtrans schema migration. Default dry-run; never prints DB URL."""
import argparse
import asyncio
import os

DDL_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS midtrans_payment_links (
        id VARCHAR(36) PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
        brand_id VARCHAR(36) NOT NULL REFERENCES brands(id),
        order_id VARCHAR(36) UNIQUE REFERENCES orders(id),
        cart_id VARCHAR(36) UNIQUE REFERENCES bot_carts(id),
        base_reference VARCHAR(50) NOT NULL UNIQUE, amount_idr INTEGER NOT NULL CHECK (amount_idr > 0),
        environment VARCHAR(16) NOT NULL, status VARCHAR(24) NOT NULL,
        invoice_url VARCHAR(1024), expires_at TIMESTAMPTZ NOT NULL, paid_attempt_id VARCHAR(128),
        CONSTRAINT midtrans_one_owner CHECK ((order_id IS NULL) <> (cart_id IS NULL)))""",
    "CREATE INDEX IF NOT EXISTS ix_midtrans_payment_links_brand_id ON midtrans_payment_links(brand_id)",
    """CREATE TABLE IF NOT EXISTS midtrans_payment_attempts (
        id VARCHAR(36) PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
        link_id VARCHAR(36) NOT NULL REFERENCES midtrans_payment_links(id),
        attempt_reference VARCHAR(128) NOT NULL UNIQUE, transaction_id VARCHAR(128) NOT NULL UNIQUE,
        status VARCHAR(32) NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_midtrans_payment_attempts_link_id ON midtrans_payment_attempts(link_id)",
]


async def apply_migration(url):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            for statement in DDL_STATEMENTS:
                await connection.execute(text(statement))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply:
        url = os.environ.get("DATABASE_URL")
        if not url:
            parser.error("--apply requires DATABASE_URL")
        asyncio.run(apply_migration(url))
    else:
        print("BEGIN;\n" + ";\n".join(DDL_STATEMENTS) + ";\nCOMMIT;")
