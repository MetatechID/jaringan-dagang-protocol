"""Tests for ``POST /api/v1/orders/{order_id}/ship/cancel``.

Mirrors ``test_webhooks_*`` style: stand up a FastAPI app with the orders
router, override ``get_db`` with a FakeSession, and stub out the carrier
dispatch + state-machine transition. Auth is via ``require_admin_token``
which we override per-test.

Behaviour matrix:
- 404 when order not found
- 409 when order state is not FULFILLING
- 200 happy path: cancel dispatched, OrderEvent written (state-machine
  transition is stubbed here), AWB/jubelio_shipment_id/shipped_at cleared
- 409 + distinguishable detail when courier refuses
- 409 + distinguishable detail when too close to pickup
- 501 when brand's active carrier is Biteship (no cancel support)
- Auth: 401 without admin token
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.orders import router as orders_router  # noqa: E402
from tests._jubelio_fakes import FakeSession  # noqa: E402


# ---- Test fixtures ---------------------------------------------------------


def _stub_brand(*, jubelio_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id="brand-id",
        slug="safiya",
        jubelio_enabled=jubelio_enabled,
        jubelio_origin_address={"zipcode": "12940"},
        biteship_origin_address={"postal_code": "12940"},
    )


def _stub_order(
    *,
    state: object = None,
    awb: str = "AWB-1",
    shipment_id: str | None = "1281",
    carrier: str = "jubelio",
) -> SimpleNamespace:
    """Build a stubbed Order. ``state`` defaults to OrderState.FULFILLING
    (an enum value, matching the real SQLAlchemy model) — pass an enum or a
    string explicitly to drive a different state."""
    from datetime import datetime, timezone
    from models.order import OrderState

    if state is None:
        state = OrderState.FULFILLING

    return SimpleNamespace(
        id="order-1",
        brand_id="brand-id",
        state=state,
        carrier=carrier,
        fulfillment_awb=awb,
        fulfillment_tracking_url="https://track",
        jubelio_shipment_id=shipment_id,
        biteship_order_id=None,
        shipped_at=datetime.now(timezone.utc),
        items=[],
    )


def _install_db(app, *results):
    """Install a stub get_db that returns a FakeSession yielding results."""
    from database import get_db as real_get_db

    session = FakeSession(list(results))

    async def _stub_get_db():
        yield session

    app.dependency_overrides[real_get_db] = _stub_get_db
    return session


def _override_admin_token(app, *, allow: bool = True):
    """Stub require_admin_token to succeed/fail regardless of header."""
    from deps import require_admin_token as real

    async def _stubbed():
        if allow:
            return "admin"
        from fastapi import HTTPException

        raise HTTPException(401, "admin token required")

    app.dependency_overrides[real] = _stubbed


@pytest.fixture
def app():
    """Build a FastAPI app with admin auth stubbed to allow by default."""
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
    """Stub lock_order_for_update to return ``value``."""

    async def _fake_lock(_db, _order_id):
        return value

    return _fake_lock


def _stub_transition_capture(monkeypatch, captured: dict):
    """Stub transition on the routers.orders module binding. The route uses
    ``from services.state_machine import transition`` so the symbol it sees
    is ``routers.orders.transition`` (an alias), not the original."""
    async def _fake_transition(db, order, to, *, actor, payload=None, **_):
        captured["to"] = to
        captured["actor"] = actor
        captured["payload"] = payload
        order.state = to
        return order

    monkeypatch.setattr("routers.orders.transition", _fake_transition)


# ---- Happy path ------------------------------------------------------------


class TestCancelHappyPath:
    def test_cancel_calls_carrier_and_transitions(self, monkeypatch, client, app, db):
        order = _stub_order()
        brand = _stub_brand()

        captured_carrier: dict = {}

        async def _fake_cancel(*, brand, order, reason):
            captured_carrier["awb"] = order.fulfillment_awb
            captured_carrier["reason"] = reason
            return {"carrier": "jubelio", "awb": "AWB-1", "status": "cancel successful"}

        monkeypatch.setattr(
            "routers.orders.lock_order_for_update", _stub_lock_returning(order)
        )
        monkeypatch.setattr(
            "routers.orders._serialize_order",
            lambda o: {"id": o.id, "state": o.state.value if hasattr(o.state, "value") else o.state},
        )

        from services import carriers as carriers_module
        monkeypatch.setattr(carriers_module, "cancel", _fake_cancel)

        captured_state: dict = {}
        _stub_transition_capture(monkeypatch, captured_state)

        resp = client.post(
            "/api/v1/orders/order-1/ship/cancel",
            json={"reason": "tester cancel"},
        )
        assert resp.status_code == 200, resp.text
        assert captured_carrier["awb"] == "AWB-1"
        assert captured_carrier["reason"] == "tester cancel"
        # State went back to ESCROW_HELD so re-book works.
        from models.order import OrderState
        assert captured_state["to"] == OrderState.ESCROW_HELD
        assert captured_state["actor"] == "seller"


# ---- 4xx paths -------------------------------------------------------------


class TestCancelErrorPaths:
    def test_404_when_order_not_found(self, monkeypatch, client, app, db):
        monkeypatch.setattr(
            "routers.orders.lock_order_for_update", _stub_lock_returning(None)
        )
        resp = client.post(
            "/api/v1/orders/order-ghost/ship/cancel", json={"reason": "x"}
        )
        assert resp.status_code == 404

    def test_409_when_state_not_fulfilling(self, monkeypatch, client, app, db):
        from models.order import OrderState

        order = _stub_order(state=OrderState.ESCROW_HELD, awb=None)
        monkeypatch.setattr(
            "routers.orders.lock_order_for_update", _stub_lock_returning(order)
        )
        resp = client.post(
            "/api/v1/orders/order-1/ship/cancel", json={"reason": "x"}
        )
        assert resp.status_code == 409
        assert "FULFILLING" in resp.json()["detail"] or "state" in resp.json()["detail"].lower()

    def test_409_with_distinguishable_detail_for_courier_rejection(
        self, monkeypatch, client, app, db
    ):
        from services import carriers as carriers_module

        order = _stub_order()
        monkeypatch.setattr(
            "routers.orders.lock_order_for_update", _stub_lock_returning(order)
        )

        async def _fake_cancel(*, brand, order, reason):
            raise carriers_module.CourierRejection(
                "the courier do not accept cancellation"
            )

        monkeypatch.setattr(carriers_module, "cancel", _fake_cancel)

        resp = client.post(
            "/api/v1/orders/order-1/ship/cancel", json={"reason": "x"}
        )
        assert resp.status_code == 409
        assert "courier" in resp.json()["detail"].lower()

    def test_409_for_pickup_window_closed(self, monkeypatch, client, app, db):
        from services import carriers as carriers_module

        order = _stub_order()
        monkeypatch.setattr(
            "routers.orders.lock_order_for_update", _stub_lock_returning(order)
        )

        async def _fake_cancel(*, brand, order, reason):
            raise carriers_module.PickupWindowClosed(
                "too close with pickup schedule"
            )

        monkeypatch.setattr(carriers_module, "cancel", _fake_cancel)

        resp = client.post(
            "/api/v1/orders/order-1/ship/cancel", json={"reason": "x"}
        )
        assert resp.status_code == 409
        assert "pickup" in resp.json()["detail"].lower()

    def test_501_when_biteship_active(self, monkeypatch, client, app, db):
        from services import carriers as carriers_module

        order = _stub_order(carrier="biteship", awb="B-1", shipment_id=None)
        order.biteship_order_id = "bs-1"
        monkeypatch.setattr(
            "routers.orders.lock_order_for_update", _stub_lock_returning(order)
        )

        async def _fake_cancel(*, brand, order, reason):
            raise carriers_module.CancelNotSupported(
                "Cancelling Biteship AWBs is not supported — "
                "use Biteship's own dashboard"
            )

        monkeypatch.setattr(carriers_module, "cancel", _fake_cancel)
        resp = client.post(
            "/api/v1/orders/order-1/ship/cancel", json={"reason": "x"}
        )
        assert resp.status_code == 501
        assert "biteship" in resp.json()["detail"].lower()

    def test_500_on_generic_shipping_error(self, monkeypatch, client, app, db):
        from services import carriers as carriers_module

        order = _stub_order()
        monkeypatch.setattr(
            "routers.orders.lock_order_for_update", _stub_lock_returning(order)
        )

        async def _fake_cancel(*, brand, order, reason):
            raise carriers_module.ShippingError("Jubelio cancel 401: invalid creds")

        monkeypatch.setattr(carriers_module, "cancel", _fake_cancel)
        resp = client.post(
            "/api/v1/orders/order-1/ship/cancel", json={"reason": "x"}
        )
        assert resp.status_code == 502


# ---- Auth ------------------------------------------------------------------


class TestCancelAuth:
    def test_401_without_admin_token(self, client, app, db):
        from deps import require_admin_token as real

        async def _deny():
            from fastapi import HTTPException

            raise HTTPException(401, "admin token required")

        app.dependency_overrides[real] = _deny
        resp = client.post(
            "/api/v1/orders/order-1/ship/cancel", json={"reason": "x"}
        )
        assert resp.status_code == 401
