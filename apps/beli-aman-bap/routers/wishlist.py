"""Wishlist — a buyer's saved products, scoped to the current profile."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_profile
from models.profile import BeliAmanProfile
from models.wishlist import WishlistItem

router = APIRouter(prefix="/api/v1/me/wishlist", tags=["wishlist"])


class WishlistItemIn(BaseModel):
    brand_slug: str
    sku: str
    name: str | None = None
    price_idr: int | None = None
    image: str | None = None


def _serialize_item(item: WishlistItem) -> dict:
    return {
        "id": item.id,
        "brand_slug": item.brand_slug,
        "sku": item.sku,
        "name": item.name,
        "price_idr": item.price_idr,
        "image": item.image,
        "created_at": item.created_at,
    }


@router.get("")
async def list_wishlist(
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(WishlistItem)
        .where(WishlistItem.profile_id == profile.id)
        .order_by(WishlistItem.created_at.desc())
    )
    return {"data": [_serialize_item(i) for i in result.scalars().all()]}


@router.post("")
async def add_wishlist_item(
    body: WishlistItemIn,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict:
    # Idempotent on (profile, sku): return the existing row if already saved.
    result = await db.execute(
        select(WishlistItem).where(
            WishlistItem.profile_id == profile.id,
            WishlistItem.sku == body.sku,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        return {"data": _serialize_item(existing)}

    item = WishlistItem(profile_id=profile.id, **body.model_dump())
    db.add(item)
    await db.flush()
    return {"data": _serialize_item(item)}


@router.delete("/{item_id}", status_code=204)
async def delete_wishlist_item(
    item_id: str,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> Response:
    result = await db.execute(
        select(WishlistItem).where(
            WishlistItem.id == item_id,
            WishlistItem.profile_id == profile.id,
        )
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Wishlist item not found")
    await db.delete(item)
    await db.flush()
    return Response(status_code=204)
