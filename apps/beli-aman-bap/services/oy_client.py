"""Async OY Indonesia HTTP client.

OY's REST API uses ``x-api-key`` + ``x-oy-username`` headers (master creds
sourced from settings; per-Brand override sourced from Brand.oy_* columns
by the caller passing them in). IP whitelisting is required at the OY
dashboard — make sure the VM's outbound IP is registered before flipping a
brand's ``payment_provider`` to ``oy``.

See https://api-docs.oyindonesia.com/.

The wire shape differs across OY products (VA Aggregator, E-Wallet
Aggregator, QRIS Aggregator). We expose a single ``create_invoice`` that
takes the ``payment_methods`` list directly — OY's own request schema —
and let the caller pick the rail mix. Webhook verification uses HMAC
SHA-256 over the raw body with the per-Brand ``oy_callback_secret``; see
routers/webhooks_oy.py for the receiver.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from config import settings

_LOG = logging.getLogger("beli_aman_bap.oy")


class OYError(Exception):
    """Raised when OY returns a non-2xx response."""

    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"OY {status_code}: {body!r}")


def _headers(*, api_key: str | None, username: str | None) -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        h["x-api-key"] = api_key
    if username:
        h["x-oy-username"] = username
    return h


_BASE_URL = "https://api.oyindonesia.com"


async def _request(
    method: str,
    path: str,
    *,
    api_key: str | None = None,
    username: str | None = None,
    json: dict | None = None,
) -> dict:
    """Thin httpx wrapper. Raises OYError on non-2xx."""
    if not api_key and not settings.oy_api_key:
        raise OYError(0, "OY_API_KEY not configured (env or Brand.oy_api_key)")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(
            method,
            f"{_BASE_URL}{path}",
            headers=_headers(
                api_key=api_key or settings.oy_api_key,
                username=username or settings.oy_default_username,
            ),
            json=json,
        )
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = resp.text
        _LOG.warning("OY %s %s -> %s: %s", method, path, resp.status_code, body)
        raise OYError(resp.status_code, body)
    return resp.json()


async def create_invoice(
    *,
    external_id: str,
    amount_idr: int,
    description: str,
    payer_email: str | None = None,
    payer_name: str | None = None,
    callback_url: str | None = None,
    success_redirect_url: str | None = None,
    failure_redirect_url: str | None = None,
    # OY accepts a list of payment methods per invoice. Each entry shape:
    #   {"name": "VA", "code": "BCA"} / {"name": "EWALLET", "code": "OVO"}
    #   / {"name": "QRIS"}
    # We accept the list as-is so the caller picks rails without us
    # hardcoding them.
    payment_methods: list[dict[str, Any]] | None = None,
    api_key: str | None = None,
    username: str | None = None,
) -> dict:
    """Mint a single OY transaction that exposes the buyer's chosen rail(s).

    Returns the raw OY response (``trx_id`` + ``checkout_url`` /
    ``payment_url`` shaped). Funds settle into the brand's OY balance.
    """
    payload: dict[str, Any] = {
        "external_id": external_id,
        "amount": int(amount_idr),
        "description": description,
        "currency": "IDR",
    }
    if payer_email:
        payload["payer_email"] = payer_email
    if payer_name:
        payload["customer_name"] = payer_name
    if callback_url:
        payload["callback_url"] = callback_url
    if success_redirect_url:
        payload["success_redirect_url"] = success_redirect_url
    if failure_redirect_url:
        payload["failure_redirect_url"] = failure_redirect_url
    if payment_methods:
        payload["payment_methods"] = payment_methods
    # ponytail: default to QRIS-only if caller omits — flip to a richer
    # mix (VA + E-Wallet) once per-brand preferences land.
    else:
        payload["payment_methods"] = [{"name": "QRIS"}]

    return await _request(
        "POST",
        "/payment/create",
        api_key=api_key,
        username=username,
        json=payload,
    )


async def get_invoice(*, invoice_id: str, api_key: str | None = None, username: str | None = None) -> dict:
    # ponytail: defer richer querying — OY's per-product status endpoints
    # differ in path. Until a vendor-agnostic status view is needed, look
    # up via the brand's per-product sub-api.
    raise NotImplementedError("OY per-product get_invoice — wire when needed")


async def create_refund(*, external_id: str, invoice_id: str, amount_idr: int) -> dict:
    # ponytail: not wired in v1 — refunds ride Xendit (different rail than
    # the buyer's payment, per the OY plan note). Add when BPP side requires
    # OY-native refunds.
    raise NotImplementedError("OY refunds — wire when needed")
