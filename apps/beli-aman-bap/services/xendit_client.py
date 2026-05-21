"""Async Xendit HTTP client.

All money endpoints route through the seller's XenPlatform sub-account by
emitting a ``for-user-id`` header. Funds custody stays with Xendit (the
licensed PJP) on a per-seller balance — never on our master balance.

See ``/Users/gogo/.claude/projects/-Users-gogo-Code-jaringan-dagang/memory/
feedback_indonesia_fund_custody.md`` for the OJK/BI reasoning.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from config import settings

_LOG = logging.getLogger("beli_aman_bap.xendit")


class XenditError(Exception):
    """Raised when Xendit returns a non-2xx response."""

    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Xendit {status_code}: {body!r}")


def _auth_header() -> str:
    """Xendit uses HTTP Basic with the secret key as username, empty password."""
    raw = f"{settings.xendit_secret_key}:".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _headers(*, for_user_id: str | None, idempotency_key: str | None = None) -> dict[str, str]:
    h: dict[str, str] = {
        "Authorization": _auth_header(),
        "Content-Type": "application/json",
    }
    if for_user_id:
        h["for-user-id"] = for_user_id
    if idempotency_key:
        # Xendit uses different idempotency header names across products. Set
        # both — Xendit ignores headers it doesn't recognize.
        h["X-IDEMPOTENCY-KEY"] = idempotency_key
        h["Idempotency-key"] = idempotency_key
    return h


_BASE_URL = "https://api.xendit.co"


async def _request(
    method: str,
    path: str,
    *,
    for_user_id: str | None,
    json: dict | None = None,
    idempotency_key: str | None = None,
) -> dict:
    if not settings.xendit_secret_key:
        raise XenditError(0, "XENDIT_SECRET_KEY not configured")

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(
            method,
            f"{_BASE_URL}{path}",
            headers=_headers(for_user_id=for_user_id, idempotency_key=idempotency_key),
            json=json,
        )

    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = resp.text
        _LOG.warning("Xendit %s %s -> %s: %s", method, path, resp.status_code, body)
        raise XenditError(resp.status_code, body)

    return resp.json()


async def create_invoice(
    *,
    for_user_id: str,
    external_id: str,
    amount_idr: int,
    description: str,
    customer_email: str | None = None,
    customer_name: str | None = None,
    success_redirect_url: str | None = None,
    failure_redirect_url: str | None = None,
    duration_seconds: int | None = None,
    items: list[dict] | None = None,
) -> dict:
    """Create a Xendit hosted invoice. Funds settle into ``for_user_id``'s
    sub-account balance. Returns the raw Xendit response (notably
    ``id`` and ``invoice_url``)."""
    payload: dict[str, Any] = {
        "external_id": external_id,
        "amount": amount_idr,
        "description": description,
        "currency": "IDR",
    }
    if customer_email:
        payload["payer_email"] = customer_email
    if customer_name:
        payload["customer"] = {"given_names": customer_name}
    if success_redirect_url:
        payload["success_redirect_url"] = success_redirect_url
    if failure_redirect_url:
        payload["failure_redirect_url"] = failure_redirect_url
    if duration_seconds:
        payload["invoice_duration"] = duration_seconds
    if items:
        payload["items"] = items

    return await _request(
        "POST",
        "/v2/invoices",
        for_user_id=for_user_id,
        json=payload,
        idempotency_key=external_id,
    )


async def get_invoice(*, for_user_id: str, invoice_id: str) -> dict:
    return await _request(
        "GET",
        f"/v2/invoices/{invoice_id}",
        for_user_id=for_user_id,
    )


async def expire_invoice(*, for_user_id: str, invoice_id: str) -> dict:
    return await _request(
        "POST",
        f"/invoices/{invoice_id}/expire!",
        for_user_id=for_user_id,
    )


async def create_disbursement(
    *,
    for_user_id: str,
    external_id: str,
    amount_idr: int,
    bank_code: str,
    account_holder_name: str,
    account_number: str,
    description: str,
) -> dict:
    """Disburse from ``for_user_id``'s sub-account Xendit balance to the
    seller's bank account. This is the release leg of escrow."""
    payload = {
        "external_id": external_id,
        "amount": amount_idr,
        "bank_code": bank_code,
        "account_holder_name": account_holder_name,
        "account_number": account_number,
        "description": description,
    }
    return await _request(
        "POST",
        "/disbursements",
        for_user_id=for_user_id,
        json=payload,
        idempotency_key=external_id,
    )


async def get_disbursement(*, for_user_id: str, disbursement_id: str) -> dict:
    return await _request(
        "GET",
        f"/disbursements/{disbursement_id}",
        for_user_id=for_user_id,
    )


async def create_refund(
    *,
    for_user_id: str,
    invoice_id: str,
    amount_idr: int,
    reason: str = "REQUESTED_BY_CUSTOMER",
) -> dict:
    """Refund a Xendit invoice. Funds come out of the same sub-account
    balance the original payment settled into."""
    payload = {
        "invoice_id": invoice_id,
        "amount": amount_idr,
        "reason": reason,
    }
    return await _request(
        "POST",
        "/refunds",
        for_user_id=for_user_id,
        json=payload,
        idempotency_key=f"refund-{invoice_id}",
    )
