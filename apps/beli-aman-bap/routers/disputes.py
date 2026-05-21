"""Disputes — V1 stub. Marks the order DISPUTED and stores reason + evidence."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_profile
from models.dispute import Dispute, DisputeReason, DisputeStatus
from models.order import Order, OrderState
from models.profile import BeliAmanProfile
from services.state_machine import StateTransitionError, lock_order_for_update, transition

router = APIRouter(prefix="/api/v1/disputes", tags=["disputes"])


class DisputeIn(BaseModel):
    order_id: str
    reason: DisputeReason
    note: str | None = None
    evidence: dict | None = None


@router.post("")
async def open_dispute(
    body: DisputeIn,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict:
    order = await lock_order_for_update(db, body.order_id)
    if not order or order.profile_id != profile.id:
        raise HTTPException(404, "Order not found")

    try:
        await transition(db, order, OrderState.DISPUTED,
                         actor=f"buyer:{profile.id}",
                         payload={"reason": body.reason.value, "note": body.note})
    except StateTransitionError as e:
        raise HTTPException(409, str(e))

    d = Dispute(
        order_id=order.id,
        opened_by=f"buyer:{profile.id}",
        reason=body.reason,
        note=body.note,
        evidence=body.evidence,
        status=DisputeStatus.OPEN,
    )
    db.add(d)
    await db.flush()
    await db.commit()

    # Send Beckn /update with refund_request — best effort, non-fatal.
    try:
        from beckn.outbound import build_ondc_context, send_beckn_request
        import os as _os
        bpp_id = order.bpp_id or _os.environ.get("DEFAULT_BPP_ID", "bpp.jaringan-dagang.id")
        bpp_uri = _os.environ.get("DEFAULT_BPP_URL", "http://localhost:8001/beckn")
        env = {
            "context": build_ondc_context(
                action="update",
                bpp_id=bpp_id,
                bpp_uri=bpp_uri,
                transaction_id=str(order.id),
            ),
            "message": {
                "order": {
                    "id": order.seller_order_ref or order.id,
                    "fulfillment_state": {"descriptor": {
                        "code": "refund_request",
                        "short_desc": body.reason.value,
                        "name": body.note or "",
                    }},
                    "payment": {"params": {"amount": str(order.total_idr or 0), "currency": "IDR"}},
                }
            },
        }
        await send_beckn_request(
            bpp_id=bpp_id, action="update", body=env,
            target_url=f"{bpp_uri.rstrip('/')}/update",
        )
    except Exception:
        import logging
        logging.getLogger(__name__).exception("beckn /update for refund failed")

    return {"id": d.id, "order_id": d.order_id, "status": d.status.value}


@router.get("/{dispute_id}")
async def get_dispute(
    dispute_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(select(Dispute).where(Dispute.id == dispute_id))
    d = result.scalar_one_or_none()
    if not d:
        raise HTTPException(404, "Dispute not found")
    return {
        "id": d.id,
        "order_id": d.order_id,
        "reason": d.reason.value,
        "note": d.note,
        "evidence": d.evidence,
        "status": d.status.value,
        "resolution": d.resolution,
        "created_at": d.created_at.isoformat(),
    }
