"""Coupons / vouchers — list currently-active coupons and claim them.

v1 scope: list + claim. Redemption-at-checkout is deferred (no checkout
integration here).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_profile
from models.coupon import BuyerCoupon, Coupon
from models.profile import BeliAmanProfile

router = APIRouter(prefix="/api/v1/me/coupons", tags=["coupons"])


def _serialize(coupon: Coupon, bc: BuyerCoupon | None) -> dict:
    return {
        "id": coupon.id,
        "code": coupon.code,
        "title": coupon.title,
        "description": coupon.description,
        "discount_type": coupon.discount_type,
        "discount_value": coupon.discount_value,
        "min_spend_idr": coupon.min_spend_idr,
        "brand_slug": coupon.brand_slug,
        "valid_until": coupon.valid_until,
        "claimed": bool(bc is not None and bc.claimed_at is not None),
        "used": bool(bc is not None and bc.used_at is not None),
    }


@router.get("")
async def list_coupons(
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Coupon, BuyerCoupon)
        .outerjoin(
            BuyerCoupon,
            (BuyerCoupon.coupon_id == Coupon.id)
            & (BuyerCoupon.profile_id == profile.id),
        )
        .where(
            Coupon.active == True,  # noqa: E712
            (Coupon.valid_until.is_(None)) | (Coupon.valid_until >= now),
        )
    )
    return {"data": [_serialize(coupon, bc) for (coupon, bc) in result.all()]}


@router.post("/{coupon_id}/claim")
async def claim_coupon(
    coupon_id: str,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict:
    now = datetime.now(timezone.utc)
    coupon = (
        await db.execute(
            select(Coupon).where(Coupon.id == coupon_id, Coupon.active == True)  # noqa: E712
        )
    ).scalar_one_or_none()
    if coupon is None:
        raise HTTPException(status_code=404, detail="coupon not found")

    existing = (
        await db.execute(
            select(BuyerCoupon).where(
                BuyerCoupon.profile_id == profile.id,
                BuyerCoupon.coupon_id == coupon_id,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        existing = BuyerCoupon(
            profile_id=profile.id,
            coupon_id=coupon_id,
            claimed_at=now,
        )
        db.add(existing)
        await db.flush()
    elif existing.claimed_at is None:
        existing.claimed_at = now
        await db.flush()

    return {"data": {"coupon_id": coupon_id, "claimed": True}}
