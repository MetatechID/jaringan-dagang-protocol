"""Buyer-facing shipping endpoints.

Returns courier options for a destination postal code + cart. Used by
StepCartReview in the Beli Aman SDK to let the buyer pick a courier before
authorising the escrow payment. Also serves the Jubelio region + service
category reference data (proxy pass-throughs) used by both the buyer-side
region picker and the (future) seller-dashboard origin-address validator.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.brand import Brand
from services import catalog as catalog_service
from services import jubelio as jubelio_service

router = APIRouter(prefix="/api/v1/shipping", tags=["shipping"])


class RateItem(BaseModel):
    sku: str
    qty: int = Field(ge=1)


class RateRequest(BaseModel):
    brand_slug: str
    destination_postal_code: str = Field(pattern=r"^[0-9]{5}$")
    items: list[RateItem]
    # Optional category filter (Jubelio only; Biteship returns all). Per
    # contract v1.8 §6.1 service_category_id: 1=REGULER, 2=EKONOMI,
    # 3=NEXTDAY, 4=INSTANT, 5=SAMEDAY, 6=CARGO.
    service_category_id: int | None = None


@router.post("/rates")
async def list_rates(
    body: RateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return courier options for the given destination + cart.

    Server-resolves each SKU against the brand catalog (including variants) so
    weight & value are trusted, not client-supplied. Dispatches to the brand's
    active carrier (Jubelio or Biteship).
    """
    brand = (
        await db.execute(select(Brand).where(Brand.slug == body.brand_slug))
    ).scalar_one_or_none()

    if brand is None:
        raise HTTPException(404, "Brand not found")
    products = await catalog_service.list_products(brand.slug)
    from services import shipping_quotes
    _, rates = await shipping_quotes.rates_for_cart(
        brand, body.destination_postal_code, body.items, products,
        service_category_id=body.service_category_id,
    )
    return {"data": rates}


# --- Jubelio reference data: courier categories + region picker ------------
#
# Pass-throughs to ``services.jubelio``. No router-layer auth: this is
# reference data used by both the buyer-side region picker and the
# (future) seller-dashboard origin-address validator. Server-side
# bundling ensures the SDK doesn't talk to Jubelio directly with the
# BAP's bearer tokens.


async def _proxy_to_jubelio(coro):
    """Call a Jubelio reference-data method; map ShippingError to 502."""
    try:
        return await coro
    except jubelio_service.ShippingError as e:
        raise HTTPException(502, str(e))


@router.get("/courier-categories")
async def list_courier_categories() -> Any:
    """List courier service categories (REGULER / INSTANT / etc.). §4.1."""
    return await _proxy_to_jubelio(jubelio_service.get_service_categories())


@router.get("/regions")
async def list_regions(name: str | None = Query(default=None)) -> Any:
    """List all regions; optional ``?name=…`` substring filter. §5.1."""
    return await _proxy_to_jubelio(jubelio_service.get_regions(name=name))


@router.get("/regions/provinces")
async def list_provinces() -> Any:
    """List provinces. §5.2."""
    return await _proxy_to_jubelio(jubelio_service.get_provinces())


@router.get("/regions/cities/{province_id}")
async def list_cities(province_id: str) -> Any:
    """List cities inside ``province_id``. §5.3."""
    return await _proxy_to_jubelio(jubelio_service.get_cities(province_id))


@router.get("/regions/districts/{city_id}")
async def list_districts(city_id: str) -> Any:
    """List districts inside ``city_id``. §5.4."""
    return await _proxy_to_jubelio(jubelio_service.get_districts(city_id))


@router.get("/regions/areas/{district_id}")
async def list_areas(district_id: str) -> Any:
    """List areas (villages) inside ``district_id``. §5.5. The leaf-level
    rows carry the ``area_id`` we hand to ``POST /shipments/create``."""
    return await _proxy_to_jubelio(jubelio_service.get_areas(district_id))
