"""Jubelio Shipment tracking webhook receiver.

Jubelio POSTs shipment status updates here (configure in Jubelio dashboard →
Setting → Developer → Webhook). It signs requests with the HMAC-SHA256 of
``(raw_body + secret)`` keyed by the shared secret from
``settings.jubelio_webhook_token``; the resulting hex digest lands in the
``x-jubelio-signature`` header.

Per contract v1.8 §7 (reference Node.js):
    signature = crypto.createHmac("sha256", secret)
                  .update(payload + secret).digest("hex")

We verify the **raw bytes** received (before JSON parsing) so the digests
match byte-for-byte, and compare with ``hmac.compare_digest`` to avoid
timing leaks.

Payload (contract v1.8 §7):
    {
      "event": "awb",
      "ref_no": "<our order.id>",
      "awb": "...",
      "shipment_id": "1",
      "latest_status": "DELIVERED",
      "courier": {...},
      "delivered_img_url": "...", "tracking_url": "...", "pod_url": "...",
      "tracking": {"date": "...", "status": "...", "status_detail": "..."}
    }

The status-mapping logic (DELIVERED → RECEIVED, RETURNED/CANCELED/SHIPMENT_ISSUE
→ DISPUTED, in-transit → persist only) lives in
``services.shipment_events.apply_shipment_event`` so the seller-driven
tracking-refresh endpoint can replay the same rule.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import get_db
from models.order import Order
from services.shipment_events import apply_shipment_event
from services.state_machine import lock_order_for_update

_LOG = logging.getLogger("beli_aman_bap.webhooks_jubelio")

router = APIRouter(prefix="/webhooks/jubelio", tags=["webhooks"])


def _verify_signature(body_bytes: bytes, signature: str | None, secret: str) -> None:
    """Verify the Jubelio webhook signature against ``secret``.

    Contract v1.8 §7: HMAC-SHA256(secret, body + secret).hexdigest().
    Comparison uses ``hmac.compare_digest`` (timing-safe). Raises
    ``HTTPException(401)`` on mismatch / missing header; ``HTTPException(503)``
    when the BAP has no secret configured (refuse rather than silently accept
    unsigned callbacks).
    """
    if not secret:
        raise HTTPException(503, "JUBELIO_WEBHOOK_TOKEN not configured")
    if not signature:
        raise HTTPException(401, "Missing Jubelio signature header")
    secret_bytes = secret.encode("utf-8")
    expected = hmac.new(
        secret_bytes, body_bytes + secret_bytes, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "Invalid Jubelio webhook signature")


@router.post("")
async def tracking_callback(
    request: Request,
    x_jubelio_signature: str | None = Header(default=None, alias="x-jubelio-signature"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Receive Jubelio shipment status updates."""
    # Read the raw body BEFORE JSON parsing — HMAC is over the exact bytes
    # Jubelio signed, and FastAPI's json() consumes the stream.
    body_bytes = await request.body()
    _verify_signature(body_bytes, x_jubelio_signature, settings.jubelio_webhook_token)
    body = await request.json()

    shipment_id = body.get("shipment_id")
    ref_no = body.get("ref_no")

    if not shipment_id and not ref_no:
        raise HTTPException(400, "Missing shipment_id and ref_no")

    _LOG.info(
        "Jubelio webhook: shipment_id=%s ref_no=%s status=%s",
        shipment_id, ref_no, body.get("latest_status"),
    )

    # Lookup. Prefer our jubelio_shipment_id; fall back to ref_no (= order.id).
    order: Order | None = None
    if shipment_id:
        order = (
            await db.execute(
                select(Order).where(Order.jubelio_shipment_id == str(shipment_id))
            )
        ).scalar_one_or_none()
    if order is None and ref_no:
        order = await lock_order_for_update(db, ref_no)
    if order is None:
        _LOG.error(
            "Jubelio tracking for unknown order: shipment_id=%s ref_no=%s",
            shipment_id, ref_no,
        )
        return {"ok": True, "matched": False}

    outcome = await apply_shipment_event(
        db=db, order=order, body=body, source="webhook",
    )

    state_value = (
        order.state.value if hasattr(order.state, "value") else order.state
    )
    resp: dict = {"ok": True, "order_id": order.id, "state": state_value}
    if outcome.get("transitioned_to") is not None:
        resp["transitioned_to"] = outcome["transitioned_to"]
    if "fulfillment_status" in outcome:
        resp["fulfillment_status"] = outcome["fulfillment_status"]
    if outcome.get("delivered_at") is not None:
        resp["auto_release_at"] = (
            order.auto_release_at.isoformat() if order.auto_release_at else None
        )
    return resp
