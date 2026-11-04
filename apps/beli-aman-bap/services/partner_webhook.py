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
import uuid
from datetime import datetime, timezone

from services.webhook_audit import Capture, safe_endpoint, write_audit

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


async def send_payment_callback(*, url: str, secret: str, payload: dict, correlation: dict | None = None) -> bool:
    """POST ``payload`` to ``url`` with the HMAC signature header.

    Retries up to 3 attempts (5s / 15s backoff). Returns True on any 2xx.
    """
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    t = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "x-oito-signature": build_signature_header(secret, body, timestamp=t),
    }

    delivery_id = str(uuid.uuid4())
    for attempt in range(1, _RETRIES + 1):
        started = time.monotonic()
        incoming = Capture()
        status, response_headers = None, {}
        record = dict(started_at=datetime.now(timezone.utc), direction="outbound",
                      integration="partner", operation="payment.paid", method="POST",
                      endpoint=safe_endpoint(url), attempt=attempt, delivery_id=delivery_id,
                      external_order_id=payload.get("order_id"), dipay_reference=payload.get("reference"))
        record.update(correlation or {})
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
                    status = resp.status_code
                    response_headers = resp.headers
                    # Raw streaming avoids unbounded decompression. Stop at limit+1.
                    async for chunk in resp.aiter_raw(chunk_size=min(4096, incoming.limit + 1)):
                        incoming.feed(chunk)
                        if incoming.length > incoming.limit:
                            break
                    else:
                        incoming.consumed = True
                        incoming.complete = True
                finally:
                    await resp.aclose()
            record.update(status=status, outcome="response" if 200 <= status < 300 else "http_error",
                          error=None if incoming.complete else "response_capture_incomplete")
            if 200 <= status < 300:
                _LOG.info("partner callback ok status=%d order_id=%s", status, payload.get("order_id"))
                await _persist_attempt(record, body, incoming, response_headers, started)
                return True
            _LOG.warning(
                "partner callback failed reason=non_2xx status=%d order_id=%s attempt=%d",
                status, payload.get("order_id"), attempt,
            )
        except Exception as exc:  # noqa: BLE001 — payment is already committed
            reason = "destination_rejected" if isinstance(exc, CallbackURLRejected) else "transport_error"
            record.update(status=status, outcome="response" if status is not None and 200 <= status < 300 else "transport_error",
                          error="response_capture_incomplete" if status is not None else reason)
            if status is not None and 200 <= status < 300:
                await _persist_attempt(record, body, incoming, response_headers, started)
                return True
            _LOG.warning(
                "partner callback failed reason=%s order_id=%s attempt=%d",
                reason, payload.get("order_id"), attempt,
            )
        await _persist_attempt(record, body, incoming, response_headers, started)
        if attempt < _RETRIES:
            await asyncio.sleep(_RETRY_DELAYS[attempt - 1])
    return False


async def _persist_attempt(record, body, incoming, response_headers, started):
    outgoing = Capture()
    outgoing.feed(body)
    outgoing.complete = True
    record.update(completed_at=datetime.now(timezone.utc),
                  duration_ms=int((time.monotonic() - started) * 1000),
                  request=outgoing.snapshot({"content-type": "application/json"}),
                  response=incoming.snapshot(response_headers),
                  request_body_raw=outgoing.raw_body(), response_body_raw=incoming.raw_body(),
                  request_body_json=outgoing.parsed_json()[0], response_body_json=incoming.parsed_json()[0])
    await write_audit(record)


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
        correlation={"brand_id": str(brand.id), "partner_order_id": str(order.id),
                     "external_order_id": order.external_order_id, "dipay_reference": order.invoice_id},
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
            await session.commit()
        # Release the small DB pool before network I/O and independent audit writes.
        await notify_partner_paid(order, brand)
    except Exception:  # noqa: BLE001
        _LOG.warning("partner callback background failed reason=background_error order_id=%s", partner_order_id)
