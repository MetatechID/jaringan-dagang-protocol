"""Tests for Dipay invoice routing: ``POST /api/v1/orders/{order_id}/invoice``.

Covers:
- Idempotent return of ready snapshots across order states (CART_REVIEWED, ESCROW_HELD)
- Backward compatibility for legacy Dipay snapshots with qris_content
- Retrying partial or failed Dipay snapshots on CART_REVIEWED
- Rejecting partial snapshots on non-CART_REVIEWED states with 409
- Mapping DipayError and unexpected exceptions to sanitized HTTP 502
- Standard 404 guards for missing order or mismatched buyer profile
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

import pytest  # noqa: E402
from database import get_db  # noqa: E402
from deps import get_current_profile  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from models.order import OrderState  # noqa: E402
from routers.orders import router as orders_router  # noqa: E402
from services.dipay_client import DipayError  # noqa: E402

from tests._dipay_fakes import FakeSession, StubBrand  # noqa: E402


def _stub_order(
    *,
    order_id: str = "order-test-1",
    profile_id: str = "profile-1",
    brand_id: str = "brand-id",
    state: OrderState = OrderState.CART_REVIEWED,
    payment_method_snapshot: dict | None = None,
    total_idr: int = 150_000,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=order_id,
        profile_id=profile_id,
        brand_id=brand_id,
        state=state,
        payment_method_snapshot=payment_method_snapshot,
        total_idr=total_idr,
        items=[{"name": "Hijab", "qty": 1, "unit_price_idr": total_idr}],
        shipping_address={"email": "buyer@example.com", "recipient_name": "Buyer"},
    )


@pytest.fixture
def test_profile():
    return SimpleNamespace(
        id="profile-1",
        email="buyer@example.com",
        display_name="Buyer One",
    )


def _build_test_app(db_session: FakeSession, profile: SimpleNamespace) -> TestClient:
    app = FastAPI()
    app.include_router(orders_router)

    async def _override_get_db():
        yield db_session

    async def _override_profile():
        return profile

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_profile] = _override_profile
    return TestClient(app)


def test_ready_snapshot_idempotent_on_cart_reviewed(test_profile, monkeypatch):
    """An order in CART_REVIEWED with a ready Dipay snapshot returns immediately."""
    ready_snap = {
        "payment_provider": "dipay",
        "invoice_status": "ready",
        "invoice_id": "q-order1",
        "partner_ref": "q-order1",
        "invoice_url": "https://api.beli-aman.metatech.id/api/v1/qris/q-order1.png",
        "qris_image_url": "https://api.beli-aman.metatech.id/api/v1/qris/q-order1.png",
        "qris_content": "00020101021226...READY",
        "expires_at": "2026-09-24T12:00:00+07:00",
    }
    order = _stub_order(
        state=OrderState.CART_REVIEWED,
        payment_method_snapshot=ready_snap,
    )
    db = FakeSession([order])
    client = _build_test_app(db, test_profile)

    async def explode(**_kwargs):
        raise AssertionError("dipay_invoices service must not be invoked for ready snapshot")

    from services import dipay_invoices
    monkeypatch.setattr(dipay_invoices, "create_invoice_for_order", explode)

    resp = client.post(f"/api/v1/orders/{order.id}/invoice")
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "dipay"
    assert data["invoice_id"] == "q-order1"
    assert data["qris_content"] == "00020101021226...READY"
    assert data["invoice_url"] == ready_snap["invoice_url"]


def test_ready_snapshot_idempotent_on_post_payment_state(test_profile, monkeypatch):
    """An order already in ESCROW_HELD with ready snapshot returns 200 idempotently."""
    ready_snap = {
        "payment_provider": "dipay",
        "invoice_status": "ready",
        "invoice_id": "q-order1",
        "partner_ref": "q-order1",
        "invoice_url": "https://api.beli-aman.metatech.id/api/v1/qris/q-order1.png",
        "qris_image_url": "https://api.beli-aman.metatech.id/api/v1/qris/q-order1.png",
        "qris_content": "00020101021226...READY",
        "expires_at": "2026-09-24T12:00:00+07:00",
    }
    order = _stub_order(
        state=OrderState.ESCROW_HELD,
        payment_method_snapshot=ready_snap,
    )
    db = FakeSession([order])
    client = _build_test_app(db, test_profile)

    resp = client.post(f"/api/v1/orders/{order.id}/invoice")
    assert resp.status_code == 200
    assert resp.json()["invoice_id"] == "q-order1"
    assert resp.json()["state"] == "ESCROW_HELD"


def test_legacy_snapshot_with_qris_content_considered_ready(test_profile):
    """Legacy snapshots having qris_content and invoice_url without invoice_status are ready."""
    legacy_snap = {
        "payment_provider": "dipay",
        "invoice_id": "q-order1",
        "partner_ref": "q-order1",
        "invoice_url": "https://api.beli-aman.metatech.id/api/v1/qris/q-order1.png",
        "qris_image_url": "https://api.beli-aman.metatech.id/api/v1/qris/q-order1.png",
        "qris_content": "00020101021226...LEGACY",
    }
    order = _stub_order(
        state=OrderState.CART_REVIEWED,
        payment_method_snapshot=legacy_snap,
    )
    db = FakeSession([order])
    client = _build_test_app(db, test_profile)

    resp = client.post(f"/api/v1/orders/{order.id}/invoice")
    assert resp.status_code == 200
    assert resp.json()["qris_content"] == "00020101021226...LEGACY"


def test_partial_failed_snapshot_on_cart_reviewed_retries_service(test_profile, monkeypatch):
    """An incident order (partial snapshot with phantom URL but no qris_content, or failed status) retries."""
    partial_snap = {
        "payment_provider": "dipay",
        "invoice_status": "failed",
        "invoice_id": "q-order1",
        "partner_ref": "q-order1",
        "invoice_url": "https://api.beli-aman.metatech.id/api/v1/qris/q-order1.png",
    }
    order = _stub_order(
        state=OrderState.CART_REVIEWED,
        payment_method_snapshot=partial_snap,
    )
    brand = StubBrand(payment_provider="dipay")
    db = FakeSession([order, brand])
    client = _build_test_app(db, test_profile)

    called = []

    async def fake_create_order_invoice(_db, _order, **_kwargs):
        called.append(True)
        return {
            "id": "q-order1",
            "invoice_url": "https://api.beli-aman.metatech.id/api/v1/qris/q-order1.png",
            "qris_image_url": "https://api.beli-aman.metatech.id/api/v1/qris/q-order1.png",
            "qris_content": "00020101021226...NEW",
            "expires_at": "2026-09-24T12:30:00+07:00",
        }

    from services import dipay_invoices
    monkeypatch.setattr(dipay_invoices, "create_invoice_for_order", fake_create_order_invoice)

    resp = client.post(f"/api/v1/orders/{order.id}/invoice")
    assert resp.status_code == 200
    assert len(called) == 1
    assert resp.json()["qris_content"] == "00020101021226...NEW"


def test_partial_snapshot_on_non_cart_reviewed_returns_409(test_profile):
    """An order in PRE_AUTH or AUTHED with a partial snapshot cannot create invoice."""
    partial_snap = {
        "payment_provider": "dipay",
        "invoice_status": "creating",
        "invoice_id": "q-order1",
    }
    order = _stub_order(
        state=OrderState.AUTHED,
        payment_method_snapshot=partial_snap,
    )
    db = FakeSession([order])
    client = _build_test_app(db, test_profile)

    resp = client.post(f"/api/v1/orders/{order.id}/invoice")
    assert resp.status_code == 409
    assert "Cannot create invoice in state AUTHED" in resp.json()["detail"]


def test_dipay_error_returns_sanitized_502(test_profile, monkeypatch):
    """Dipay upstream error (like 4017300 Unknown Client) returns sanitized 502 with no leak."""
    order = _stub_order(state=OrderState.CART_REVIEWED)
    brand = StubBrand(payment_provider="dipay")
    db = FakeSession([order, brand])
    client = _build_test_app(db, test_profile)

    async def fake_fail(_db, _order, **_kwargs):
        raise DipayError(401, {"responseCode": "4017300", "responseMessage": "Unauthorized. Unknown Client"})

    from services import dipay_invoices
    monkeypatch.setattr(dipay_invoices, "create_invoice_for_order", fake_fail)

    resp = client.post(f"/api/v1/orders/{order.id}/invoice")
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Payment provider temporarily unavailable"
    # Verify no upstream secret / payload leak in response body
    assert "4017300" not in resp.text
    assert "Unknown Client" not in resp.text


def test_unexpected_exception_returns_sanitized_502(test_profile, monkeypatch):
    """Network connection failure or timeout returns sanitized 502."""
    order = _stub_order(state=OrderState.CART_REVIEWED)
    brand = StubBrand(payment_provider="dipay")
    db = FakeSession([order, brand])
    client = _build_test_app(db, test_profile)

    async def fake_net_err(_db, _order, **_kwargs):
        raise RuntimeError("Connection reset by peer: secret-connection-info")

    from services import dipay_invoices
    monkeypatch.setattr(dipay_invoices, "create_invoice_for_order", fake_net_err)

    resp = client.post(f"/api/v1/orders/{order.id}/invoice")
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Payment provider temporarily unavailable"
    assert "secret-connection-info" not in resp.text


def test_order_not_found_returns_404(test_profile):
    db = FakeSession([None])
    client = _build_test_app(db, test_profile)
    resp = client.post("/api/v1/orders/nonexistent-id/invoice")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Order not found"


def test_order_wrong_profile_returns_404(test_profile):
    order = _stub_order(profile_id="other-profile-id")
    db = FakeSession([order])
    client = _build_test_app(db, test_profile)
    resp = client.post(f"/api/v1/orders/{order.id}/invoice")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Order not found"
