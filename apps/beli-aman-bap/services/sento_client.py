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
    # ponytail: Sento's create-v2 requires this. Comma-separated bank-code
    # string. 002=BRI, 008=Bank Mandiri, 009=BNI, 014=BCA (per sento-docs
    # Disbursement Bank Codes table). Add a Brand.sento_enabled_banks column
    # when per-brand multi-bank config is needed.
    list_enabled_banks: str | None = "002,008,009,014",
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

    Status API values: created / waiting_payment / expired /
    charge_in_progress / failed / complete / closed.

    Note: the Payment Link *callback* (POST) uses a different vocabulary:
    success / failed / processing. See ``routers/webhooks_sento._parse_status``
    for the normalization logic.
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


# ---------------------------------------------------------------------------
# Disbursement ("remit") — money-out. See sento-docs "Fund Disbursement".
# Buyer payments settle into the partner's single Sento balance; on escrow
# release we disburse from that balance to the brand/seller's bank account.
# Auth is the same x-api-key + x-username as the Payment Link endpoints.
# ---------------------------------------------------------------------------


async def create_disbursement(
    *,
    recipient_bank: str,
    recipient_account: str,
    amount_idr: int,
    partner_trx_id: str,
    note: str | None = None,
    email: str | None = None,
    additional_data: dict | None = None,
    api_key: str | None = None,
    username: str | None = None,
) -> dict:
    """Create a Sento disbursement (``POST /api/remit``).

    ``recipient_bank`` is Sento's NUMERIC bank code (e.g. "014" BCA, "008"
    Mandiri). ``recipient_account`` is digits only. ``partner_trx_id`` is our
    idempotency / correlation key — Sento echoes it back in every callback /
    status response and rejects duplicates (code 203). Minimum amount is
    Rp10.000. ``sender_info`` is intentionally omitted: Sento disburses from
    the partner's registered bank account (Settings > Bank Accounts), which is
    the platform's source account — not the recipient's.

    Note: Sento returns **HTTP 200 with a business ``status.code``** even for
    rejections (e.g. 203 duplicate, 300 failed), so this does NOT raise
    ``SentoError`` for those — the caller interprets ``status.code`` /
    ``trx_id``. Only a non-2xx HTTP response raises ``SentoError``. A successful
    create is code ``101`` ("Request is Processed", non-final) with a non-empty
    ``trx_id``; rejections return ``trx_id == ""``.

    Returns the raw Sento response (``status{code,message}``, ``amount``,
    ``recipient_bank``, ``recipient_account``, ``trx_id``, ``partner_trx_id``,
    ``timestamp``).
    """
    payload: dict[str, Any] = {
        "recipient_bank": recipient_bank,
        "recipient_account": recipient_account,
        "amount": int(amount_idr),
        "partner_trx_id": partner_trx_id,
    }
    if note:
        payload["note"] = note
    if email:
        payload["email"] = email
    if additional_data:
        payload["additional_data"] = additional_data

    return await _request(
        "POST",
        "/api/remit",
        api_key=api_key,
        username=username,
        json=payload,
    )


async def get_disbursement_status(
    *,
    partner_trx_id: str,
    send_callback: bool = False,
    api_key: str | None = None,
    username: str | None = None,
) -> dict:
    """Poll a disbursement's status by ``partner_trx_id``
    (``POST /api/remit-status``).

    Terminal codes: ``000`` success, ``300`` failed (also ``206`` balance-
    not-enough, ``225`` exceeds max). Non-final: ``101``/``102`` in progress,
    ``301`` pending, ``504``/``999`` unknown. ``204`` = the partner_tx_id was
    never created. Set ``send_callback=True`` to ask Sento to re-fire the
    disbursement callback (useful when the dashboard-configured callback URL
    didn't receive one). Sento suggests polling ~60s after create, and again
    after a create timeout, until a final status.
    """
    payload: dict[str, Any] = {"partner_trx_id": partner_trx_id}
    if send_callback:
        payload["send_callback"] = True
    return await _request(
        "POST",
        "/api/remit-status",
        api_key=api_key,
        username=username,
        json=payload,
    )


async def get_balance(*, api_key: str | None = None, username: str | None = None) -> dict:
    """Fetch the partner's Sento balance (``GET /api/balance``).

    Returns ``status``, ``balance``, ``availableBalance`` (the figure usable for
    disbursement = balance + available overdraft - pending), ``pendingBalance``,
    ``overdraftBalance``, ``overbookingBalance``, ``timestamp``. Optional
    pre-check before disbursing; not required for the happy path.
    """
    return await _request(
        "GET",
        "/api/balance",
        api_key=api_key,
        username=username,
    )
