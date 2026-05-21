"""Escrow inspection + buyer-driven confirmation."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_profile
from models.escrow_ledger import EscrowLedger
from models.order import OrderState
from models.profile import BeliAmanProfile
from services import escrow as escrow_service
from services.state_machine import StateTransitionError, lock_order_for_update, transition

router = APIRouter(prefix="/api/v1/orders", tags=["escrow"])


@router.get("/{order_id}/escrow")
async def get_escrow(
    order_id: str,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict:
    order = await lock_order_for_update(db, order_id)
    if not order or order.profile_id != profile.id:
        raise HTTPException(404, "Order not found")

    held = await escrow_service.held_balance(db, order_id=order.id)
    rows = await db.execute(
        select(EscrowLedger).where(EscrowLedger.order_id == order.id).order_by(EscrowLedger.created_at)
    )
    return {
        "order_id": order.id,
        "state": order.state.value,
        "held_balance_idr": held,
        "ledger": [
            {
                "entry_type": r.entry_type.value,
                "amount_idr": r.amount_idr,
                "description": r.description,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows.scalars().all()
        ],
    }


@router.post("/{order_id}/confirm-receipt")
async def buyer_confirm_receipt(
    order_id: str,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Buyer-initiated 'sudah diterima, semua oke'. RECEIVED → ESCROW_RELEASED."""
    order = await lock_order_for_update(db, order_id)
    if not order or order.profile_id != profile.id:
        raise HTTPException(404, "Order not found")
    if order.state not in (OrderState.RECEIVED, OrderState.FULFILLING):
        raise HTTPException(409, f"Cannot confirm receipt in state {order.state.value}")

    if order.state == OrderState.FULFILLING:
        # Treat as combined "delivered + received"
        try:
            await transition(db, order, OrderState.RECEIVED,
                             actor=f"buyer:{profile.id}",
                             payload={"reason": "buyer marked received before carrier event"})
            order.delivered_simulated_at = datetime.now(timezone.utc)
        except StateTransitionError as e:
            raise HTTPException(409, str(e))

    try:
        await transition(db, order, OrderState.ESCROW_RELEASED,
                         actor=f"buyer:{profile.id}",
                         payload={"reason": "buyer confirmed receipt"})
    except StateTransitionError as e:
        raise HTTPException(409, str(e))

    await escrow_service.release(
        db, order_id=order.id, amount_idr=order.total_idr,
        description="Released — buyer confirmed receipt",
    )
    order.released_at = datetime.now(timezone.utc)
    return {"id": order.id, "state": order.state.value}
