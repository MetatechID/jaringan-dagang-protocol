"""Biteship shipping integration for Beli Aman BAP.

Calls Biteship's /v1/rates/couriers endpoint to fetch live rate quotes for the
buyer-side cart review. Falls back to a deterministic mock when no API key is
configured so the demo always works.

Why this lives in the BAP and not only in the seller BPP:
  - The shipping options screen is buyer-facing UX that runs before the order
    is committed. The buyer needs a quote synchronously from the BAP so the
    Beli Aman SDK can render courier options inside the checkout modal.
  - When the order is confirmed and pushed to the seller via seller_bridge,
    the chosen courier code is forwarded so the seller's shipping_service
    creates the actual shipment with the same selection.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterable

import httpx

logger = logging.getLogger(__name__)

BITESHIP_API_KEY = os.environ.get("BITESHIP_API_KEY")
BITESHIP_API_BASE = os.environ.get("BITESHIP_API_BASE", "https://api.biteship.com/v1")
DEFAULT_ORIGIN_POSTAL = os.environ.get("BITESHIP_ORIGIN_POSTAL", "10110")  # Tanah Abang, Jakarta Pusat
DEFAULT_COURIERS = "jne,jnt,sicepat,anteraja,tiki,ide,gojek,grab"


class ShippingItem(dict):
    """{name, weight, quantity, value} — matches Biteship items shape."""


def _mock_rates(items: Iterable[ShippingItem]) -> list[dict[str, Any]]:
    total_weight = sum(int(it.get("weight", 500)) * int(it.get("quantity", 1)) for it in items)
    multiplier = max(1.0, total_weight / 1000)
    base = [
        {"courier_code": "jne",      "courier_service_code": "REG",     "courier_service_name": "JNE Regular",     "duration": "2-3 hari",    "price":  12000},
        {"courier_code": "jne",      "courier_service_code": "YES",     "courier_service_name": "JNE YES",         "duration": "1 hari",      "price":  24000},
        {"courier_code": "jnt",      "courier_service_code": "EZ",      "courier_service_name": "J&T Express",     "duration": "1-2 hari",    "price":  11000},
        {"courier_code": "sicepat",  "courier_service_code": "REG",     "courier_service_name": "SiCepat REG",     "duration": "1-2 hari",    "price":  10000},
        {"courier_code": "anteraja", "courier_service_code": "REG",     "courier_service_name": "AnterAja Regular","duration": "1-2 hari",    "price":   9000},
        {"courier_code": "tiki",     "courier_service_code": "REG",     "courier_service_name": "TIKI Regular",    "duration": "2-3 hari",    "price":  13000},
        {"courier_code": "gojek",    "courier_service_code": "INSTANT", "courier_service_name": "Gojek Instant",   "duration": "Same-day",    "price":  28000},
        {"courier_code": "grab",     "courier_service_code": "INSTANT", "courier_service_name": "Grab Instant",    "duration": "Same-day",    "price":  29000},
    ]
    for r in base:
        r["price"] = int(round(r["price"] * multiplier / 1000) * 1000)
    return base


async def get_rates(
    *,
    destination_postal_code: str,
    items: list[ShippingItem],
    origin_postal_code: str | None = None,
    couriers: str = DEFAULT_COURIERS,
) -> list[dict[str, Any]]:
    """Fetch courier rate quotes for the buyer's destination + cart items."""
    if not BITESHIP_API_KEY:
        logger.info("BITESHIP_API_KEY not set; returning mock rates")
        return _mock_rates(items)

    payload = {
        "origin_postal_code": int(origin_postal_code or DEFAULT_ORIGIN_POSTAL),
        "destination_postal_code": int(destination_postal_code),
        "couriers": couriers,
        "items": items,
    }
    headers = {
        "Authorization": f"Bearer {BITESHIP_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{BITESHIP_API_BASE}/rates/couriers",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("Biteship rates call failed (%s); falling back to mock", e)
        return _mock_rates(items)

    out: list[dict[str, Any]] = []
    for rate in data.get("pricing", []):
        out.append({
            "courier_code": rate.get("courier_code"),
            "courier_service_code": rate.get("courier_service_code"),
            "courier_service_name": rate.get("courier_service_name"),
            "duration": rate.get("duration"),
            "price": rate.get("price"),
        })
    return out or _mock_rates(items)
