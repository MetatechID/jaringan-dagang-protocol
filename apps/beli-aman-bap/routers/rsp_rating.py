"""ONDC RSP /settle + Rating REST surface for the buyer (Task A6).

Two endpoints:

* ``POST /api/v1/orders/{order_id}/rating`` (Firebase-authed, owns the order)
    — buyer (or storefront on their behalf) submits a rating set.
* ``POST /api/v1/orders/{order_id}/settle`` (admin-token-gated)
    — operator triggers a settlement record request to the BPP.

The /settle endpoint is admin-only because v1 settlement records are
operator-driven (no buyer-facing UI for settlement). Once we have v2
auto-settlement, the gate will narrow to "BAP system process" and
operators won't trigger this manually.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_profile, require_admin_token
from models.order import Order
from models.order_rating import OrderRating
from models.profile import BeliAmanProfile
from services import rating as rating_service
from services import settlement as settlement_service

router = APIRouter(prefix="/api/v1/orders", tags=["rsp", "rating"])


class RatingItemIn(BaseModel):
    category: str = Field(
        ...,
        description=(
            "ONDC rating category: Item / Order / Fulfillment / Provider / "
            "Agent."
        ),
    )
    value: float = Field(
        ..., ge=1.0, le=5.0,
        description="Rating value in [1.0, 5.0].",
    )
    id: str | None = Field(
        default=None,
        description=(
            "Id of the rated entity (item_id, provider_id). Omitted for "
            "Order-level ratings."
        ),
    )
    comments: str | None = Field(
        default=None,
        description="Optional free-text comments attached to this rating.",
    )


class SubmitRatingIn(BaseModel):
    ratings: list[RatingItemIn] = Field(
        ..., min_length=1,
        description="List of rating entries — at least one required.",
    )


class RequestSettlementIn(BaseModel):
    settlement_basis: str = Field(
        default="DELIVERY",
        description="One of DELIVERY / PICKUP / RECEIPT.",
    )
    settlement_window: str = Field(
        default="P1D",
        description="One of P1D / P3D / P7D (ISO 8601 duration codes).",
    )


def _serialize_rating(r: OrderRating) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "order_id": r.order_id,
        "ratings": r.ratings,
        "acknowledged": bool(r.acknowledged),
        "acknowledged_at": (
            r.acknowledged_at.isoformat() if r.acknowledged_at else None
        ),
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.post("/{order_id}/rating", status_code=202)
async def submit_rating(
    order_id: str,
    body: SubmitRatingIn,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Submit a buyer-side /rating to the BPP for the given order.

    Returns 202 (Accepted) + the persisted OrderRating row. The BPP's
    /on_rating ack flips ``OrderRating.acknowledged`` asynchronously.
    """
    order = await db.get(Order, order_id)
    if order is None or order.profile_id != profile.id:
        raise HTTPException(404, "Order not found")

    try:
        rating_row = await rating_service.submit_rating(
            db,
            order_id=order_id,
            ratings=[r.model_dump() for r in body.ratings],
        )
    except rating_service.OrderNotFoundError:
        raise HTTPException(404, "Order not found")
    except rating_service.OrderNotEligibleError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    await db.commit()
    await db.refresh(rating_row)
    return _serialize_rating(rating_row)


@router.post(
    "/{order_id}/settle",
    status_code=202,
    dependencies=[Depends(require_admin_token)],
)
async def request_settlement(
    order_id: str,
    body: RequestSettlementIn | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Trigger an ONDC RSP /settle request to the BPP for the given order.

    Admin-token-gated (operator-only). v1 settlement records are
    observability-only; the operator settles funds out-of-band.
    """
    body = body or RequestSettlementIn()
    try:
        state = await settlement_service.request_settlement(
            db,
            order_id=order_id,
            settlement_basis=body.settlement_basis,
            settlement_window=body.settlement_window,
        )
    except settlement_service.OrderNotFoundError:
        raise HTTPException(404, "Order not found")
    except settlement_service.OrderNotEligibleError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    await db.commit()
    return state
