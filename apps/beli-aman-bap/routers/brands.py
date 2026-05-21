"""Brand catalog + per-brand payouts/fulfillment admin endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_profile
from models.brand import Brand
from models.profile import BeliAmanProfile
from models.store_membership import StoreMembership
from services import catalog as catalog_service

router = APIRouter(prefix="/api/v1/brands", tags=["brands"])


@router.get("")
async def list_brands(db: AsyncSession = Depends(get_db)) -> list[dict]:
    result = await db.execute(select(Brand).order_by(Brand.slug))
    return [
        {
            "id": b.id,
            "slug": b.slug,
            "name": b.name,
            "bpp_id": b.bpp_id,
        }
        for b in result.scalars().all()
    ]


@router.get("/{slug}")
async def get_brand(slug: str, db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(select(Brand).where(Brand.slug == slug))
    brand = result.scalar_one_or_none()
    if not brand:
        raise HTTPException(404, f"Brand '{slug}' not found")
    return {
        "id": brand.id,
        "slug": brand.slug,
        "name": brand.name,
        "bpp_id": brand.bpp_id,
        "default_warehouse_address": brand.default_warehouse_address,
    }


@router.get("/{slug}/products")
async def list_products(slug: str) -> list[dict]:
    try:
        return await catalog_service.list_products(slug)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@router.get("/{slug}/products/{product_slug}")
async def get_product(slug: str, product_slug: str) -> dict:
    try:
        product = await catalog_service.get_product(slug, product_slug)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    if not product:
        raise HTTPException(404, f"Product '{product_slug}' not found in brand '{slug}'")
    return product


# ----- Payouts & Fulfillment (vibe-admin) -----


class PayoutsIn(BaseModel):
    xendit_sub_account_id: str | None = None
    xendit_disbursement_bank_code: str | None = None
    xendit_disbursement_bank_account: str | None = None
    xendit_disbursement_holder_name: str | None = None
    biteship_origin_address: dict | None = None
    biteship_default_courier: str | None = None


def _payouts_view(brand: Brand) -> dict:
    return {
        "slug": brand.slug,
        "xendit_sub_account_id": brand.xendit_sub_account_id,
        "xendit_disbursement_bank_code": brand.xendit_disbursement_bank_code,
        # Mask the account number — only last 4 digits go back to the client.
        "xendit_disbursement_bank_account_masked": (
            "•••• " + brand.xendit_disbursement_bank_account[-4:]
            if brand.xendit_disbursement_bank_account
            and len(brand.xendit_disbursement_bank_account) > 4
            else brand.xendit_disbursement_bank_account
        ),
        "xendit_disbursement_holder_name": brand.xendit_disbursement_holder_name,
        "biteship_origin_address": brand.biteship_origin_address,
        "biteship_default_courier": brand.biteship_default_courier,
    }


async def _resolve_brand_for_edit(
    slug: str, profile: BeliAmanProfile, db: AsyncSession
) -> Brand:
    brand = (
        await db.execute(select(Brand).where(Brand.slug == slug))
    ).scalar_one_or_none()
    if brand is None:
        raise HTTPException(404, f"Brand '{slug}' not found")
    if not profile.is_super_admin:
        membership = (
            await db.execute(
                select(StoreMembership)
                .where(StoreMembership.profile_id == profile.id)
                .where(StoreMembership.store_slug == slug)
            )
        ).scalar_one_or_none()
        if membership is None:
            raise HTTPException(403, f"Not a member of store '{slug}'")
    return brand


@router.get("/{slug}/payouts")
async def get_payouts(
    slug: str,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict:
    brand = await _resolve_brand_for_edit(slug, profile, db)
    return _payouts_view(brand)


@router.put("/{slug}/payouts")
async def put_payouts(
    slug: str,
    body: PayoutsIn,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict:
    brand = await _resolve_brand_for_edit(slug, profile, db)

    def _normalize(v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    if body.xendit_sub_account_id is not None:
        brand.xendit_sub_account_id = _normalize(body.xendit_sub_account_id)
    if body.xendit_disbursement_bank_code is not None:
        brand.xendit_disbursement_bank_code = _normalize(body.xendit_disbursement_bank_code)
    if body.xendit_disbursement_bank_account is not None:
        brand.xendit_disbursement_bank_account = _normalize(body.xendit_disbursement_bank_account)
    if body.xendit_disbursement_holder_name is not None:
        brand.xendit_disbursement_holder_name = _normalize(body.xendit_disbursement_holder_name)
    if body.biteship_origin_address is not None:
        brand.biteship_origin_address = body.biteship_origin_address or None
    if body.biteship_default_courier is not None:
        brand.biteship_default_courier = _normalize(body.biteship_default_courier)

    await db.flush()
    return _payouts_view(brand)
