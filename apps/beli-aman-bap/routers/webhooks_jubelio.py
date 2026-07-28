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

``latest_status`` ∈ WAITING | CONFIRMED_BY_COURIER | ON_THE_WAY_PICK_UP |
PICKED_UP | ON_DELIVERY | ON_HOLD | DELIVERED | RETURNED | CANCELED |
SHIPMENT_ISSUE.

Status mapping (mirrors webhooks_biteship):
  - DELIVERED → order.delivered_at, FULFILLING → RECEIVED, schedule D+3 release
  - RETURNED / CANCELED / SHIPMENT_ISSUE → DISPUTED, open IGM ticket
  - everything else → fulfillment_status update only, no state change
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import get_db
from models.order import Order, OrderState
from services.release_clock import compute_auto_release_at
from services.state_machine import (
    StateTransitionError,
    lock_order_for_update,
    transition,
)

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


_DELIVERED = {"DELIVERED"}
_PROBLEM = {"RETURNED", "CANCELED", "CANCELLED", "SHIPMENT_ISSUE"}


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
    status_raw = (body.get("latest_status") or "").upper()
    tracking = body.get("tracking") or {}
    event_time_raw = tracking.get("date") if isinstance(tracking, dict) else None

    if not shipment_id and not ref_no:
        raise HTTPException(400, "Missing shipment_id and ref_no")

    _LOG.info(
        "Jubelio tracking: shipment_id=%s ref_no=%s status=%s",
        shipment_id, ref_no, status_raw,
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

    event_time = _parse_iso(event_time_raw) or datetime.now(timezone.utc)
    order.fulfillment_status = status_raw.lower() or order.fulfillment_status
    order.fulfillment_last_event_at = event_time
    if body.get("awb") and not order.fulfillment_awb:
        order.fulfillment_awb = body.get("awb")
    if body.get("tracking_url") and not order.fulfillment_tracking_url:
        order.fulfillment_tracking_url = body.get("tracking_url")

    if status_raw in _DELIVERED:
        order.delivered_at = event_time
        order.auto_release_at = compute_auto_release_at(
            event_time, settings.auto_release_days,
        )
        if order.state == OrderState.FULFILLING:
            try:
                await transition(
                    db, order, OrderState.RECEIVED,
                    actor="system:jubelio_webhook",
                    payload={"event": "delivered", "shipment_id": shipment_id},
                )
            except StateTransitionError as e:
                _LOG.warning("FULFILLING→RECEIVED rejected for order %s: %s", order.id, e)
        return {
            "ok": True,
            "order_id": order.id,
            "state": order.state.value,
            "auto_release_at": order.auto_release_at.isoformat() if order.auto_release_at else None,
        }

    if status_raw in _PROBLEM:
        try:
            await transition(
                db, order, OrderState.DISPUTED,
                actor="system:jubelio_webhook",
                payload={"event": status_raw, "shipment_id": shipment_id},
            )
        except StateTransitionError as e:
            _LOG.warning("→DISPUTED rejected for order %s: %s", order.id, e)
        return {"ok": True, "order_id": order.id, "state": order.state.value}

    # In-transit events — just persist the status; no state change.
    return {
        "ok": True,
        "order_id": order.id,
        "state": order.state.value,
        "fulfillment_status": order.fulfillment_status,
    }


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
