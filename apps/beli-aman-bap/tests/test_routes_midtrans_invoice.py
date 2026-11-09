"""Hosted invoice contract remains provider-specific and retry-safe."""
from types import SimpleNamespace
import pytest
from models.order import OrderState
from tests._dipay_fakes import FakeSession
from tests.test_routes_dipay_invoice import _build_test_app, _stub_order


@pytest.mark.parametrize("state", [OrderState.CART_REVIEWED, OrderState.ESCROW_HELD])
def test_ready_midtrans_snapshot_is_reused(state, monkeypatch):
    from services import midtrans_invoices
    async def forbidden(*args, **kwargs):
        raise AssertionError("Ready invoice must not trigger provider creation")
    monkeypatch.setattr(midtrans_invoices, "create_invoice_for_order", forbidden)
    snapshot = dict(payment_provider="midtrans", invoice_status="ready", invoice_id="mt-stable",
                    invoice_url="https://app.sandbox.midtrans.com/payment-links/opaque",
                    expires_at="2026-12-01T12:00:00+00:00")
    order = _stub_order(state=state, payment_method_snapshot=snapshot)
    profile = SimpleNamespace(id="profile-1", email="buyer@example.com", display_name="Buyer")
    response = _build_test_app(FakeSession([order]), profile).post(f"/api/v1/orders/{order.id}/invoice")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "midtrans"
    assert body["invoice_id"] == "mt-stable"
    assert body["invoice_url"] == snapshot["invoice_url"]
    assert not body.get("qris_content")
    assert not body.get("qris_image_url")


def test_partial_midtrans_snapshot_cannot_create_after_payment():
    order = _stub_order(state=OrderState.ESCROW_HELD, payment_method_snapshot={
        "payment_provider": "midtrans", "invoice_status": "creating", "invoice_id": "mt-stable"})
    profile = SimpleNamespace(id="profile-1", email="buyer@example.com", display_name="Buyer")
    response = _build_test_app(FakeSession([order]), profile).post(f"/api/v1/orders/{order.id}/invoice")
    assert response.status_code == 409, response.text
