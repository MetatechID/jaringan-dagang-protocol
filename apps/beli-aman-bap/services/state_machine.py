"""The single source of truth for Order state transitions.

Every code path that mutates `Order.state` MUST go through `transition()`.
This guarantees an OrderEvent row per change and one hook for future
side-effects (webhooks, push notifications, Beckn /on_status callbacks).
"""

from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.order import Order, OrderState
from models.order_event import OrderEvent

# Allowed transitions per state. Keep in lockstep with OrderState.
ALLOWED: dict[OrderState, set[OrderState]] = {
    OrderState.PRE_AUTH: {OrderState.AUTHED, OrderState.REFUNDED},
    OrderState.AUTHED: {OrderState.CART_REVIEWED, OrderState.REFUNDED},
    OrderState.CART_REVIEWED: {OrderState.ESCROW_HELD, OrderState.REFUNDED},
    OrderState.ESCROW_HELD: {
        OrderState.FULFILLING,
        OrderState.DISPUTED,
        OrderState.REFUNDED,
    },
    OrderState.FULFILLING: {OrderState.RECEIVED, OrderState.DISPUTED},
    OrderState.RECEIVED: {OrderState.ESCROW_RELEASED, OrderState.DISPUTED},
    OrderState.DISPUTED: {OrderState.ESCROW_RELEASED, OrderState.REFUNDED},
    OrderState.ESCROW_RELEASED: set(),
    OrderState.REFUNDED: set(),
}


class StateTransitionError(ValueError):
    """Raised when a requested transition is not allowed from the current state."""


async def transition(
    db: AsyncSession,
    order: Order,
    to: OrderState,
    *,
    actor: str,
    payload: Mapping[str, Any] | None = None,
) -> Order:
    """Move `order` to `to` if allowed, recording an OrderEvent.

    Caller is responsible for `db.commit()`.
    """
    if order.state == to:
        # idempotent no-op
        return order

    allowed = ALLOWED.get(order.state, set())
    if to not in allowed:
        raise StateTransitionError(
            f"Cannot transition {order.state.value} -> {to.value}. "
            f"Allowed: {sorted(s.value for s in allowed)}"
        )

    event = OrderEvent(
        order_id=order.id,
        from_state=order.state,
        to_state=to,
        actor=actor,
        payload=dict(payload) if payload else None,
    )
    db.add(event)
    order.state = to
    return order


async def lock_order_for_update(db: AsyncSession, order_id: str) -> Order | None:
    """SELECT ... FOR UPDATE — serializes concurrent admin clicks."""
    result = await db.execute(
        select(Order).where(Order.id == order_id).with_for_update()
    )
    return result.scalar_one_or_none()
