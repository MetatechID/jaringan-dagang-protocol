"""Signed payment callback to external partners (e.g. consumerland.id).

When a partner order is paid we POST a JSON callback to the partner's
``partner_callback_url`` with an HMAC-SHA256 signature header:

    x-oito-signature: t=<unix_ts>,v1=<hex>

where ``v1 = HMAC-SHA256(partner_callback_secret, "<t>.<raw_body>")``.
The partner recomputes the digest over the raw body bytes and compares.

Failure is non-fatal (mirrors ``services/seller_bridge.py``): the money
is already taken — the partner can also poll ``GET /api/v1/partner/orders/{id}``
or flip the order in their admin.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time

import httpx

_LOG = logging.getLogger("beli_aman_bap.partner_webhook")

_TIMEOUT_SECONDS = 8.0
_RETRIES = 3
_RETRY_DELAYS = (5.0, 15.0)


def _sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode("utf-8") + body, hashlib.sha256)
    return mac.hexdigest()


def build_callback_payload(order) -> dict:
    """Canonical payload consumerland's ``handleOitoWebhook`` expects.
    ``order`` is a PartnerOrder with its Brand loaded (``order.brand``)."""
    return {
        "event": "payment.paid",
        "order_id": order.external_order_id,
        "status": "paid",
        "amount": order.amount_idr,
        "reference": order.invoice_id or "",
        "paid_at": order.paid_at.isoformat() if order.paid_at else None,
    }


def build_signature_header(secret: str, body: bytes, *, timestamp: int | None = None) -> str:
    """Build the ``x-oito-signature`` header value. Exposed for tests."""
    t = int(time.time()) if timestamp is None else timestamp
    return f"t={t},v1={_sign(secret, t, body)}"


async def send_payment_callback(*, url: str, secret: str, payload: dict) -> bool:
    """POST ``payload`` to ``url`` with the HMAC signature header.

    Retries up to 3 attempts (5s / 15s backoff). Returns True on any 2xx.
    """
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    t = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "x-oito-signature": build_signature_header(secret, body, timestamp=t),
    }

    for attempt in range(1, _RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                resp = await client.post(url, content=body, headers=headers)
            if 200 <= resp.status_code < 300:
                _LOG.info(
                    "partner callback ok %s -> %s (order_id=%s)",
                    url, resp.status_code, payload.get("order_id"),
                )
                return True
            _LOG.warning(
                "partner callback non-2xx %s -> %s %s (order_id=%s, attempt %d)",
                url, resp.status_code, resp.text[:200],
                payload.get("order_id"), attempt,
            )
        except Exception as e:  # noqa: BLE001
            _LOG.warning(
                "partner callback exception %s (order_id=%s, attempt %d): %s",
                url, payload.get("order_id"), attempt, e,
            )
        if attempt < _RETRIES:
            await asyncio.sleep(_RETRY_DELAYS[attempt - 1])
    return False


async def notify_partner_paid(order, brand) -> bool:
    """Fire-and-forget wrapper used from webhook handlers. Signature timing
    uses the payload built at call time (paid_at already stamped)."""
    if not brand.partner_callback_url or not brand.partner_callback_secret:
        _LOG.warning(
            "partner callback skipped — brand %s has no callback URL/secret",
            brand.id,
        )
        return False
    payload = build_callback_payload(order)
    return await send_payment_callback(
        url=brand.partner_callback_url,
        secret=brand.partner_callback_secret,
        payload=payload,
    )
