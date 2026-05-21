"""Biteship shipping integration for Beli Aman BAP.

Two surfaces:
- ``get_rates`` — buyer-facing quote lookup for the SDK / cart UI.
- ``create_shipment`` — seller-facing booking after escrow is held.

The same Biteship API key drives both. Production fail-loud: no
``BITESHIP_API_KEY`` in env raises rather than silently mocking, so
checkout can't end up with a "shipping booked" UI that quietly served
fake rates. Non-prod (development/test) keeps the mock fallback so
local demos keep working without keys.

Why this lives in the BAP and not only in the seller BPP:
  - Rate quotes are buyer-facing UX that runs before the order
    is committed. The buyer needs synchronous rates from the BAP.
  - Booking happens after the BAP transitions the order to FULFILLING,
    and the AWB / tracking URL needs to be persisted on the BAP's Order
    row anyway (it shows up in the SDK timeline).
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

import httpx

from config import settings

logger = logging.getLogger(__name__)


DEFAULT_COURIERS = "jne,jnt,sicepat,anteraja,tiki,ide,gojek,grab"


class ShippingItem(dict):
    """{name, weight, quantity, value} — matches Biteship items shape."""


class ShippingError(Exception):
    """Raised when Biteship returns a non-2xx response on a write call."""


def _api_base() -> str:
    return settings.biteship_api_base.rstrip("/")


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.biteship_api_key}",
        "Content-Type": "application/json",
    }


def _require_key() -> None:
    if not settings.biteship_api_key:
        if settings.environment in ("test", "development"):
            return
        raise ShippingError(
            "BITESHIP_API_KEY is required in production environment"
        )


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
    """Fetch courier rate quotes for the buyer's destination + cart items.

    Falls back to mock rates in development/test when no API key is set.
    In production, no API key raises; a Biteship 5xx or network error
    still falls back to mock so a transient outage doesn't block checkout
    (the booking call will revalidate).
    """
    _require_key()
    if not settings.biteship_api_key:
        logger.info("BITESHIP_API_KEY not set (non-prod); returning mock rates")
        return _mock_rates(items)

    payload = {
        "destination_postal_code": int(destination_postal_code),
        "couriers": couriers,
        "items": items,
    }
    if origin_postal_code:
        payload["origin_postal_code"] = int(origin_postal_code)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{_api_base()}/v1/rates/couriers",
                headers=_headers(),
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:  # noqa: BLE001
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


async def create_shipment(
    *,
    origin: dict[str, Any],
    destination: dict[str, Any],
    items: list[dict[str, Any]],
    courier_code: str,
    courier_service_code: str,
    reference_id: str,
) -> dict[str, Any]:
    """Book a Biteship shipment.

    See https://biteship.com/en/docs/api/orders for full schema.

    Returns the raw Biteship order response; the caller persists
    ``id`` (Biteship's internal order id), ``courier.waybill_id`` (the
    AWB), and ``courier.tracking_url``.
    """
    _require_key()
    if not settings.biteship_api_key:
        # Non-prod stub so local demos don't 500
        return {
            "id": f"mock-{reference_id}",
            "courier": {
                "waybill_id": f"MOCKAWB{reference_id[:10].upper()}",
                "tracking_url": f"https://biteship.com/track/MOCKAWB{reference_id[:10].upper()}",
                "company": courier_code,
                "type": courier_service_code,
            },
            "price": 0,
            "status": "confirmed",
        }

    payload = {
        "shipper_contact_name": origin.get("contact_name", ""),
        "shipper_contact_phone": origin.get("contact_phone", ""),
        "shipper_contact_email": origin.get("contact_email", ""),
        "shipper_organization": origin.get("organization", ""),
        "origin_contact_name": origin.get("contact_name", ""),
        "origin_contact_phone": origin.get("contact_phone", ""),
        "origin_address": origin.get("address", ""),
        "origin_note": origin.get("note", ""),
        "origin_postal_code": int(origin["postal_code"]),
        "origin_coordinate": (
            {"latitude": origin["latitude"], "longitude": origin["longitude"]}
            if origin.get("latitude") and origin.get("longitude")
            else None
        ),
        "destination_contact_name": destination.get("recipient_name", ""),
        "destination_contact_phone": destination.get("phone_e164")
            or destination.get("phone", ""),
        "destination_contact_email": destination.get("email", ""),
        "destination_address": _join_address(destination),
        "destination_postal_code": int(destination["postal_code"]),
        "destination_note": destination.get("note", ""),
        "courier_company": courier_code,
        "courier_type": courier_service_code,
        "delivery_type": "now",
        "items": items,
        "reference_id": reference_id,
    }
    # Per-shipment tracking webhook. Biteship's dashboard doesn't expose a
    # global webhook URL config — webhooks are bound per order via this
    # field. Token in query param is how we auth (Biteship doesn't sign).
    if settings.biteship_webhook_token:
        payload["webhook_url"] = (
            f"{settings.xendit_callback_base_url.rstrip('/')}"
            f"/webhooks/biteship/tracking?token={settings.biteship_webhook_token}"
        )
    # Drop the coordinate key entirely if origin didn't have it
    if payload.get("origin_coordinate") is None:
        payload.pop("origin_coordinate")

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{_api_base()}/v1/orders",
            headers=_headers(),
            json=payload,
        )

    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = resp.text
        logger.warning("Biteship POST /v1/orders -> %s: %s", resp.status_code, body)
        raise ShippingError(f"Biteship {resp.status_code}: {body!r}")

    return resp.json()


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
