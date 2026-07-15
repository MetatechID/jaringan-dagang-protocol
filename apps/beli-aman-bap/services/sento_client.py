"""Async Sento payment gateway HTTP client.

Sento's Payment Link REST API uses ``x-api-key`` + ``x-username`` headers
(master creds sourced from settings; per-Brand override sourced from
``Brand.sento_api_key`` / ``Brand.sento_username`` by the caller passing
them in). IP whitelisting is required at the Sento dashboard (sandbox or
prod, matching ``settings.sento_base_url``) — register the VM's outbound
IP before flipping a brand's ``payment_provider`` to ``"sento"``.

See https://api-docs.sento.id/docs-page/payment-link.

The Payment Link API has a flat resource shape: a single ``create_invoice``
returns ``url`` + ``payment_link_id`` + ``tx_ref_number`` (the buyer-facing
link and Sento-internal id, respectively); a single ``get_status`` returns
the current lifecycle state plus the Sento-internal ``tx_ref_number``. There's
no HMAC — we verify payment state via the status API (see
``routers/webhooks_sento.py``).

Callback URL is server-side dashboard config, not per-payload. Best-effort
forwarded in create but ultimately configured in the Sento dashboard.
QRIS-only scope for now.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from config import settings

_LOG = logging.getLogger("beli_aman_bap.sento")


class SentoError(Exception):
    """Raised when Sento returns a non-2xx response."""

    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Sento {status_code}: {body!r}")


def _headers(*, api_key: str | None, username: str | None) -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        h["x-api-key"] = api_key
    if username:
        h["x-username"] = username
    return h


def _base_url() -> str:
    # ponytail: env-driven so prod/sandbox switch is a .env flip, no code
    # edit. Default sandbox (https://api-demo.sento.id); prod overrides
    # SENTO_BASE_URL=https://partner.sento.id.
    return settings.sento_base_url.rstrip("/")


async def _request(
    method: str,
    path: str,
    *,
    api_key: str | None = None,
    username: str | None = None,
    json: dict | None = None,
    params: dict | None = None,
) -> dict:
    """Thin httpx wrapper. Raises SentoError on non-2xx."""
    if not api_key and not settings.sento_api_key:
        raise SentoError(0, "SENTO_API_KEY not configured (env or Brand.sento_api_key)")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(
            method,
            f"{_base_url()}{path}",
            headers=_headers(
                api_key=api_key or settings.sento_api_key,
                username=username or settings.sento_default_username,
            ),
            json=json,
            params=params,
        )
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = resp.text
        _LOG.warning("Sento %s %s -> %s: %s", method, path, resp.status_code, body)
        raise SentoError(resp.status_code, body)
    return resp.json()


async def create_invoice(
    *,
    partner_tx_id: str,
    amount_idr: int,
    sender_name: str,
    description: str | None = None,
    notes: str | None = None,
    email: str | None = None,
    phone_number: str | None = None,
    is_open: bool = False,
    include_admin_fee: bool = False,
    list_disabled_payment_methods: str | None = None,
    # ponytail: Sento's create-v2 requires this. Default "002" (BI code for
    # BCA) is the only code in the docs sample; also constrains the
    # transaction to QRIS in staging. Add a Brand.sento_enabled_banks column
    # when multi-bank is needed.
    list_enabled_banks: str | None = "002",
    expiration: str | None = None,  # "yyyy-MM-dd HH:mm:ss"
    va_display_name: str | None = None,
    callback_url: str | None = None,
    api_key: str | None = None,
    username: str | None = None,
) -> dict:
    """Mint a single Sento payment link.

    Returns the raw Sento response (``status`` + ``url`` + ``payment_link_id``
    + ``tx_ref_number`` shaped). Funds settle into the brand's Sento
    balance.
    """
    payload: dict[str, Any] = {
        "partner_tx_id": partner_tx_id,
        "amount": int(amount_idr),
        "sender_name": sender_name,
        "is_open": is_open,
        "include_admin_fee": include_admin_fee,
    }
    if description:
        payload["description"] = description
    if notes:
        payload["notes"] = notes
    if email:
        payload["email"] = email
    if phone_number:
        payload["phone_number"] = phone_number
    if list_disabled_payment_methods:
        payload["list_disabled_payment_methods"] = list_disabled_payment_methods
    if list_enabled_banks is not None:
        payload["list_enabled_banks"] = list_enabled_banks
    if expiration:
        payload["expiration"] = expiration
    if va_display_name:
        payload["va_display_name"] = va_display_name
    if callback_url:
        # Best-effort forward; Sento's docs do not document callback_url in
        # create-v2 (it's dashboard-config) but forwarding is harmless.
        payload["callback_url"] = callback_url

    return await _request(
        "POST",
        "/api/payment-checkout/create-v2",
        api_key=api_key,
        username=username,
        json=payload,
    )


async def get_status(
    *,
    partner_tx_id: str,
    api_key: str | None = None,
    username: str | None = None,
) -> dict:
    """Fetch current payment-link state from Sento.

    Status values per docs: created / waiting_payment / expired /
    charge_in_progress / failed / complete / closed.
    """
    return await _request(
        "GET",
        "/api/payment-checkout/status",
        api_key=api_key,
        username=username,
        params={"partner_tx_id": partner_tx_id},
    )


async def cancel_invoice(
    *, partner_tx_id: str, api_key: str | None = None, username: str | None = None
) -> dict:
    # ponytail: Sento's DELETE /api/payment-checkout/{id} only works while
    # the link is still active and no payment method has been selected.
    # Wire when we need a buyer-side cancel button; v1 expires by TTL.
    raise NotImplementedError("Sento cancel_invoice — wire when needed")


async def create_refund(*, partner_tx_id: str, amount_idr: int) -> dict:
    # ponytail: Sento's refund path isn't in the Payment Link section of
    # the docs we sampled. Wire when BPP side requires Sento-native
    # refunds — until then, escalate via Sento dashboard like Xendit.
    raise NotImplementedError("Sento refunds — wire when needed")
