"""Shared mapping for incoming Jubelio shipment events.

Used by ``routers/webhooks_jubelio.py:tracking_callback`` (webhook delivery)
and the new ``POST /orders/{id}/tracking/refresh`` poll endpoint (PR3).
Owning the rule in one place keeps the webhook handler small and the
seller-driven refresh consistent with the producer side.

Rule (mirrors the previous inline branch in ``tracking_callback``):
  - DELIVERED → set delivered_at + auto_release_at; FULFILLING → RECEIVED.
  - RETURNED / CANCELED / SHIPMENT_ISSUE → DISPUTED.
  - everything else → persist ``fulfillment_*`` fields only, no transition.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.order import Order, OrderState
from services.release_clock import compute_auto_release_at
from services.state_machine import transition

_LOG = logging.getLogger("beli_aman_bap.shipment_events")

_DELIVERED = {"DELIVERED"}
_PROBLEM = {"RETURNED", "CANCELED", "CANCELLED", "SHIPMENT_ISSUE"}


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


async def apply_shipment_event(
    *,
    db: AsyncSession,
    order: Order,
    body: Mapping[str, Any],
    source: str,
) -> dict[str, Any]:
    """Apply a Jubelio shipment event payload to ``order``.

    ``source`` drives the state-machine ``actor`` string (e.g.
    ``"system:jubelio_webhook"`` for webhook deliveries,
    ``"seller:refresh:<order_id>"`` for the manual refresh endpoint).
    Returns a small dict that the caller hands back to its client — the
    shape is the same regardless of source so the refresh endpoint can
    return the same JSON the webhook does.
    """
    shipment_id = body.get("shipment_id")
    status_raw = (body.get("latest_status") or "").upper()
    tracking = body.get("tracking") or {}
    event_time = _parse_iso(
        tracking.get("date") if isinstance(tracking, dict) else None
    ) or datetime.now(timezone.utc)

    # Persist tracking fields. Don't overwrite AWB / tracking_url if the
    # order already has them — the first value is what the buyer was told
    # to track.
    order.fulfillment_status = status_raw.lower() or order.fulfillment_status
    order.fulfillment_last_event_at = event_time
    if body.get("awb") and not order.fulfillment_awb:
        order.fulfillment_awb = body["awb"]
    if body.get("tracking_url") and not order.fulfillment_tracking_url:
        order.fulfillment_tracking_url = body["tracking_url"]

    if status_raw in _DELIVERED:
        order.delivered_at = event_time
        order.auto_release_at = compute_auto_release_at(
            event_time, settings.auto_release_days,
        )
        if order.state == OrderState.FULFILLING:
            try:
                await transition(
                    db, order, OrderState.RECEIVED,
                    actor=f"system:jubelio_{source}",
                    payload={"event": "delivered", "shipment_id": shipment_id},
                )
            except Exception as e:  # noqa: BLE001
                _LOG.warning(
                    "FULFILLING→RECEIVED rejected for order %s: %s", order.id, e
                )
        return {
            "transitioned_to": order.state.value
            if hasattr(order.state, "value")
            else order.state,
            "delivered_at": order.delivered_at,
        }

    if status_raw in _PROBLEM:
        try:
            await transition(
                db, order, OrderState.DISPUTED,
                actor=f"system:jubelio_{source}",
                payload={"event": status_raw, "shipment_id": shipment_id},
            )
        except Exception as e:  # noqa: BLE001
            _LOG.warning("→DISPUTED rejected for order %s: %s", order.id, e)
        return {
            "transitioned_to": order.state.value
            if hasattr(order.state, "value")
            else order.state,
        }

    # In-transit events — just persist the status; no state change.
    return {
        "transitioned_to": None,
        "fulfillment_status": order.fulfillment_status,
    }
