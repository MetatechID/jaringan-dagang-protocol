"""Sento webhook router — pure unit tests.

Mirrors ``tests/test_webhooks_oy.py`` — same fake harness, Sento-specific
endpoints. Covers:

- Status dispatch (``complete`` → handle_paid, ``expired`` → handle_expired,
  ``failed`` / ``closed`` → handle_failed, other statuses → acknowledged
  but ignored)
- /invoice PAID on order path → ``mark_order_paid`` runs with
  actor='system:sento_webhook'
- /invoice PAID on cart path → cart.payment_state='paid',
  invoice_provider='sento', EscrowLedger inserted (when cart.order_id)
- /invoice EXPIRED → cart.payment_state='expired' + cart.status=EXPIRED
- /invoice FAILED/CLOSED → cart.payment_state='failed'
- 400 / 404 / 401 / 200 error paths
- Status verification: sento_client.get_status called before mutation;
  SentoError(404) → 404 to caller; other SentoError → proceed defensively
- Brand resolution (cart-branch, order-snapshot-branch, no-match)
- Payment Link shape: lowercase ``status`` field on body, flat response

DB and HTTP fakes live in ``tests/_sento_fakes``.

Sento's webhook contract has no HMAC — we verify via
``get_status`` instead (see ``routers/webhooks_sento.py``).
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

from routers.webhooks_sento import (  # noqa: E402
    router as sento_router,
)
from services import sento_client  # noqa: E402
from services.sento_client import SentoError  # noqa: E402
from tests._sento_fakes import (  # noqa: E402
    FakeSession,
    StubBrand,
    StubCart,
    StubOrder,
)


@pytest.fixture
def client():
    """Build a TestClient. Tests install their FakeSession via
    ``app.dependency_overrides[get_db]``."""
    app = FastAPI()
    app.include_router(sento_router)
    return TestClient(app), app


def _patch_db(app, monkeypatch, *results):
    """Install a stubbed get_db via FastAPI's dependency_overrides dict."""
    from database import get_db as real_get_db  # noqa: F401

    session = FakeSession(list(results))

    async def _stub_get_db():
        yield session

    app.dependency_overrides[real_get_db] = _stub_get_db
    return session


def _stub_brand_lookup(monkeypatch, brand):
    async def _resolve(db, partner_tx_id):
        return brand
    monkeypatch.setattr(
        "routers.webhooks_sento._resolve_brand_for_invoice_id", _resolve
    )


def _stub_status_check(
    monkeypatch, *, raises=None, returns=None
):
    """Replace ``sento_client.get_status`` so the test controls
    the response.

    Default: returns a fake 'complete' status (Payment Link shape) so
    the happy-path runs end-to-end. Tests wanting 404 behavior pass a
    SentoError via ``raises``.
    """
    async def _fake_get_status(
        *, partner_tx_id, api_key=None, username=None,
    ):
        if raises is not None:
            raise raises
        return returns or {"status": "complete", "partner_tx_id": partner_tx_id}

    monkeypatch.setattr(sento_client, "get_status", _fake_get_status)
    return _fake_get_status


# ---- Status dispatch — Payment Link shape --------------------------------


class TestInvoiceCallbackComplete:
    def test_handle_paid_order_path_uses_tx_ref_number(self, monkeypatch, client):
        """complete + partner_tx_id → mark_order_paid runs with
        actor='system:sento_webhook' and invoice_id from tx_ref_number
        (the Sento-internal id that flows into the ledger as external_ref)."""
        client_, app = client
        brand = StubBrand()
        _patch_db(app, monkeypatch)
        _stub_brand_lookup(monkeypatch, brand)
        _stub_status_check(
            monkeypatch,
            returns={"status": "complete", "partner_tx_id": "order-ord-1"},
        )

        captured: dict = {}

        async def fake_mark_paid(db, *, order_id, invoice_id, actor, **_):
            captured.update(order_id=order_id, invoice_id=invoice_id, actor=actor)
            return SimpleNamespace(id=order_id, state=SimpleNamespace(value="ESCROW_HELD"))
        monkeypatch.setattr("routers.webhooks_sento.order_paid.mark_order_paid", fake_mark_paid)

        body = json.dumps({
            "partner_tx_id": "order-ord-1",
            "tx_ref_number": "STX-1",
            "status": "complete",
            "amount": 110000,
            "paid_amount": 110000,
            "payment_reference_number": "PRN-X1",
        }).encode()
        resp = client_.post(
            "/webhooks/sento/invoice",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        assert captured["order_id"] == "ord-1"
        assert captured["actor"] == "system:sento_webhook"
        # tx_ref_number (Sento's internal id) wins; falls back to partner_tx_id
        # only when tx_ref_number is absent.
        assert captured["invoice_id"] == "STX-1"
        assert resp.json()["state"] == "ESCROW_HELD"

    def test_handle_paid_cart_path_inserts_escrow_ledger(self, monkeypatch, client):
        client_, app = client
        brand = StubBrand()
        cart = StubCart(payment_state="pending", order_id="order-abc")
        # cart SELECT + escrow-ledger verify SELECT (returns None).
        session = _patch_db(app, monkeypatch, cart, None)
        _stub_brand_lookup(monkeypatch, brand)
        _stub_status_check(
            monkeypatch,
            returns={"status": "complete"},
        )

        body = json.dumps({
            "partner_tx_id": f"cart-{cart.id}",
            "tx_ref_number": "STX-2",
            "status": "complete",
            "amount": 50000,
            "paid_amount": 50000,
            "payment_reference_number": "PRN-9",
        }).encode()
        resp = client_.post(
            "/webhooks/sento/invoice",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["cart_id"] == cart.id
        assert cart.payment_state == "paid"
        assert cart.invoice_provider == "sento"
        assert len(session.added) == 1, session.added
        ledger = session.added[0]
        from models.escrow_ledger import EscrowEntryStatus, EscrowEntryType
        assert ledger.entry_type == EscrowEntryType.HOLD
        assert ledger.status == EscrowEntryStatus.COMPLETED
        assert ledger.order_id == "order-abc"

    def test_handle_expired_updates_cart_state(self, monkeypatch, client):
        client_, app = client
        brand = StubBrand()
        cart = StubCart(payment_state="pending")
        _patch_db(app, monkeypatch, cart)
        _stub_brand_lookup(monkeypatch, brand)
        _stub_status_check(
            monkeypatch,
            returns={"status": "complete"},  # status-api shape; body drives dispatch
        )

        body = json.dumps({
            "partner_tx_id": f"cart-{cart.id}",
            "status": "expired",
        }).encode()
        resp = client_.post(
            "/webhooks/sento/invoice",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        assert cart.payment_state == "expired"
        from models.bot_rest import CartStatus
        assert cart.status == CartStatus.EXPIRED

    def test_handle_failed_updates_cart_state(self, monkeypatch, client):
        client_, app = client
        brand = StubBrand()
        cart = StubCart(payment_state="pending")
        _patch_db(app, monkeypatch, cart)
        _stub_brand_lookup(monkeypatch, brand)
        _stub_status_check(
            monkeypatch,
            returns={"status": "complete"},
        )

        body = json.dumps({
            "partner_tx_id": f"cart-{cart.id}",
            "status": "failed",
        }).encode()
        resp = client_.post(
            "/webhooks/sento/invoice",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        assert cart.payment_state == "failed"

    def test_ignores_pending(self, monkeypatch, client):
        client_, app = client
        brand = StubBrand()
        _patch_db(app, monkeypatch)
        _stub_brand_lookup(monkeypatch, brand)
        _stub_status_check(
            monkeypatch,
            returns={"status": "waiting_payment"},
        )

        body = json.dumps({
            "partner_tx_id": "cart-pending",
            "status": "waiting_payment",
        }).encode()
        resp = client_.post(
            "/webhooks/sento/invoice",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["ok"] is True
        # _parse_status normalizes waiting_payment → "pending".
        assert out["ignored_status"] == "pending"

    def test_handle_paid_recovers_mock_invoice_via_snapshot(self, monkeypatch, client):
        """Mock-mode invoice id (``sento-dev-{id}``) recovers via snapshot."""
        client_, app = client
        brand = StubBrand()
        order = StubOrder(id="order-recovered")
        session = _patch_db(app, monkeypatch, order, order)
        _stub_brand_lookup(monkeypatch, brand)
        _stub_status_check(
            monkeypatch,
            returns={"status": "complete"},
        )

        captured: dict = {}

        async def fake_mark_paid(db, *, order_id, invoice_id, actor, **_):
            captured.update(order_id=order_id, invoice_id=invoice_id, actor=actor)
            return SimpleNamespace(id=order_id, state=SimpleNamespace(value="ESCROW_HELD"))
        monkeypatch.setattr("routers.webhooks_sento.order_paid.mark_order_paid", fake_mark_paid)

        body = json.dumps({
            "partner_tx_id": "sento-dev-abc-123",
            "status": "complete",
        }).encode()
        resp = client_.post(
            "/webhooks/sento/invoice",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        assert captured["order_id"] == "order-recovered"
        assert captured["actor"] == "system:sento_webhook"
        assert resp.json()["state"] == "ESCROW_HELD"

    def test_callback_status_success_triggers_paid(self, monkeypatch, client):
        """Payment Link callback sends ``status: "success"`` (not ``"complete"``).
        _parse_status must normalize it to "complete" so _handle_paid fires."""
        client_, app = client
        brand = StubBrand()
        _patch_db(app, monkeypatch)
        _stub_brand_lookup(monkeypatch, brand)
        _stub_status_check(
            monkeypatch,
            returns={"status": "complete"},
        )

        captured: dict = {}

        async def fake_mark_paid(db, *, order_id, invoice_id, actor, **_):
            captured.update(order_id=order_id, invoice_id=invoice_id, actor=actor)
            return SimpleNamespace(id=order_id, state=SimpleNamespace(value="ESCROW_HELD"))
        monkeypatch.setattr("routers.webhooks_sento.order_paid.mark_order_paid", fake_mark_paid)

        body = json.dumps({
            "partner_tx_id": "order-ord-99",
            "tx_ref_number": "STX-99",
            "status": "success",
            "amount": 55000,
            "paid_amount": 55000,
        }).encode()
        resp = client_.post(
            "/webhooks/sento/invoice",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        assert captured["order_id"] == "ord-99"
        assert captured["actor"] == "system:sento_webhook"
        assert resp.json()["state"] == "ESCROW_HELD"

    def test_callback_status_processing_treated_as_pending(self, monkeypatch, client):
        """Payment Link callback sends ``status: "processing"`` for in-flight
        transactions. Must be treated as pending (ignored, no mutation)."""
        client_, app = client
        brand = StubBrand()
        _patch_db(app, monkeypatch)
        _stub_brand_lookup(monkeypatch, brand)
        _stub_status_check(
            monkeypatch,
            returns={"status": "processing"},
        )

        body = json.dumps({
            "partner_tx_id": "cart-pending-2",
            "status": "processing",
        }).encode()
        resp = client_.post(
            "/webhooks/sento/invoice",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["ok"] is True
        assert out["ignored_status"] == "pending"


# ---- Error paths -----------------------------------------------------------


class TestInvoiceCallbackErrors:
    def test_400_on_missing_partner_tx_id(self, monkeypatch, client):
        client_, app = client
        _patch_db(app, monkeypatch)
        _stub_brand_lookup(monkeypatch, StubBrand())

        body = json.dumps({"status": "complete"}).encode()
        resp = client_.post(
            "/webhooks/sento/invoice",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert "partner_tx_id" in resp.json()["detail"].lower()

    def test_returns_404_when_no_brand_for_partner_tx_id(self, monkeypatch, client):
        client_, app = client
        _patch_db(app, monkeypatch)
        _stub_brand_lookup(monkeypatch, None)

        body = json.dumps({
            "partner_tx_id": "cart-ghost",
            "status": "complete",
        }).encode()
        resp = client_.post(
            "/webhooks/sento/invoice",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 404


# ---- Status verification ---------------------------------------------------


class TestStatusVerification:
    def test_calls_get_status_before_mutation_when_brand_found(self, monkeypatch, client):
        client_, app = client
        brand = StubBrand(sento_api_key="brand-key", sento_username="brand-user")
        _patch_db(app, monkeypatch)
        _stub_brand_lookup(monkeypatch, brand)

        calls: dict = {}

        async def _fake_get_status(
            *, partner_tx_id, api_key=None, username=None,
        ):
            calls.update(
                partner_tx_id=partner_tx_id,
                api_key=api_key,
                username=username,
            )
            return {"status": "complete", "partner_tx_id": partner_tx_id}
        monkeypatch.setattr(sento_client, "get_status", _fake_get_status)

        async def fake_mark_paid(db, **_kw):
            return SimpleNamespace(id="ord-1", state=SimpleNamespace(value="ESCROW_HELD"))
        monkeypatch.setattr("routers.webhooks_sento.order_paid.mark_order_paid", fake_mark_paid)

        body = json.dumps({
            "partner_tx_id": "order-ord-1",
            "status": "complete",
        }).encode()
        resp = client_.post(
            "/webhooks/sento/invoice",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        assert calls["partner_tx_id"] == "order-ord-1"
        assert calls["api_key"] == "brand-key"
        assert calls["username"] == "brand-user"

    def test_404_when_status_api_returns_invoice_not_found(self, monkeypatch, client):
        client_, app = client
        brand = StubBrand()
        _patch_db(app, monkeypatch)
        _stub_brand_lookup(monkeypatch, brand)
        _stub_status_check(
            monkeypatch,
            raises=SentoError(404, {"message": "invoice not found"}),
        )

        body = json.dumps({
            "partner_tx_id": "cart-ghost",
            "status": "complete",
        }).encode()
        resp = client_.post(
            "/webhooks/sento/invoice",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 404

    def test_proceeds_with_status_from_body_if_get_status_fails_for_other_reason(
        self, monkeypatch, client,
    ):
        """Non-404 SentoError from get_status → proceed with body
        status (defensive: webhooks are advisory, status API may be transient)."""
        client_, app = client
        brand = StubBrand()
        cart = StubCart(payment_state="pending")
        _patch_db(app, monkeypatch, cart, None)
        _stub_brand_lookup(monkeypatch, brand)
        _stub_status_check(
            monkeypatch,
            raises=SentoError(500, {"message": "internal error"}),
        )

        body = json.dumps({
            "partner_tx_id": f"cart-{cart.id}",
            "status": "complete",
            "amount": 100000,
            "paid_amount": 100000,
            "payment_reference_number": "PRN-2",
        }).encode()
        resp = client_.post(
            "/webhooks/sento/invoice",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        assert cart.payment_state == "paid"


# ---- Brand resolution branches --------------------------------------------


class TestResolveBrand:
    @pytest.mark.asyncio
    async def test_resolves_brand_via_cart_invoice_id_with_provider_sento(self):
        from routers.webhooks_sento import _resolve_brand_for_invoice_id
        cart = StubCart(invoice_id="cart-stx-1", invoice_provider="sento")
        brand = StubBrand()
        db = FakeSession([cart, brand])
        result = await _resolve_brand_for_invoice_id(db, "cart-stx-1")
        assert result is brand

    @pytest.mark.asyncio
    async def test_resolves_brand_via_order_snapshot_invoice_id(self):
        from routers.webhooks_sento import _resolve_brand_for_invoice_id
        order = StubOrder(
            payment_method_snapshot={
                "type": "sento_invoice",  # Payment Link shape
                "payment_provider": "sento",
                "invoice_id": "order-stx-2",
            }
        )
        brand = StubBrand()
        db = FakeSession([None, order, brand])
        result = await _resolve_brand_for_invoice_id(db, "order-stx-2")
        assert result is brand

    @pytest.mark.asyncio
    async def test_returns_none_when_no_match(self):
        from routers.webhooks_sento import _resolve_brand_for_invoice_id
        db = FakeSession([None, None])
        result = await _resolve_brand_for_invoice_id(db, "stx-unknown")
        assert result is None
