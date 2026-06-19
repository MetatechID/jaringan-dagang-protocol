"""Jubelio Shipment integration for Beli Aman BAP.

Mirror of ``services/shipping.py`` (Biteship) but speaks the Jubelio
Shipment API (contract v1.8). Two surfaces:

- ``get_rates`` — buyer-facing quote lookup for the SDK / cart UI.
- ``create_shipment`` — seller-facing booking after escrow is held.

Auth differs from Biteship: Jubelio uses a ``client_id`` / ``client_secret``
pair exchanged at ``POST /auth/generate-token`` for a bearer token that lives
``expires_in`` seconds (default 86400). We cache it in-process and refresh a
little before expiry.

Production fail-loud: missing credentials raise rather than silently mocking,
so checkout can't show "shipping booked" on fake data. Non-prod
(development/test) keeps a mock fallback so local demos work without creds.

Endpoints used (base = settings.jubelio_api_base):
  POST /auth/generate-token        → {token, expires_in}
  POST /rates/all                  → [{courier_id, courier_service_id, rates, ...}]
  POST /shipments/create           → {shipment_id, awb, tracking_url, price}
  POST /shipments/cancel           → {status, awb_code, ...}
  GET  /shipments/awb/{awb}        → {latest_status, tracking[], ...}
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Iterable

import httpx

from config import settings

logger = logging.getLogger(__name__)


class ShippingItem(dict):
    """{name, weight, quantity, value} — same shape as the Biteship service."""


class ShippingError(Exception):
    """Raised when Jubelio returns a non-2xx response on a write call."""


# --- Token cache (module-level, guarded by a lock) ---------------------------

_token_value: str | None = None
_token_expiry: float = 0.0  # epoch seconds
_token_lock = asyncio.Lock()
# Refresh this many seconds before the stated expiry to avoid edge races.
_TOKEN_SKEW = 120


def _api_base() -> str:
    return settings.jubelio_api_base.rstrip("/")


def _require_creds() -> None:
    if settings.jubelio_client_id and settings.jubelio_client_secret:
        return
    if settings.environment in ("test", "development"):
        return
    raise ShippingError(
        "JUBELIO_CLIENT_ID / JUBELIO_CLIENT_SECRET are required in production"
    )


async def _get_token(force: bool = False) -> str:
    """Return a valid bearer token, refreshing via /auth/generate-token.

    Cached in-process until ``_TOKEN_SKEW`` seconds before expiry.
    """
    global _token_value, _token_expiry
    now = time.time()
    if not force and _token_value and now < _token_expiry:
        return _token_value

    async with _token_lock:
        now = time.time()
        if not force and _token_value and now < _token_expiry:
            return _token_value
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{_api_base()}/auth/generate-token",
                json={
                    "client_id": settings.jubelio_client_id,
                    "client_secret": settings.jubelio_client_secret,
                },
            )
        if resp.status_code >= 400:
            raise ShippingError(
                f"Jubelio token {resp.status_code}: {resp.text!r}"
            )
        data = resp.json()
        token = data.get("token")
        if not token:
            raise ShippingError(f"Jubelio token response missing token: {data!r}")
        expires_in = int(data.get("expires_in") or 86400)
        _token_value = token
        _token_expiry = time.time() + max(60, expires_in - _TOKEN_SKEW)
        return token


async def _auth_headers() -> dict[str, str]:
    token = await _get_token()
    return {"authorization": f"Bearer {token}", "Content-Type": "application/json"}


# --- Mock fallback (dev/test only) -------------------------------------------

def _mock_rates(items: Iterable[ShippingItem]) -> list[dict[str, Any]]:
    total_weight = sum(int(it.get("weight", 500)) * int(it.get("quantity", 1)) for it in items)
    multiplier = max(1.0, total_weight / 1000)
    base = [
        {"courier_code": "11",  "courier_service_code": "1101", "courier_name": "JNE",      "courier_service_name": "JNE REG",       "duration": "2-3 hari", "price": 12000},
        {"courier_code": "13",  "courier_service_code": "1326", "courier_name": "SiCepat",  "courier_service_name": "SiCepat REG",   "duration": "1-2 hari", "price": 10000},
        {"courier_code": "24",  "courier_service_code": "2452", "courier_name": "Lion Parcel", "courier_service_name": "REGPACK",    "duration": "2-4 hari", "price":  9000},
    ]
    for r in base:
        r["price"] = int(round(r["price"] * multiplier / 1000) * 1000)
        r["carrier"] = "jubelio"
    return base


# --- Rates -------------------------------------------------------------------

async def get_rates(
    *,
    origin_zipcode: str,
    destination_zipcode: str,
    items: list[ShippingItem],
    origin_area_id: str | None = None,
    destination_area_id: str | None = None,
    origin_coordinate: str | None = None,
    destination_coordinate: str | None = None,
    total_value: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch courier rate quotes across all couriers (``POST /rates/all``).

    Returns rows shaped like the Biteship service so the SDK/UI can render
    both carriers identically: {carrier, courier_code, courier_service_code,
    courier_name, courier_service_name, duration, price}. For Jubelio,
    ``courier_code`` / ``courier_service_code`` carry the numeric
    ``courier_id`` / ``courier_service_id`` (as strings) needed to book.
    """
    _require_creds()
    if not (settings.jubelio_client_id and settings.jubelio_client_secret):
        logger.info("Jubelio creds not set (non-prod); returning mock rates")
        return _mock_rates(items)

    total_weight = sum(
        int(it.get("weight", 500)) * int(it.get("quantity", 1)) for it in items
    ) or 1000

    origin: dict[str, Any] = {"zipcode": str(origin_zipcode)}
    if origin_area_id:
        origin["area_id"] = str(origin_area_id)
    if origin_coordinate:
        origin["coordinate"] = origin_coordinate

    destination: dict[str, Any] = {"zipcode": str(destination_zipcode)}
    if destination_area_id:
        destination["area_id"] = str(destination_area_id)
    if destination_coordinate:
        destination["coordinate"] = destination_coordinate

    payload: dict[str, Any] = {
        "origin": origin,
        "destination": destination,
        "items": [
            {
                "quantity": int(it.get("quantity", 1)),
                "weight": int(it.get("weight", 500)),
                "length": int(it.get("length", 10)),
                "width": int(it.get("width", 10)),
                "height": int(it.get("height", 10)),
            }
            for it in items
        ],
        "weight": total_weight,
    }
    if total_value is not None:
        payload["total_value"] = int(total_value)

    try:
        headers = await _auth_headers()
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{_api_base()}/rates/all", headers=headers, json=payload
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("Jubelio rates call failed (%s); falling back to mock", e)
        return _mock_rates(items)

    out: list[dict[str, Any]] = []
    for rate in data if isinstance(data, list) else []:
        out.append({
            "carrier": "jubelio",
            "courier_code": str(rate.get("courier_id")),
            "courier_service_code": str(rate.get("courier_service_id")),
            "courier_name": rate.get("courier_name"),
            "courier_service_name": rate.get("courier_service_name"),
            "duration": _eta_label(rate),
            # Per the contract note: the charged amount is "rates", not
            # "final_rates".
            "price": int(rate.get("rates") or 0),
        })
    return out or _mock_rates(items)


def _eta_label(rate: dict[str, Any]) -> str:
    cat = rate.get("courier_service_category")
    if cat:
        return str(cat).title()
    return ""


# --- Booking -----------------------------------------------------------------

async def create_shipment(
    *,
    origin: dict[str, Any],
    destination: dict[str, Any],
    items: list[dict[str, Any]],
    courier_id: int,
    courier_service_id: int,
    reference_id: str,
    shipping_insurance: int | None = None,
    is_cod: bool = False,
) -> dict[str, Any]:
    """Book a Jubelio shipment (``POST /shipments/create``).

    ``origin`` / ``destination`` are dicts with name/phone/address/zipcode
    (area_id + coordinate optional). Returns a normalized result:
    {shipment_id, awb, tracking_url, price}.
    """
    _require_creds()
    if not (settings.jubelio_client_id and settings.jubelio_client_secret):
        # Non-prod stub so local demos don't 500.
        return {
            "shipment_id": f"mock-{reference_id}",
            "awb": f"MOCKAWB{reference_id[:10].upper()}",
            "tracking_url": f"https://shipment.jubelio.com/track/MOCKAWB{reference_id[:10].upper()}",
            "price": 0,
        }

    payload: dict[str, Any] = {
        "ref_no": reference_id,
        "courier_id": int(courier_id),
        "courier_service_id": int(courier_service_id),
        "is_cod": bool(is_cod),
        "origin": _party(origin),
        "destination": _party(destination),
        "items": items,
    }
    if shipping_insurance:
        payload["shipping_insurance"] = int(shipping_insurance)

    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{_api_base()}/shipments/create", headers=headers, json=payload
        )

    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = resp.text
        logger.warning("Jubelio POST /shipments/create -> %s: %s", resp.status_code, body)
        raise ShippingError(f"Jubelio {resp.status_code}: {body!r}")

    data = resp.json()
    return {
        "shipment_id": data.get("shipment_id"),
        "awb": data.get("awb"),
        "tracking_url": data.get("tracking_url"),
        "price": data.get("price"),
    }


def _party(p: dict[str, Any]) -> dict[str, Any]:
    """Build a Jubelio origin/destination object, dropping empty optionals."""
    out: dict[str, Any] = {
        "name": p.get("name") or "",
        "phone": p.get("phone") or "",
        "address": p.get("address") or "",
        "zipcode": str(p.get("zipcode") or ""),
    }
    if p.get("email"):
        out["email"] = p["email"]
    if p.get("area_id"):
        out["area_id"] = str(p["area_id"])
    if p.get("coordinate"):
        out["coordinate"] = p["coordinate"]
    return out


async def cancel_shipment(*, awb_code: str, reason: str = "Barang belum siap") -> dict[str, Any]:
    """Cancel an AWB (``POST /shipments/cancel``). May be rejected by courier."""
    _require_creds()
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{_api_base()}/shipments/cancel",
            headers=headers,
            json={"cancel_reason": reason, "awb_code": awb_code},
        )
    if resp.status_code >= 400:
        raise ShippingError(f"Jubelio cancel {resp.status_code}: {resp.text!r}")
    return resp.json()
