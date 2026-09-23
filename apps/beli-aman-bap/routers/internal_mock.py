"""Admin / demo cockpit endpoints — gated by X-Admin-Token header.

These are the levers the admin page uses to drive the demo lifecycle without
real Xendit / real carrier events.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import get_db
from deps import require_admin_token
from models.order import Order, OrderState
from services import escrow as escrow_service
from services import seller_bridge
from services.release_clock import compute_auto_release_at
from services.state_machine import StateTransitionError, lock_order_for_update, transition

router = APIRouter(prefix="/api/v1/internal-mock", tags=["internal-mock"])


class DipayMockPaidIn(BaseModel):
    invoice_id: str = Field(min_length=1, max_length=128)
    amount_idr: int | None = Field(default=None, ge=0)


@router.post("/dipay-qris-paid", dependencies=[Depends(require_admin_token)])
async def mock_dipay_qris_paid(
    body: DipayMockPaidIn,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Mark a development Dipay mock invoice paid without provider I/O."""
    if str(settings.environment).lower() not in {"development", "test"}:
        raise HTTPException(404, "Not found")
    if not body.invoice_id.startswith("dipay-dev-"):
        raise HTTPException(422, "Only dipay-dev-* invoices may use this route")

    from models.bot_rest import Cart
    from models.order import Order
    from services import order_paid

    cart = (await db.execute(
        select(Cart).where(
            Cart.invoice_id == body.invoice_id,
            Cart.invoice_provider == "dipay",
        )
    )).scalar_one_or_none()
    order = None
    if cart is None:
        order = (await db.execute(
            select(Order).where(
                or_(
                    Order.payment_method_snapshot["invoice_id"].astext == body.invoice_id,
                    Order.payment_method_snapshot["partner_ref"].astext == body.invoice_id,
                )
            )
        )).scalar_one_or_none()
    if cart is None and order is None:
        raise HTTPException(404, "Unknown Dipay mock invoice")
    expected = (
        int((cart.quote_json or {}).get("total_idr") or 0)
        if cart is not None else int(order.total_idr)
    )
    if body.amount_idr is not None and body.amount_idr != expected:
        raise HTTPException(409, "Mock payment amount mismatch")
    if cart is not None:
        cart.payment_state = "paid"
        cart.invoice_provider = "dipay"
        return {"ok": True, "cart_id": cart.id, "payment_state": cart.payment_state}
    paid = await order_paid.mark_order_paid(
        db, order_id=order.id, invoice_id=body.invoice_id,
        actor="system:dipay_mock",
    )
    return {"ok": True, "order_id": paid.id, "state": paid.state.value}


@router.get("/orders", dependencies=[Depends(require_admin_token)])
async def list_all_orders(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """List every order, newest first. Admin cockpit calls this to render the table."""
    result = await db.execute(select(Order).order_by(Order.created_at.desc()))
    return [
        {
            "id": o.id,
            "state": o.state.value,
            "brand_id": o.brand_id,
            "total_idr": o.total_idr,
            "items": o.items,
            "delivered_at": o.delivered_at.isoformat() if o.delivered_at else None,
            "auto_release_at": o.auto_release_at.isoformat() if o.auto_release_at else None,
            "released_at": o.released_at.isoformat() if o.released_at else None,
            "created_at": o.created_at.isoformat(),
        }
        for o in result.scalars().all()
    ]


@router.post("/order/{order_id}/seller-shipped", dependencies=[Depends(require_admin_token)])
async def mock_seller_shipped(order_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    order = await lock_order_for_update(db, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    try:
        await transition(db, order, OrderState.FULFILLING, actor="admin:mock", payload={"event": "seller_shipped"})
    except StateTransitionError as e:
        raise HTTPException(409, str(e))
    order.shipped_at = datetime.now(timezone.utc)
    return {"id": order.id, "state": order.state.value}


@router.post("/order/{order_id}/delivered", dependencies=[Depends(require_admin_token)])
async def mock_delivered(order_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    order = await lock_order_for_update(db, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    try:
        await transition(db, order, OrderState.RECEIVED, actor="admin:mock", payload={"event": "carrier_delivered"})
    except StateTransitionError as e:
        raise HTTPException(409, str(e))
    now = datetime.now(timezone.utc)
    order.delivered_at = now
    # Pin to Asia/Jakarta calendar day (spec §11) — not UTC + 72h.
    order.auto_release_at = compute_auto_release_at(now, settings.auto_release_days)
    return {
        "id": order.id,
        "state": order.state.value,
        "auto_release_at": order.auto_release_at.isoformat(),
    }


@router.post("/order/{order_id}/elapse-d3", dependencies=[Depends(require_admin_token)])
async def mock_elapse_d3(order_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Fast-forward auto_release_at to the past and trigger release inline."""
    order = await lock_order_for_update(db, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    if order.state != OrderState.RECEIVED:
        raise HTTPException(409, f"Order in state {order.state.value}, not RECEIVED")

    order.auto_release_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    try:
        await escrow_service.release(
            db, order_id=order.id, amount_idr=order.total_idr,
            description="Auto-release after D+3 (admin elapse)",
        )
    except escrow_service.ReleaseFailed as e:
        raise HTTPException(e.status_code, str(e)) from e
    try:
        await transition(db, order, OrderState.ESCROW_RELEASED,
                         actor="system:auto_release", payload={"reason": "D+3 elapsed (admin fast-forward)"})
    except StateTransitionError as e:
        raise HTTPException(409, str(e))

    order.released_at = datetime.now(timezone.utc)
    # Best-effort: keep the seller-dashboard escrow mirror in sync.
    asyncio.create_task(
        seller_bridge.patch_escrow_status(order_id=order.id, escrow_status="released")
    )
    return {"id": order.id, "state": order.state.value}


@router.post("/order/{order_id}/release", dependencies=[Depends(require_admin_token)])
async def mock_release(order_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    order = await lock_order_for_update(db, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    try:
        await escrow_service.release(
            db, order_id=order.id, amount_idr=order.total_idr,
            description="Released by admin",
        )
    except escrow_service.ReleaseFailed as e:
        raise HTTPException(e.status_code, str(e)) from e
    try:
        await transition(db, order, OrderState.ESCROW_RELEASED, actor="admin:mock")
    except StateTransitionError as e:
        raise HTTPException(409, str(e))
    order.released_at = datetime.now(timezone.utc)
    # Best-effort: keep the seller-dashboard escrow mirror in sync.
    asyncio.create_task(
        seller_bridge.patch_escrow_status(order_id=order.id, escrow_status="released")
    )
    return {"id": order.id, "state": order.state.value}


@router.post("/order/{order_id}/refund", dependencies=[Depends(require_admin_token)])
async def mock_refund(order_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    order = await lock_order_for_update(db, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    try:
        await transition(db, order, OrderState.REFUNDED, actor="admin:mock")
    except StateTransitionError as e:
        raise HTTPException(409, str(e))
    await escrow_service.refund(db, order_id=order.id, amount_idr=order.total_idr,
                                description="Refunded by admin")
    # Best-effort: keep the seller-dashboard escrow mirror in sync.
    asyncio.create_task(
        seller_bridge.patch_escrow_status(order_id=order.id, escrow_status="refunded")
    )
    return {"id": order.id, "state": order.state.value}
