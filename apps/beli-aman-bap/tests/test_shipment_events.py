"""Tests for the shared shipment-event mapper.

The mapper is used by both the Jubelio webhook receiver and the new
tracking-refresh endpoint. It owns the rule from
``routers/webhooks_jubelio.py:tracking_callback``:

- DELIVERED → fill delivered_at + auto_release_at; FULFILLING → RECEIVED.
- RETURNED / CANCELED / SHIPMENT_ISSUE → DISPUTED.
- everything else → persist fulfillment_* fields only, no state change.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

import pytest

from services import shipment_events


# ---- helpers --------------------------------------------------------------


def _stub_order(
    *,
    state: object = None,
    awb: str | None = None,
    tracking_url: str | None = None,
) -> SimpleNamespace:
    """Order stub with the fields the mapper mutates + the ones
    ``_serialize_order`` reads (so the function-under-test doesn't crash)."""
    from models.order import OrderState

    if state is None:
        state = OrderState.FULFILLING
    return SimpleNamespace(
        id="order-1",
        state=state,
        fulfillment_status=None,
        fulfillment_awb=awb,
        fulfillment_tracking_url=tracking_url,
        fulfillment_last_event_at=None,
        jubelio_shipment_id="1281",
        delivered_at=None,
        auto_release_at=None,
    )


def _stub_transition(monkeypatch, captured: list):
    """Capture (to, actor, payload) tuples rather than mutating state."""
    async def _fake(db, order, to, *, actor, payload=None, **_):
        captured.append({"to": to, "actor": actor, "payload": payload})
        order.state = to
        return order

    monkeypatch.setattr(shipment_events, "transition", _fake)


# ---- delivereD -------------------------------------------------------------


class TestDelivered:
    @pytest.mark.asyncio
    async def test_delivered_fills_times_and_transitions(self, monkeypatch):
        order = _stub_order()
        body = {
            "shipment_id": 1281,
            "latest_status": "DELIVERED",
            "awb": "AWB-NEW",
            "tracking_url": "https://track.new",
            "tracking": {"date": "2025-04-01T12:00:00Z"},
        }
        captured: list = []
        _stub_transition(monkeypatch, captured)

        result = await shipment_events.apply_shipment_event(
            db=SimpleNamespace(),
            order=order,
            body=body,
            source="webhook",
        )

        # New state machine transition: FULFILLING → RECEIVED.
        assert len(captured) == 1
        from models.order import OrderState

        assert captured[0]["to"] == OrderState.RECEIVED
        assert captured[0]["actor"] == "system:jubelio_webhook"
        # Outcome the caller hands back to the webhook client.
        assert result == {
            "transitioned_to": OrderState.RECEIVED,
            "delivered_at": order.delivered_at,
        }
        # Fulfillment fields filled (already-empty order, so AWB + URL get set).
        assert order.fulfillment_awb == "AWB-NEW"
        assert order.fulfillment_tracking_url == "https://track.new"
        # delivered_at + auto_release_at stamped.
        assert order.delivered_at is not None
        assert order.auto_release_at is not None

    @pytest.mark.asyncio
    async def test_delivered_does_not_overwrite_existing_awb(self, monkeypatch):
        """Webhook says AWB-NEW but order already has one — don't overwrite,
        because the original was what the buyer was told to track. The
        newer value is informational; the earlier value is contractual."""
        order = _stub_order(awb="AWB-ORIG")
        body = {
            "shipment_id": 1281,
            "latest_status": "DELIVERED",
            "awb": "AWB-NEW",
            "tracking_url": "https://track.new",
            "tracking": {"date": "2025-04-01T12:00:00Z"},
        }
        _stub_transition(monkeypatch, [])

        await shipment_events.apply_shipment_event(
            db=SimpleNamespace(),
            order=order,
            body=body,
            source="refresh",
        )
        assert order.fulfillment_awb == "AWB-ORIG"


# ---- problem statuses ------------------------------------------------------


class TestProblemStatuses:
    @pytest.mark.parametrize("status", ["RETURNED", "CANCELED", "SHIPMENT_ISSUE"])
    @pytest.mark.asyncio
    async def test_problem_status_transitions_to_disputed(self, monkeypatch, status):
        order = _stub_order()
        body = {
            "shipment_id": 1281,
            "latest_status": status,
            "awb": "AWB-1",
            "tracking_url": "https://track",
            "tracking": {"date": "2025-04-01T12:00:00Z"},
        }
        captured: list = []
        _stub_transition(monkeypatch, captured)

        result = await shipment_events.apply_shipment_event(
            db=SimpleNamespace(),
            order=order,
            body=body,
            source="webhook",
        )

        from models.order import OrderState

        assert captured[0]["to"] == OrderState.DISPUTED
        assert result == {"transitioned_to": OrderState.DISPUTED}


# ---- in-transit / no transition -------------------------------------------


class TestInTransit:
    @pytest.mark.asyncio
    async def test_in_transit_persists_status_no_transition(self, monkeypatch):
        order = _stub_order()
        body = {
            "shipment_id": 1281,
            "latest_status": "ON_DELIVERY",
            "awb": "AWB-1",
            "tracking_url": "https://track",
            "tracking": {"date": "2025-04-01T12:00:00Z"},
        }
        captured: list = []
        _stub_transition(monkeypatch, captured)

        result = await shipment_events.apply_shipment_event(
            db=SimpleNamespace(),
            order=order,
            body=body,
            source="refresh",
        )

        assert captured == []
        assert result["transitioned_to"] is None
        assert result["fulfillment_status"] == "on_delivery"
        # fulfillment_status is lowercase (existing convention).
        assert order.fulfillment_status == "on_delivery"
        assert order.fulfillment_last_event_at is not None
