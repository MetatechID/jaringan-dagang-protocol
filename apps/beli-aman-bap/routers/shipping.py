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
from services import carriers as carrier_service
from services import catalog as catalog_service
from services import jubelio as jubelio_service

router = APIRouter(prefix="/api/v1/shipping", tags=["shipping"])


class RateItem(BaseModel):
    sku: str
    qty: int = Field(ge=1)


class RateRequest(BaseModel):
    brand_slug: str
    destination_postal_code: str
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
    products = await catalog_service.list_products(body.brand_slug)
    brand = (
        await db.execute(select(Brand).where(Brand.slug == body.brand_slug))
    ).scalar_one_or_none()

    # Build a SKU → (name, weight_grams, price_idr) index that covers parent
    # SKUs and variant SKUs.
    sku_index: dict[str, dict[str, Any]] = {}
    for p in products:
        sku_index[p["sku"]] = {
            "name": p["name"],
            "weight": int(p.get("weight_grams") or 500),
            "value": int(p.get("price_idr") or 0),
        }
        for v in p.get("variants", []) or []:
            sku_index[v["sku"]] = {
                "name": f'{p["name"]} - {v.get("label", "")}',
                "weight": int(v.get("weight_grams") or 500),
                "value": int(v.get("price_idr") or p.get("price_idr") or 0),
            }

    line_items: list[dict[str, Any]] = []
    total_value = 0
    for item in body.items:
        info = sku_index.get(item.sku)
        if not info:
            raise HTTPException(400, f"Unknown SKU '{item.sku}' for brand {body.brand_slug}")
        line_items.append({
            "name": info["name"],
            "value": info["value"],
            "weight": info["weight"],
            "quantity": item.qty,
        })
        total_value += int(info["value"]) * int(item.qty)

    rates = await carrier_service.get_rates(
        brand=brand,
        destination_postal_code=body.destination_postal_code,
        items=line_items,
        total_value=total_value,
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
