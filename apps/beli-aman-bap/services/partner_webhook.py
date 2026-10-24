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
from sqlalchemy import select

from services.partner_callback_url import CallbackTransport, CallbackURLRejected, callback_destination

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
            callback_destination(url)
            # Use the transport directly: no redirect/proxy machinery and no
            # HTTPX client INFO log (which includes credential-bearing queries).
            async with asyncio.timeout(_TIMEOUT_SECONDS), CallbackTransport() as transport:
                request = httpx.Request("POST", url, content=body, headers=headers)
                request.extensions["timeout"] = {
                    phase: _TIMEOUT_SECONDS for phase in ("connect", "read", "write", "pool")
                }
                resp = await transport.handle_async_request(request)
                try:
                    # Headers/status only: never buffer arbitrary response bodies.
                    status = resp.status_code
                finally:
                    await resp.aclose()
            if 200 <= status < 300:
                _LOG.info("partner callback ok status=%d order_id=%s", status, payload.get("order_id"))
                return True
            _LOG.warning(
                "partner callback failed reason=non_2xx status=%d order_id=%s attempt=%d",
                status, payload.get("order_id"), attempt,
            )
        except Exception as exc:  # noqa: BLE001 — payment is already committed
            reason = "destination_rejected" if isinstance(exc, CallbackURLRejected) else "transport_error"
            _LOG.warning(
                "partner callback failed reason=%s order_id=%s attempt=%d",
                reason, payload.get("order_id"), attempt,
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


async def notify_partner_paid_bg(partner_order_id: str) -> None:
    """Background task — opens its own DB session, POSTs the signed payment
    callback to the partner. Decoupled from the request lifecycle on purpose:
    the webhook handler ACKs 200 (Xendit retries on slow ACKs) as soon as the
    paid flip is committed. Schedulers/cron should re-drive unpaid partner
    orders whose callback failed — the money is already taken."""
    try:
        from database import async_session
        from models.brand import Brand
        from services import partner_orders

        async with async_session() as session:
            order = await partner_orders.get_partner_order(session, partner_order_id)
            if order is None:
                return
            brand = (
                await session.execute(
                    select(Brand).where(Brand.id == order.brand_id)
                )
            ).scalar_one_or_none()
            if brand is None:
                return
            await notify_partner_paid(order, brand)
            await session.commit()
    except Exception:  # noqa: BLE001
        _LOG.warning("partner callback background failed reason=background_error order_id=%s", partner_order_id)
