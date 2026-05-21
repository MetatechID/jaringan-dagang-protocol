"""ONDC IGM (Issue & Grievance Management) — buyer REST surface.

Task A5 (narrow): a single endpoint the storefront calls to open a
refund-request Issue against a BPP order. Auth: same Firebase profile
that owns the order. Out-of-scope for v1:

* Listing / polling Issues from the buyer side — the storefront keeps
  the Dispute row from the response and polls /api/v1/disputes/{id}.
* Buyer-initiated /issue_status, /CLOSE actions (deferred to A6).

The actual envelope-building + outbound POST + Dispute persistence
lives in :mod:`services.igm`; this layer is auth + validation + a
typed request/response.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_profile
from models.dispute import Dispute
from models.order import Order
from models.profile import BeliAmanProfile
from services import igm as igm_service

router = APIRouter(prefix="/api/v1/orders", tags=["igm"])


class OpenIssueIn(BaseModel):
    category: str = Field(
        ...,
        description=(
            "ONDC IGM category. v1 refund-request path uses 'ITEM'; "
            "other categories accepted but not actioned in v1."
        ),
    )
    sub_category: str = Field(
        ...,
        description=(
            "IGM sub-category. For category=ITEM must be ITM01..ITM05."
        ),
    )
    description: str = Field(
        ..., min_length=1,
        description="Free-text description of the Issue.",
    )
    refund_amount: int | None = Field(
        default=None,
        description="Refund amount in IDR (whole rupiahs).",
    )


def _serialize(d: Dispute) -> dict[str, Any]:
    return {
        "id": d.id,
        "order_id": d.order_id,
        "opened_by": d.opened_by,
        "reason": d.reason.value if d.reason else None,
        "note": d.note,
        "status": d.status.value if d.status else None,
        "resolution": d.resolution,
        "bpp_issue_id": d.bpp_issue_id,
        "bpp_resolution_note": d.bpp_resolution_note,
        "bpp_refund_request_id": d.bpp_refund_request_id,
        "resolved_at": d.resolved_at,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


@router.post("/{order_id}/issue")
async def open_issue(
    order_id: str,
    body: OpenIssueIn,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Open an ONDC IGM Issue against a buyer-owned order.

    Returns the persisted Dispute row (with ``bpp_issue_id`` populated)
    so the storefront can poll for resolution via the existing
    /api/v1/disputes/{id} GET.
    """
    order = await db.get(Order, order_id)
    if order is None or order.profile_id != profile.id:
        raise HTTPException(404, "Order not found")

    try:
        dispute = await igm_service.open_issue(
            db,
            order_id=order_id,
            profile_id=profile.id,
            category=body.category,
            sub_category=body.sub_category,
            description=body.description,
            refund_amount=body.refund_amount,
            complainant_name=getattr(profile, "display_name", None),
            complainant_email=getattr(profile, "email", None),
            complainant_phone=getattr(profile, "phone", None),
        )
    except igm_service.OrderNotFoundError:
        raise HTTPException(404, "Order not found")
    except igm_service.OrderNotEligibleError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        # Invalid category / sub_category from the typed model.
        raise HTTPException(400, str(exc))

    await db.commit()
    await db.refresh(dispute)
    return _serialize(dispute)
