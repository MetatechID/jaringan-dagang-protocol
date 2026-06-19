"""Carrier dispatch — one seam over Biteship + Jubelio.

The routers call into here instead of a specific carrier so adding/removing a
carrier is a change in one place. Selection order:

  1. ``brand.jubelio_enabled`` → "jubelio"
  2. else ``settings.default_carrier``
  3. fallback "biteship"

Both back-ends normalize to the same rate-row shape so the SDK/UI renders them
identically:

    {carrier, courier_code, courier_service_code, courier_name,
     courier_service_name, duration, price}

For Jubelio, ``courier_code`` / ``courier_service_code`` carry the numeric
``courier_id`` / ``courier_service_id`` (as strings) needed to book later.
"""
from __future__ import annotations

import logging
from typing import Any

from config import settings
from models.brand import Brand
from models.order import Order
from services import jubelio as jubelio_service
from services import shipping as biteship_service

logger = logging.getLogger(__name__)

BITESHIP = "biteship"
JUBELIO = "jubelio"


class ShippingError(Exception):
    """Carrier-agnostic booking failure (wraps the underlying carrier error)."""


def active_carrier(brand: Brand | None) -> str:
    if brand is not None and getattr(brand, "jubelio_enabled", False):
        return JUBELIO
    chosen = (settings.default_carrier or BITESHIP).lower()
    return chosen if chosen in (BITESHIP, JUBELIO) else BITESHIP


# --- Rates -------------------------------------------------------------------

async def get_rates(
    *,
    brand: Brand | None,
    destination_postal_code: str,
    items: list[dict[str, Any]],
    total_value: int | None = None,
) -> list[dict[str, Any]]:
    """Buyer-facing rate quotes from the brand's active carrier.

    ``items`` are the resolved cart lines: {name, value, weight, quantity}.
    """
    carrier = active_carrier(brand)
    if carrier == JUBELIO:
        origin = (getattr(brand, "jubelio_origin_address", None) or {}) if brand else {}
        origin_zip = origin.get("zipcode") or _biteship_origin_zip(brand)
        if not origin_zip:
            logger.warning(
                "Jubelio rates: brand has no origin zipcode; returning empty"
            )
            return []
        rates = await jubelio_service.get_rates(
            origin_zipcode=str(origin_zip),
            destination_zipcode=str(destination_postal_code),
            items=[jubelio_service.ShippingItem(i) for i in items],
            origin_area_id=origin.get("area_id"),
            origin_coordinate=origin.get("coordinate"),
            total_value=total_value,
        )
        return rates

    # Biteship default — origin is implied by the Biteship account.
    bs = await biteship_service.get_rates(
        destination_postal_code=str(destination_postal_code),
        items=[biteship_service.ShippingItem(i) for i in items],
    )
    for r in bs:
        r.setdefault("carrier", BITESHIP)
    return bs


def _biteship_origin_zip(brand: Brand | None) -> str | None:
    addr = getattr(brand, "biteship_origin_address", None) if brand else None
    if isinstance(addr, dict):
        return addr.get("postal_code") or addr.get("zipcode")
    return None


# --- Booking -----------------------------------------------------------------

class BookingResult(dict):
    """{carrier, external_id, awb, tracking_url, price}."""


async def book(
    *,
    brand: Brand,
    order: Order,
    courier_code: str,
    courier_service_code: str,
) -> BookingResult:
    """Book a shipment with the brand's active carrier.

    Returns a normalized result the caller persists onto the order:
    {carrier, external_id, awb, tracking_url, price}. ``external_id`` goes to
    the carrier-specific id column (biteship_order_id / jubelio_shipment_id).
    """
    carrier = active_carrier(brand)
    dest = order.shipping_address or {}

    if carrier == JUBELIO:
        origin = getattr(brand, "jubelio_origin_address", None) or {}
        if not origin.get("zipcode"):
            raise ShippingError(
                f"Brand '{brand.slug}' has no jubelio_origin_address. Set it "
                "in vibe-admin → Payouts & Fulfillment."
            )
        try:
            cid = int(courier_code)
            csid = int(courier_service_code)
        except (TypeError, ValueError):
            raise ShippingError(
                "Jubelio booking needs numeric courier_id/courier_service_id "
                f"(got {courier_code!r}/{courier_service_code!r})"
            )
        try:
            res = await jubelio_service.create_shipment(
                origin=origin,
                destination={
                    "name": dest.get("recipient_name"),
                    "phone": dest.get("phone_e164") or dest.get("phone"),
                    "address": _join_address(dest),
                    "zipcode": dest.get("postal_code"),
                    "area_id": dest.get("area_id"),
                },
                items=_jubelio_items(order),
                courier_id=cid,
                courier_service_id=csid,
                reference_id=order.id,
            )
        except jubelio_service.ShippingError as e:
            raise ShippingError(str(e))
        return BookingResult(
            carrier=JUBELIO,
            external_id=res.get("shipment_id"),
            awb=res.get("awb"),
            tracking_url=res.get("tracking_url"),
            price=res.get("price"),
        )

    # Biteship default
    if not getattr(brand, "biteship_origin_address", None):
        raise ShippingError(
            f"Brand '{brand.slug}' has no biteship_origin_address. Set it "
            "in vibe-admin → Payouts & Fulfillment."
        )
    try:
        res = await biteship_service.create_shipment(
            origin=brand.biteship_origin_address,
            destination=dest,
            items=_biteship_items(order),
            courier_code=courier_code,
            courier_service_code=courier_service_code,
            reference_id=order.id,
        )
    except biteship_service.ShippingError as e:
        raise ShippingError(str(e))
    courier = res.get("courier") or {}
    return BookingResult(
        carrier=BITESHIP,
        external_id=res.get("id"),
        awb=courier.get("waybill_id"),
        tracking_url=courier.get("tracking_url"),
        price=res.get("price"),
    )


def _biteship_items(order: Order) -> list[dict[str, Any]]:
    return [
        {
            "name": (i.get("name") or i.get("sku") or "item")[:255],
            "description": (i.get("name") or "")[:255],
            "value": int(i.get("unit_price_idr") or 0),
            "weight": int(i.get("weight_grams") or 500),
            "quantity": int(i.get("qty") or 1),
        }
        for i in (order.items or [])
    ]


def _jubelio_items(order: Order) -> list[dict[str, Any]]:
    return [
        {
            "item_code": i.get("sku") or "",
            "item_name": (i.get("name") or i.get("sku") or "item")[:255],
            "category": "-",
            "quantity": int(i.get("qty") or 1),
            "value": int(i.get("unit_price_idr") or 0),
            "weight": int(i.get("weight_grams") or 500),
            "length": int(i.get("length_cm") or 10),
            "width": int(i.get("width_cm") or 10),
            "height": int(i.get("height_cm") or 10),
        }
        for i in (order.items or [])
    ]


def _join_address(addr: dict[str, Any]) -> str:
    parts = [
        addr.get("line1"),
        addr.get("line2"),
        addr.get("kelurahan"),
        addr.get("kecamatan"),
        addr.get("kota"),
        addr.get("provinsi"),
    ]
    return ", ".join(p for p in parts if p)
