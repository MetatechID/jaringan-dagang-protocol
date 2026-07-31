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
import json
import logging
import random
import re
import time
from typing import Any, Iterable

import httpx

from config import settings

logger = logging.getLogger(__name__)

# Status codes we will retry on /shipments/create. The contract documents
# 500 as "Internal Server Error" and the 4xx class as validation failures.
# 502/503/504 are also retryable since they represent a transient
# upstream/downstream problem on Jubelio's side, not a malformed request.
TRANSIENT_5XX_STATUSES = {500, 502, 503, 504}
# Carrier-side timeout signatures. The actual failure we observed in
# production was Jubelio's own downstream timing out at 5s and being
# surfaced as a 500 with a body like
#   {"code":"ECONNABORTED","message":"timeout of 5000ms exceeded"}
# Matching either phrase (the literal ECONNABORTED code, or a "timeout of
# Nms exceeded" message) is the narrowest discriminator that catches the
# known failure mode without masking real outages.
_RETRYABLE_BODY_RE = re.compile(r"ECONNABORTED|timeout of \d+ms exceeded")


class ShippingItem(dict):
    """{name, weight, quantity, value} — same shape as the Biteship service."""


class ShippingError(Exception):
    """Raised when Jubelio returns a non-2xx response on a write call.

    ``code`` is a stable, machine-readable string for the dashboard to branch on
    (not an HTTP code): CARRIER_ERROR (default — generic failure),
    CARRIER_TRANSIENT (retryable upstream timeout, the seller should retry).

    ``retryable`` is a convenience for the dashboard's Retry button. Both
    fields default to the generic / non-retryable case, so existing raises
    across the file do not need to set them.

    ``__str__`` is intentionally left as ``args[0]`` (the message) so the
    two other consumers that do ``f"... {e}"`` — ``routers/orders.py:427``
    (cancel) and ``:499`` (tracking refresh) — keep their current wire
    shape unchanged.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "CARRIER_ERROR",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


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
    service_category_id: int | None = None,
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
        rates_path = (
            "/rates" if service_category_id is not None else "/rates/all"
        )
        if service_category_id is not None:
            payload["service_category_id"] = int(service_category_id)

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{_api_base()}{rates_path}", headers=headers, json=payload
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

# --- Booking -----------------------------------------------------------------


def _is_retryable_body(body: Any) -> bool:
    """True iff the response body matches the carrier-upstream-timeout
    signature (ECONNABORTED or 'timeout of Nms exceeded'). Used by the
    create_shipment retry loop to discriminate transient failures from
    real outages.
    """
    if isinstance(body, dict):
        haystack = json.dumps(body, default=str)
    elif isinstance(body, str):
        haystack = body
    else:
        return False
    return bool(_RETRYABLE_BODY_RE.search(haystack))


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
    # Bounded retry on transient carrier 5xx. Safety net: this entire block
    # runs BEFORE any BAP-side persistence — the row lock and fulfillment_awb
    # guard in book_shipment protect against double-booking from the caller's
    # side. Each iteration opens a fresh client because reusing a half-read
    # connection across the backoff is footgun territory.
    last_status: int | None = None
    last_body: Any = None
    resp = None
    for attempt in (1, 2):
        if attempt == 2:
            await asyncio.sleep(0.75 + random.uniform(0, 0.25))
            logger.warning(
                "Jubelio /shipments/create transient failure, attempt 1/2: status=%s body=%s",
                last_status, last_body,
            )
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{_api_base()}/shipments/create", headers=headers, json=payload
            )
        if resp.status_code < 400:
            if attempt == 2:
                logger.info("Jubelio /shipments/create recovered on attempt 2/2")
            break
        try:
            last_body = resp.json()
        except Exception:  # noqa: BLE001
            last_body = resp.text
        last_status = resp.status_code
        if not (resp.status_code in TRANSIENT_5XX_STATUSES and _is_retryable_body(last_body)):
            break

    if resp is not None and resp.status_code >= 400:
        logger.warning("Jubelio POST /shipments/create -> %s: %s", last_status, last_body)
        # Distinguish the retryable carrier-upstream-timeout case from a real
        # carrier rejection (4xx, or 5xx without the timeout signature). The
        # only failure mode we've seen in production — and the only one we
        # auto-retry — is Jubelio returning 500 with ECONNABORTED /
        # "timeout of Nms exceeded" in the body, which signals their own
        # downstream timed out before the request was handled. A real
        # outage (5xx with a different body) is not retryable and the
        # seller should pick a different courier.
        is_retryable = last_status in TRANSIENT_5XX_STATUSES and _is_retryable_body(last_body)
        if is_retryable:
            raise ShippingError(
                "The courier service is temporarily busy. Please try again in a moment.",
                code="CARRIER_TRANSIENT",
                retryable=True,
            ) from None
        raise ShippingError(f"Jubelio {last_status}: {last_body!r}")

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


async def get_shipment_by_awb(awb: str) -> dict[str, Any]:
    """Fetch full shipment detail (``GET /shipments/awb/{awb}``).

    Contract v1.8 §3.3. Returns the full document including the
    ``tracking[]`` event list, ``latest_status``, ETA, origin/destination,
    pricing. Used as the polling fallback when a webhook is missed
    (``POST /orders/{id}/tracking/refresh``).

    Raises ``ShippingError`` on non-2xx (404 when the AWB is unknown,
    401 when the token is wrong, 5xx on Jubelio-side trouble).
    """
    _require_creds()
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            f"{_api_base()}/shipments/awb/{awb}",
            headers=headers,
        )
    if resp.status_code >= 400:
        raise ShippingError(
            f"Jubelio detail {resp.status_code}: {resp.text!r}"
        )
    return resp.json()


# --- Reference data: service categories + region picker --------------------
#
# Contract v1.8 §4.1 (categories) and §5.1–§5.5 (provinces → areas).
# Thin wrappers used by the buyer-side region picker and the (future)
# seller-dashboard origin-address validator. No caching — each request
# is a direct HTTPS call to Jubelio.


async def get_service_categories() -> list[dict[str, Any]]:
    """List courier service categories (§4.1).

    Returned rows: ``{service_category_id, name}``. The six known values:
    REGULER (1), EKONOMI (2), NEXTDAY (3), INSTANT (4), SAMEDAY (5),
    CARGO (6).
    """
    _require_creds()
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            f"{_api_base()}/services/categories", headers=headers,
        )
    if resp.status_code >= 400:
        raise ShippingError(
            f"Jubelio categories {resp.status_code}: {resp.text!r}"
        )
    return resp.json()


async def get_regions(name: str | None = None) -> list[dict[str, Any]]:
    """List regions (§5.1). Optional ``?name=…`` substring filter
    (``province + city + district + area`` match on the row's ``name``).
    """
    _require_creds()
    headers = await _auth_headers()
    params: dict[str, str] | None = {"name": name} if name else None
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            f"{_api_base()}/regions",
            headers=headers,
            params=params,
        )
    if resp.status_code >= 400:
        raise ShippingError(
            f"Jubelio regions {resp.status_code}: {resp.text!r}"
        )
    return resp.json()


async def get_provinces() -> list[dict[str, Any]]:
    """List provinces (§5.2)."""
    _require_creds()
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            f"{_api_base()}/region/provinces", headers=headers,
        )
    if resp.status_code >= 400:
        raise ShippingError(
            f"Jubelio provinces {resp.status_code}: {resp.text!r}"
        )
    return resp.json()


async def get_cities(province_id: str) -> list[dict[str, Any]]:
    """List cities inside ``province_id`` (§5.3)."""
    _require_creds()
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            f"{_api_base()}/region/cities/{province_id}", headers=headers,
        )
    if resp.status_code >= 400:
        raise ShippingError(
            f"Jubelio cities {resp.status_code}: {resp.text!r}"
        )
    return resp.json()


async def get_districts(city_id: str) -> list[dict[str, Any]]:
    """List districts inside ``city_id`` (§5.4)."""
    _require_creds()
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            f"{_api_base()}/region/districts/{city_id}", headers=headers,
        )
    if resp.status_code >= 400:
        raise ShippingError(
            f"Jubelio districts {resp.status_code}: {resp.text!r}"
        )
    return resp.json()


async def get_areas(district_id: str) -> list[dict[str, Any]]:
    """List sub-district areas inside ``district_id`` (§5.5).

    The leaf-level rows: ``{area_id, district_id, name, zipcode}``. The
    ``area_id`` is what we hand to ``POST /shipments/create`` as the
    origin/destination ``area_id``.
    """
    _require_creds()
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            f"{_api_base()}/region/areas/{district_id}", headers=headers,
        )
    if resp.status_code >= 400:
        raise ShippingError(
            f"Jubelio areas {resp.status_code}: {resp.text!r}"
        )
    return resp.json()
