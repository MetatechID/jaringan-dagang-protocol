"""D+3 auto-release, shared by the lazy release on read and the scheduled sweep.

The clock (``Order.auto_release_at``) starts when the order is delivered:
a carrier DELIVERED event (``shipment_events``), the buyer confirming receipt,
or the seller marking it delivered in its dashboard (``/seller-fulfillment``).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from models.order import Order, OrderState
from services import escrow as escrow_service
from services.state_machine import StateTransitionError, transition


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_due(order: Order, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return (
        order.state == OrderState.RECEIVED
        and order.auto_release_at is not None
        and _utc(order.auto_release_at) <= now
    )


async def release_if_due(db: AsyncSession, order: Order, *, reason: str) -> bool:
    """Release a RECEIVED order whose D+3 window has passed. The caller holds the
    row lock and tells the seller afterwards. False when not due or the release
    was refused (the attempt is recorded in the escrow ledger by ``release``)."""
    if not is_due(order):
        return False
    try:
        await escrow_service.release(
            db, order_id=order.id, amount_idr=order.total_idr,
            description="Auto-release after D+3",
        )
        await transition(
            db, order, OrderState.ESCROW_RELEASED,
            actor="system:auto_release", payload={"reason": reason},
        )
    except (StateTransitionError, escrow_service.ReleaseFailed):
        return False
    order.released_at = datetime.now(timezone.utc)
    # Loyalty: earn points on auto-release too (idempotent per order).
    from models.loyalty import accrue_for_order

    await accrue_for_order(db, profile_id=order.profile_id, order_id=order.id, total_idr=order.total_idr)
    return True
