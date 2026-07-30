"""Tests for ``POST /api/v1/orders/{order_id}/tracking/refresh``.

The endpoint lets the seller (admin token) forcibly pull the latest
tracking state from Jubelio when a webhook was missed. Mirrors the
webhook's status-mapping via the shared ``apply_shipment_event`` helper.
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
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.orders import router as orders_router  # noqa: E402
from services import jubelio as jubelio_service  # noqa: E402
from services import shipment_events  # noqa: E402
from tests._jubelio_fakes import FakeSession  # noqa: E402


def _stub_order(
    *,
    state: object = None,
    awb: str = "AWB-1",
    tracking_url: str | None = None,
) -> SimpleNamespace:
    from models.order import OrderState

    if state is None:
        state = OrderState.FULFILLING
    return SimpleNamespace(
        id="order-1",
        brand_id="brand-id",
        state=state,
        carrier="jubelio",
        fulfillment_awb=awb,
        fulfillment_tracking_url=tracking_url,
        jubelio_shipment_id="1281",
        biteship_order_id=None,
        shipped_at=datetime.now(timezone.utc),
        items=[],
        fulfillment_status=None,
        fulfillment_last_event_at=None,
        delivered_at=None,
        auto_release_at=None,
    )


def _install_db(app, *results):
    from database import get_db as real_get_db

    session = FakeSession(list(results))

    async def _stub_get_db():
        yield session

    app.dependency_overrides[real_get_db] = _stub_get_db
    return session


def _override_admin_token(app, *, allow: bool = True):
    from deps import require_admin_token as real

    async def _stubbed():
        if allow:
            return "admin"
        from fastapi import HTTPException

        raise HTTPException(401, "admin token required")

    app.dependency_overrides[real] = _stubbed


@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(orders_router)
    _override_admin_token(a, allow=True)
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def db(app):
    return _install_db(app)


def _stub_lock_returning(value):
    async def _fake_lock(_db, _order_id):
        return value

    return _fake_lock


def _stub_transition(monkeypatch, captured: list):
    async def _fake_transition(db, order, to, *, actor, payload=None, **_):
        captured.append({"to": to, "actor": actor, "payload": payload})
        order.state = to
        return order

    monkeypatch.setattr(shipment_events, "transition", _fake_transition)


def _stub_poll(monkeypatch, body: dict):
    """Stub ``jubelio_service.get_shipment_by_awb`` to return ``body``."""
    async def _fake_poll(awb):
        return body

    monkeypatch.setattr(jubelio_service, "get_shipment_by_awb", _fake_poll)


def _serialize_stub(app):
    """Stub ``_serialize_order`` so the test doesn't need a full Order row."""
    monkeypatch_target = "routers.orders._serialize_order"
    # Use monkeypatch fixture indirectly by passing the app-injected version.
    # We're keeping this simple: register an inline lambda via FastAPI's
    # dependency_overrides? No — easier path is to patch the module
    # binding directly. Tests do that individually via ``monkeypatch.setattr``
    # before calling client.post; here we provide a default that returns a
    # minimal shape so callers don't need to remember.

    def _lambda(o):
        return {
            "id": o.id,
            "state": o.state.value if hasattr(o.state, "value") else o.state,
        }

    # We can't apply a monkeypatch here (no fixture param) — tests will
    # call ``monkeypatch.setattr("routers.orders._serialize_order", _stub)``
    # explicitly when they need richer output.
    return monkeypatch_target, _lambda


# ---- 4xx paths -------------------------------------------------------------


class TestErrorPaths:
    def test_404_when_order_not_found(self, monkeypatch, client, app, db):
        monkeypatch.setattr(
            "routers.orders.lock_order_for_update", _stub_lock_returning(None)
        )
        resp = client.post("/api/v1/orders/ghost/tracking/refresh")
        assert resp.status_code == 404

    def test_409_when_state_not_fulfilling(self, monkeypatch, client, app, db):
        from models.order import OrderState

        order = _stub_order(state=OrderState.ESCROW_HELD, awb=None)
        monkeypatch.setattr(
            "routers.orders.lock_order_for_update", _stub_lock_returning(order)
        )
        resp = client.post("/api/v1/orders/order-1/tracking/refresh")
        assert resp.status_code == 409
        assert (
            "FULFILLING" in resp.json()["detail"]
            or "state" in resp.json()["detail"].lower()
        )

    def test_409_when_no_awb(self, monkeypatch, client, app, db):
        order = _stub_order(awb=None)
        monkeypatch.setattr(
            "routers.orders.lock_order_for_update", _stub_lock_returning(order)
        )
        resp = client.post("/api/v1/orders/order-1/tracking/refresh")
        assert resp.status_code == 409
        assert "AWB" in resp.json()["detail"]

    def test_502_when_jubelio_404(self, monkeypatch, client, app, db):
        order = _stub_order()
        monkeypatch.setattr(
            "routers.orders.lock_order_for_update", _stub_lock_returning(order)
        )

        async def _fake_poll(awb):
            raise jubelio_service.ShippingError(
                "Jubelio detail 404: AWB tidak ditemukan"
            )

        monkeypatch.setattr(jubelio_service, "get_shipment_by_awb", _fake_poll)
        monkeypatch.setattr(
            "routers.orders._serialize_order",
            lambda o: {"id": o.id, "state": o.state.value},
        )
        resp = client.post("/api/v1/orders/order-1/tracking/refresh")
        assert resp.status_code == 502


# ---- happy paths -----------------------------------------------------------


class TestHappyPath:
    def test_delivered_poll_transitions_to_received(self, monkeypatch, client, app, db):
        order = _stub_order()
        monkeypatch.setattr(
            "routers.orders.lock_order_for_update", _stub_lock_returning(order)
        )
        monkeypatch.setattr(
            "routers.orders._serialize_order",
            lambda o: {
                "id": o.id,
                "state": o.state.value if hasattr(o.state, "value") else o.state,
                "fulfillment_awb": o.fulfillment_awb,
            },
        )
        _stub_poll(
            monkeypatch,
            {
                "shipment_id": 1281,
                "awb": "AWB-1",
                "tracking_url": "https://track",
                "latest_status": "DELIVERED",
                "tracking": {"date": "2025-04-01T12:00:00Z"},
            },
        )
        captured: list = []
        _stub_transition(monkeypatch, captured)

        resp = client.post("/api/v1/orders/order-1/tracking/refresh")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["state"] == "RECEIVED"
        # FULFILLING → RECEIVED transition happened.
        from models.order import OrderState

        assert captured[0]["to"] == OrderState.RECEIVED
        assert captured[0]["actor"] == "system:jubelio_refresh"

    def test_in_transit_poll_persists_no_transition(self, monkeypatch, client, app, db):
        order = _stub_order()
        monkeypatch.setattr(
            "routers.orders.lock_order_for_update", _stub_lock_returning(order)
        )
        monkeypatch.setattr(
            "routers.orders._serialize_order",
            lambda o: {"id": o.id, "state": o.state.value},
        )
        _stub_poll(
            monkeypatch,
            {
                "shipment_id": 1281,
                "awb": "AWB-1",
                "tracking_url": "https://track",
                "latest_status": "ON_DELIVERY",
                "tracking": {"date": "2025-04-01T12:00:00Z"},
            },
        )
        captured: list = []
        _stub_transition(monkeypatch, captured)

        resp = client.post("/api/v1/orders/order-1/tracking/refresh")

        assert resp.status_code == 200, resp.text
        assert captured == []
        assert order.fulfillment_status == "on_delivery"

    def test_refresh_fills_missing_tracking_url(self, monkeypatch, client, app, db):
        """The endpoint requires an AWB, but other fulfillment fields can
        legitimately be missing (race between book_shipment persist and the
        first webhook delivery). Verify the seam fills tracking_url when
        the poll returns it."""
        order = _stub_order(awb="AWB-1", tracking_url=None)
        monkeypatch.setattr(
            "routers.orders.lock_order_for_update", _stub_lock_returning(order)
        )
        monkeypatch.setattr(
            "routers.orders._serialize_order",
            lambda o: {"id": o.id, "state": o.state.value},
        )
        _stub_poll(
            monkeypatch,
            {
                "shipment_id": 1281,
                "awb": "AWB-1",
                "tracking_url": "https://track.new",
                "latest_status": "PICKED_UP",
                "tracking": {"date": "2025-04-01T12:00:00Z"},
            },
        )

        async def _noop_transition(*_a, **_k):
            return None

        monkeypatch.setattr(
            shipment_events, "transition", _noop_transition
        )

        resp = client.post("/api/v1/orders/order-1/tracking/refresh")

        assert resp.status_code == 200, resp.text
        assert order.fulfillment_awb == "AWB-1"
        assert order.fulfillment_tracking_url == "https://track.new"


# ---- auth ------------------------------------------------------------------


class TestAuth:
    def test_401_without_admin_token(self, client, app, db):
        from deps import require_admin_token as real

        async def _deny():
            from fastapi import HTTPException

            raise HTTPException(401, "admin token required")

        app.dependency_overrides[real] = _deny
        resp = client.post("/api/v1/orders/order-1/tracking/refresh")
        assert resp.status_code == 401
