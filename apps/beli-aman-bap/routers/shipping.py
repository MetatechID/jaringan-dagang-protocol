"""Buyer-facing shipping rate endpoint.

Returns courier options for a destination postal code + cart. Used by
StepCartReview in the Beli Aman SDK to let the buyer pick a courier before
authorising the escrow payment.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services import catalog as catalog_service
from services import shipping as shipping_service

router = APIRouter(prefix="/api/v1/shipping", tags=["shipping"])


class RateItem(BaseModel):
    sku: str
    qty: int = Field(ge=1)


class RateRequest(BaseModel):
    brand_slug: str
    destination_postal_code: str
    items: list[RateItem]


@router.post("/rates")
async def list_rates(body: RateRequest) -> dict[str, Any]:
    """Return courier options for the given destination + cart.

    Server-resolves each SKU against the brand catalog (including variants) so
    weight & value are trusted, not client-supplied.
    """
    products = await catalog_service.list_products(body.brand_slug)

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

    bs_items: list[shipping_service.ShippingItem] = []
    for item in body.items:
        info = sku_index.get(item.sku)
        if not info:
            raise HTTPException(400, f"Unknown SKU '{item.sku}' for brand {body.brand_slug}")
        bs_items.append(shipping_service.ShippingItem({
            "name": info["name"],
            "value": info["value"],
            "weight": info["weight"],
            "quantity": item.qty,
        }))

    rates = await shipping_service.get_rates(
        destination_postal_code=body.destination_postal_code,
        items=bs_items,
    )
    return {"data": rates}
