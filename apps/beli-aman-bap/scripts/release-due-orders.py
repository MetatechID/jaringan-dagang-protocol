"""Release every order whose D+3 window has passed (run by a systemd timer).

Until now an order was only released when someone opened it (lazy release on
read), so an order nobody looked at stayed held forever. One transaction per
order; a refused release is recorded in the escrow ledger and skipped. The
seller is told about each release before the next order.

    python scripts/release-due-orders.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def run(*, dry_run: bool = False, session_factory=None) -> Counter:
    from sqlalchemy import select

    from database import async_session
    from models.order import Order, OrderState
    from services import auto_release, seller_bridge
    from services.state_machine import lock_order_for_update

    Session = session_factory or async_session
    now = datetime.now(timezone.utc)
    async with Session() as db:
        ids = (await db.execute(
            select(Order.id).where(
                Order.state == OrderState.RECEIVED,
                Order.auto_release_at.is_not(None),
                Order.auto_release_at <= now,
            ).order_by(Order.auto_release_at)
        )).scalars().all()

    seen: Counter = Counter()
    for order_id in ids:
        if dry_run:
            seen["due"] += 1
            continue
        async with Session() as db:
            order = await lock_order_for_update(db, order_id)
            released = bool(order) and await auto_release.release_if_due(
                db, order, reason="D+3 elapsed (scheduled sweep)"
            )
            await db.commit()
        if released:
            seen["released"] += 1
            await seller_bridge.patch_escrow_status(order_id=order_id, escrow_status="released")
        else:
            seen["skipped"] += 1
    return seen


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    seen = asyncio.run(run(dry_run=ap.parse_args().dry_run))
    print(", ".join(f"{k}={v}" for k, v in seen.items()) or "nothing due")


if __name__ == "__main__":
    main()
