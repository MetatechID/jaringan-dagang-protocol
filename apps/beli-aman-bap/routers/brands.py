"""Brand catalog — public endpoints (no auth required)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.brand import Brand
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
