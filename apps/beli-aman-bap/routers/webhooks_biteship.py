"""Biteship tracking webhook receiver.

Biteship POSTs shipment status updates here. See
https://biteship.com/en/docs/api/webhooks. We auth by static token
configured in the Biteship dashboard → Integrations → Webhooks.

Status mapping:
  - allocating / picking_up / on_pickup / dropping_off / on_delivery
    → fulfillment_status update only, no state change
  - delivered → order.delivered_at, FULFILLING → RECEIVED, schedule D+3 release
  - cancelled / returned / rejected / problem → DISPUTED, open IGM ticket
"""

from __future__ import annotations

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

_LOG = logging.getLogger("beli_aman_bap.webhooks_biteship")

router = APIRouter(prefix="/webhooks/biteship", tags=["webhooks"])


def _verify(authorization: str | None, token_query: str | None) -> None:
    """Biteship's per-shipment ``webhook_url`` doesn't sign requests, so we
    embed the token in the URL itself as ``?token=…``. Header-based auth is
    accepted as a fallback if a future Biteship release does set Authorization.
    """
    expected = settings.biteship_webhook_token
    if not expected:
        raise HTTPException(503, "BITESHIP_WEBHOOK_TOKEN not configured")
    candidates = {
        authorization,
        authorization.removeprefix("Bearer ") if authorization else None,
        token_query,
    }
    if expected in candidates:
        return
    raise HTTPException(401, "Invalid Biteship webhook token")


_IN_TRANSIT = {
    "confirmed", "allocated", "allocating", "assigned",
    "picking_up", "picked", "picked_up", "on_pickup",
    "dropping_off", "on_delivery", "courier_not_found",
}
_DELIVERED = {"delivered"}
_PROBLEM = {"cancelled", "canceled", "returned", "rejected", "problem"}


@router.post("/tracking")
async def tracking_callback(
    request: Request,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Receive Biteship shipment status updates."""
    _verify(authorization, request.query_params.get("token"))
    body = await request.json()

    # Biteship payload shape varies slightly between v1/v2; tolerate both.
    biteship_order_id = (
        body.get("order_id")
        or (body.get("order") or {}).get("id")
        or body.get("id")
    )
    status_raw = (
        body.get("status")
        or (body.get("courier") or {}).get("status")
        or ""
    ).lower()
    reference_id = body.get("reference_id")
    event_time_raw = body.get("updated_at") or body.get("created_at")

    if not biteship_order_id and not reference_id:
        raise HTTPException(400, "Missing order_id and reference_id")

    _LOG.info(
        "Biteship tracking: biteship_order_id=%s reference_id=%s status=%s",
        biteship_order_id, reference_id, status_raw,
    )

    # Lookup. Prefer biteship_order_id; fall back to our own order id
    # (Biteship echoes it back in reference_id).
    order: Order | None = None
    if biteship_order_id:
        order = (
            await db.execute(
                select(Order).where(Order.biteship_order_id == biteship_order_id)
            )
        ).scalar_one_or_none()
    if order is None and reference_id:
        order = await lock_order_for_update(db, reference_id)
    if order is None:
        # No matching order — 200 OK to stop Biteship retrying, log loudly.
        _LOG.error(
            "Biteship tracking event for unknown order: biteship_order_id=%s "
            "reference_id=%s", biteship_order_id, reference_id,
        )
        return {"ok": True, "matched": False}

    event_time = _parse_iso(event_time_raw) or datetime.now(timezone.utc)
    order.fulfillment_status = status_raw or order.fulfillment_status
    order.fulfillment_last_event_at = event_time

    if status_raw in _DELIVERED:
        order.delivered_at = event_time
        order.auto_release_at = compute_auto_release_at(
            event_time, settings.auto_release_days,
        )
        if order.state == OrderState.FULFILLING:
            try:
                await transition(
                    db, order, OrderState.RECEIVED,
                    actor="system:biteship_webhook",
                    payload={"event": "delivered", "biteship_order_id": biteship_order_id},
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
        # Don't auto-refund here — leave the move to IGM dispute flow so
        # ops sees the ticket and can confirm before money moves back.
        try:
            await transition(
                db, order, OrderState.DISPUTED,
                actor="system:biteship_webhook",
                payload={"event": status_raw, "biteship_order_id": biteship_order_id},
            )
        except StateTransitionError as e:
            _LOG.warning("→DISPUTED rejected for order %s: %s", order.id, e)
        return {"ok": True, "order_id": order.id, "state": order.state.value}

    # In-transit events — just persist the status; no state change.
    return {
        "ok": True,
        "order_id": order.id,
        "state": order.state.value,
        "fulfillment_status": status_raw,
    }


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Biteship uses ISO-8601 with 'Z' suffix.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
