"""Dipay webhook router — pure unit tests.

Mirrors ``tests/test_webhooks_sento.py`` — same fake harness, Dipay-specific
endpoints. Covers:

- /webhooks/dipay/qris ``latestTransactionStatus`` dispatch: ``00`` →
  _handle_paid (order path via ``mark_order_paid`` actor
  'system:dipay_webhook'; cart path → payment_state='paid',
  invoice_provider='dipay', idempotent EscrowLedger HOLD row),
  ``01`` → acknowledged no-op, ``05`` → cart expired
- 404 on unknown refs and provider not-found; all transient or malformed
  verification failures fail closed without mutating local state
- provider status/reference/amount/currency are authoritative over callback JSON
- /webhooks/dipay/remit terminal codes: ``00`` → RELEASE row COMPLETED +
  Dipay's ``originalReferenceNo`` as external_ref + receipt appended,
  ``06`` → FAILED + failedReason appended, ``02`` → stays PENDING (with
  external_ref refreshed); 404 when no ledger row matches

DB and HTTP fakes live in ``tests/_dipay_fakes``.

Dipay's callback signing isn't verifiable in the demo env — the receiver
re-verifies via ``dipay_client.query_qris`` / ``get_disbursement_status``
instead (see ``routers/webhooks_dipay.py``).
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

from routers.webhooks_dipay import (  # noqa: E402
    router as dipay_router,
)
from services import dipay_client  # noqa: E402
from services.dipay_client import DipayError  # noqa: E402
from tests._dipay_fakes import (  # noqa: E402
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
    app.include_router(dipay_router)
    return TestClient(app), app


def _patch_db(app, monkeypatch, *results):
    """Install a stubbed get_db via FastAPI's dependency_overrides dict."""
    from database import get_db as real_get_db  # noqa: F401

    session = FakeSession(list(results))

    async def _stub_get_db():
        yield session

    app.dependency_overrides[real_get_db] = _stub_get_db
    return session


def _stub_resolve(monkeypatch, *, brand=None, cart=None, order=None, partner_order=None):
    """Replace ``_resolve_targets`` so the test controls which surface the
    callback resolves to."""
    async def _resolve(db, ref):
        return brand, cart, order, partner_order
    monkeypatch.setattr("routers.webhooks_dipay._resolve_targets", _resolve)


def _stub_query_qris(monkeypatch, *, raises=None, returns=None):
    """Replace ``dipay_client.query_qris`` — default: a live ``00`` status."""
    async def _fake_query(*, partner_reference_no, **kwargs):
        if raises is not None:
            raise raises
        if returns is not None:
            ret = dict(returns)
            ret.setdefault("originalPartnerReferenceNo", partner_reference_no)
            ret.setdefault("amount", {"value": "100000.00", "currency": "IDR"})
            return ret
        return {
            "originalPartnerReferenceNo": partner_reference_no,
            "latestTransactionStatus": "00",
            "amount": {"value": "100000.00", "currency": "IDR"},
        }
    monkeypatch.setattr(dipay_client, "query_qris", _fake_query)
    return _fake_query


def _stub_disbursement_status(monkeypatch, *, raises=None, returns=None):
    """Replace ``dipay_client.get_disbursement_status`` for remit tests."""
    async def _fake_status(*, partner_reference_no, **kwargs):
        if raises is not None:
            raise raises
        if returns is not None:
            ret = dict(returns)
            ret.setdefault("originalPartnerReferenceNo", partner_reference_no)
            return ret
        return {
            "originalPartnerReferenceNo": partner_reference_no,
            "latestTransactionStatus": "00",
        }
    monkeypatch.setattr(dipay_client, "get_disbursement_status", _fake_status)
    return _fake_status


def _stub_mark_paid(monkeypatch, captured=None):
    async def fake_mark_paid(db, *, order_id, invoice_id, actor, **_):
        if captured is not None:
            captured.update(order_id=order_id, invoice_id=invoice_id, actor=actor)
        return SimpleNamespace(id=order_id, state=SimpleNamespace(value="ESCROW_HELD"))
    monkeypatch.setattr(
        "routers.webhooks_dipay.order_paid.mark_order_paid", fake_mark_paid
    )


def _stub_ledger_row(*, partner_ref="r-abc123", order_id="order-1"):
    from models.escrow_ledger import EscrowEntryStatus, EscrowEntryType

    return SimpleNamespace(
        id="ledger-1",
        order_id=order_id,
        entry_type=EscrowEntryType.RELEASE,
        amount_idr=500_000,
        description="Funds released to seller after delivery confirmed",
        external_ref=None,
        partner_ref=partner_ref,
        status=EscrowEntryStatus.PENDING,
    )


def _qris_body(ref: str, status: str, **extra) -> bytes:
    payload = {
        "originalPartnerReferenceNo": ref,
        "latestTransactionStatus": status,
        "transactionStatusDesc": "desc",
        "amount": {"value": "100000.00", "currency": "IDR"},
        "originalExternalId": "EXT-1",
        "additionalInfo": {},
    }
    payload.update(extra)
    return json.dumps(payload).encode()


# ---- QRIS callback — status dispatch ---------------------------------------


class TestQrisCallbackPaid:
    def test_paid_order_path_marks_order_paid(self, monkeypatch, client):
        """00 + order-resolved ref → mark_order_paid runs with
        actor='system:dipay_webhook' and invoice_id = the echoed ref."""
        client_, app = client
        brand = StubBrand()
        order = StubOrder(id="ord-1", total_idr=100_000)
        _patch_db(app, monkeypatch)
        _stub_resolve(monkeypatch, brand=brand, order=order)
        _stub_query_qris(monkeypatch)
        _stub_mark_paid(monkeypatch)

        resp = client_.post(
            "/webhooks/dipay/qris",
            content=_qris_body("q0f0e8a2e123", "00"),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["responseCode"] == "2005200"
        assert resp.json()["state"] == "ESCROW_HELD"

    def test_paid_order_path_captured_args(self, monkeypatch, client):
        client_, app = client
        brand = StubBrand()
        order = StubOrder(id="ord-1", total_idr=100_000)
        _patch_db(app, monkeypatch)
        _stub_resolve(monkeypatch, brand=brand, order=order)
        _stub_query_qris(monkeypatch)

        captured: dict = {}
        _stub_mark_paid(monkeypatch, captured)

        resp = client_.post(
            "/webhooks/dipay/qris",
            content=_qris_body("q-ord-ref-1", "00"),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["responseCode"] == "2005200"
        assert captured["order_id"] == "ord-1"
        assert captured["invoice_id"] == "q-ord-ref-1"
        assert captured["actor"] == "system:dipay_webhook"

    def test_paid_cart_path_inserts_escrow_ledger(self, monkeypatch, client):
        client_, app = client
        brand = StubBrand()
        cart = StubCart(payment_state="pending", order_id="order-abc")
        # The cart branch's idempotency SELECT (escrow-ledger verify) → None.
        session = _patch_db(app, monkeypatch, None)
        _stub_resolve(monkeypatch, brand=brand, cart=cart)
        _stub_query_qris(monkeypatch)

        resp = client_.post(
            "/webhooks/dipay/qris",
            content=_qris_body("q-cart-ref-1", "00"),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["responseCode"] == "2005200"
        assert out["cart_id"] == cart.id
        assert out["payment_state"] == "paid"
        assert cart.payment_state == "paid"
        assert cart.invoice_provider == "dipay"
        assert len(session.added) == 1, session.added
        ledger = session.added[0]
        from models.escrow_ledger import EscrowEntryStatus, EscrowEntryType
        assert ledger.entry_type == EscrowEntryType.HOLD
        assert ledger.status == EscrowEntryStatus.COMPLETED
        assert ledger.order_id == "order-abc"
        assert ledger.external_ref == "q-cart-ref-1"
        # Amount comes from the cart's quote_json total.
        assert ledger.amount_idr == 100_000

    def test_paid_cart_path_is_idempotent_on_ledger(self, monkeypatch, client):
        """An existing HOLD row with the same external_ref → no new row."""
        client_, app = client
        brand = StubBrand()
        cart = StubCart(payment_state="paid", order_id="order-abc")
        from models.escrow_ledger import EscrowEntryStatus, EscrowEntryType
        existing = SimpleNamespace(
            entry_type=EscrowEntryType.HOLD, external_ref="q-cart-ref-1",
        )
        session = _patch_db(app, monkeypatch, existing)
        _stub_resolve(monkeypatch, brand=brand, cart=cart)
        _stub_query_qris(monkeypatch)

        resp = client_.post(
            "/webhooks/dipay/qris",
            content=_qris_body("q-cart-ref-1", "00"),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["responseCode"] == "2005200"
        assert session.added == []


class TestQrisCallbackPendingExpired:
    def test_pending_is_a_noop(self, monkeypatch, client):
        client_, app = client
        brand = StubBrand()
        order = StubOrder(id="ord-1", total_idr=100_000)
        session = _patch_db(app, monkeypatch)
        _stub_resolve(monkeypatch, brand=brand, order=order)
        _stub_query_qris(monkeypatch, returns={"latestTransactionStatus": "01"})
        _stub_mark_paid(monkeypatch)

        resp = client_.post(
            "/webhooks/dipay/qris",
            content=_qris_body("q-ord-ref-1", "01"),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["responseCode"] == "2005200"
        assert out["responseMessage"] == "Request has been processed successfully"
        assert out["ok"] is True
        assert out["status"] == "pending"
        # Nothing was mutated or enqueued.
        assert session.added == []

    def test_expired_updates_cart_state(self, monkeypatch, client):
        client_, app = client
        brand = StubBrand()
        cart = StubCart(payment_state="pending")
        _patch_db(app, monkeypatch)
        _stub_resolve(monkeypatch, brand=brand, cart=cart)
        _stub_query_qris(monkeypatch, returns={"latestTransactionStatus": "05"})

        resp = client_.post(
            "/webhooks/dipay/qris",
            content=_qris_body("q-cart-ref-1", "05"),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["responseCode"] == "2005200"
        assert cart.payment_state == "expired"
        from models.bot_rest import CartStatus
        assert cart.status == CartStatus.EXPIRED


# ---- QRIS callback — partner-order branch -----------------------------------


class TestQrisCallbackPartnerOrder:
    """External-merchant partner orders (e.g. Consumerland tickets) paid via
    Dipay QRIS — the ``/api/v1/partner`` surface, resolved by the same
    ``invoice_id == ref`` lookup the QRIS PNG renderer uses."""

    def _make_order(self, **overrides):
        from tests._dipay_fakes import StubPartnerOrder
        return StubPartnerOrder(**overrides)

    def _stub_mark_partner_paid(self, monkeypatch, captured=None):
        from services import partner_orders as po_mod

        async def fake_mark_paid(*, order, invoice_id, actor, invoice_provider="xendit", **_):
            if captured is not None:
                captured.update(
                    order=order, invoice_id=invoice_id, actor=actor,
                    invoice_provider=invoice_provider,
                )
            # Mirror the real service: mutate and return.
            order.status = "paid"
            order.paid_at = "2026-10-11T00:00:00+00:00"
            return order
        monkeypatch.setattr(po_mod, "mark_partner_order_paid", fake_mark_paid)
        # The router imports the module, not the symbol — patch what it uses.
        monkeypatch.setattr(
            "routers.webhooks_dipay.partner_orders.mark_partner_order_paid",
            fake_mark_paid,
        )

    def _stub_notify_bg(self, monkeypatch, captured=None):
        async def fake_bg(order_id):
            if captured is not None:
                captured.setdefault("bg_order_ids", []).append(order_id)
        monkeypatch.setattr(
            "routers.webhooks_dipay.partner_webhook.notify_partner_paid_bg", fake_bg
        )

    def test_paid_flips_partner_order_and_schedules_callback(self, monkeypatch, client):
        client_, app = client
        brand = StubBrand()
        p_order = self._make_order(id="po-1", amount_idr=100_000)
        _patch_db(app, monkeypatch)
        _stub_resolve(monkeypatch, brand=brand, partner_order=p_order)
        _stub_query_qris(monkeypatch)
        captured: dict = {}
        self._stub_mark_partner_paid(monkeypatch, captured)
        self._stub_notify_bg(monkeypatch, captured)

        resp = client_.post(
            "/webhooks/dipay/qris",
            content=_qris_body("q-po-ref-1", "00"),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["responseCode"] == "2005200"
        assert out["order_id"] == "po-1"
        assert out["status"] == "paid"
        assert captured["order"] is p_order
        assert captured["invoice_id"] == "q-po-ref-1"
        assert captured["actor"] == "system:dipay_webhook"
        assert captured["invoice_provider"] == "dipay"
        assert captured["bg_order_ids"] == ["po-1"]

    def test_paid_idempotent_when_already_paid(self, monkeypatch, client):
        """Real service no-ops on an already-paid order; router still ACKs."""
        from services import partner_orders as po_mod

        client_, app = client
        brand = StubBrand()
        p_order = self._make_order(id="po-1", status="paid")
        _patch_db(app, monkeypatch)
        _stub_resolve(monkeypatch, brand=brand, partner_order=p_order)
        _stub_query_qris(monkeypatch)

        called: dict = {}
        async def fake_mark_paid(*, order, **_):
            called["hit"] = True
            return order
        monkeypatch.setattr(
            "routers.webhooks_dipay.partner_orders.mark_partner_order_paid",
            fake_mark_paid,
        )
        self._stub_notify_bg(monkeypatch)

        resp = client_.post(
            "/webhooks/dipay/qris",
            content=_qris_body("q-po-ref-1", "00"),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "paid"
        # Router delegates idempotency to the service; it was still called.
        assert called.get("hit") is True

    def test_amount_mismatch_is_snap_invalid_amount(self, monkeypatch, client):
        client_, app = client
        brand = StubBrand()
        p_order = self._make_order(id="po-1", amount_idr=250_000)
        _patch_db(app, monkeypatch)
        _stub_resolve(monkeypatch, brand=brand, partner_order=p_order)
        _stub_query_qris(monkeypatch)  # verified amount = 100000.00 IDR

        resp = client_.post(
            "/webhooks/dipay/qris",
            content=_qris_body("q-po-ref-1", "00"),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 404, resp.text
        assert resp.json() == {"responseCode": "4045213", "responseMessage": "Invalid Amount"}

    def test_expired_flips_pending_partner_order(self, monkeypatch, client):
        from services import partner_orders as po_mod

        client_, app = client
        brand = StubBrand()
        p_order = self._make_order(id="po-1", status="pending")
        _patch_db(app, monkeypatch)
        _stub_resolve(monkeypatch, brand=brand, partner_order=p_order)
        _stub_query_qris(monkeypatch, returns={"latestTransactionStatus": "05"})

        async def fake_expired(order):
            order.status = "expired"
            return order
        monkeypatch.setattr(
            "routers.webhooks_dipay.partner_orders.mark_partner_order_expired",
            fake_expired,
        )

        resp = client_.post(
            "/webhooks/dipay/qris",
            content=_qris_body("q-po-ref-1", "05"),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["responseCode"] == "2005200"
        assert out["order_id"] == "po-1"
        assert out["status"] == "expired"

    def test_pending_status_is_a_noop(self, monkeypatch, client):
        client_, app = client
        brand = StubBrand()
        p_order = self._make_order(id="po-1", status="pending")
        session = _patch_db(app, monkeypatch)
        _stub_resolve(monkeypatch, brand=brand, partner_order=p_order)
        _stub_query_qris(monkeypatch, returns={"latestTransactionStatus": "01"})

        resp = client_.post(
            "/webhooks/dipay/qris",
            content=_qris_body("q-po-ref-1", "01"),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["responseCode"] == "2005200"
        assert out["status"] == "pending"
        assert p_order.status == "pending"
        assert session.added == []


# ---- QRIS callback — error paths -------------------------------------------


class TestQrisCallbackErrors:
    @pytest.mark.parametrize("body", [b"not json", b"[1, 2]"])
    def test_400_invalid_field_format_on_bad_json(self, monkeypatch, client, body):
        client_, app = client
        _patch_db(app, monkeypatch)

        resp = client_.post(
            "/webhooks/dipay/qris",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert resp.json() == {"responseCode": "4005201", "responseMessage": "Invalid Field Format"}

    def test_400_on_missing_ref(self, monkeypatch, client):
        client_, app = client
        _patch_db(app, monkeypatch)
        _stub_resolve(monkeypatch, brand=StubBrand())
        _stub_query_qris(monkeypatch)

        body = json.dumps({"latestTransactionStatus": "00"}).encode()
        resp = client_.post(
            "/webhooks/dipay/qris",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert resp.json()["responseCode"] == "4005202"
        assert "originalPartnerReferenceNo" in resp.json()["responseMessage"]

    def test_404_when_ref_unknown(self, monkeypatch, client):
        client_, app = client
        _patch_db(app, monkeypatch)
        _stub_resolve(monkeypatch)  # (None, None, None)
        _stub_query_qris(monkeypatch)

        resp = client_.post(
            "/webhooks/dipay/qris",
            content=_qris_body("q-ghost", "00"),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 404
        assert resp.json() == {"responseCode": "4045201", "responseMessage": "Transaction Not Found"}

    def test_404_when_query_api_says_not_found(self, monkeypatch, client):
        """HTTP 404 from the QRIS query → refuse the callback."""
        client_, app = client
        brand = StubBrand()
        order = StubOrder(id="ord-1")
        _patch_db(app, monkeypatch)
        _stub_resolve(monkeypatch, brand=brand, order=order)
        _stub_query_qris(
            monkeypatch,
            raises=DipayError(404, {"responseCode": "4045101",
                                    "responseMessage": "Not Found"}),
        )

        resp = client_.post(
            "/webhooks/dipay/qris",
            content=_qris_body("q-forged", "00"),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 404
        assert resp.json()["responseCode"] == "4045201"

    def test_404_on_snap_business_not_found_code(self, monkeypatch, client):
        """A 4045100-style SNAP responseCode (non-404 HTTP) also refuses."""
        client_, app = client
        brand = StubBrand()
        order = StubOrder(id="ord-1")
        _patch_db(app, monkeypatch)
        _stub_resolve(monkeypatch, brand=brand, order=order)
        _stub_query_qris(
            monkeypatch,
            raises=DipayError(400, {"responseCode": "4045100"}),
        )

        resp = client_.post(
            "/webhooks/dipay/qris",
            content=_qris_body("q-forged", "00"),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 404

    def test_proceeds_with_body_status_on_transient_query_error(
        self, monkeypatch, client,
    ):
        """Non-404 DipayError from the query → proceed with the body
        status (defensive: webhooks are advisory, the query may be down)."""
        client_, app = client
        brand = StubBrand()
        cart = StubCart(payment_state="pending", order_id="order-abc")
        _patch_db(app, monkeypatch, None)
        _stub_resolve(monkeypatch, brand=brand, cart=cart)
        _stub_query_qris(
            monkeypatch,
            raises=DipayError(500, {"responseMessage": "boom"}),
        )

        resp = client_.post(
            "/webhooks/dipay/qris",
            content=_qris_body("q-cart-ref-1", "00"),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        assert cart.payment_state == "paid"


# ---- QRIS callback — mock-invoice recovery ----------------------------------


class TestMockInvoiceRecovery:
    def test_dipay_dev_ref_recovers_order_via_snapshot(self, monkeypatch, client):
        """Mock-mode invoice ids (``dipay-dev-{order_id}``) recover via the
        order snapshot when no cart / order row matched the resolver."""
        client_, app = client
        brand = StubBrand()
        order = StubOrder(id="order-recovered", total_idr=100_000)
        _patch_db(app, monkeypatch, order)
        _stub_resolve(monkeypatch, brand=brand)
        _stub_query_qris(monkeypatch)

        captured: dict = {}
        _stub_mark_paid(monkeypatch, captured)

        resp = client_.post(
            "/webhooks/dipay/qris",
            content=_qris_body("dipay-dev-order-recovered", "00"),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["responseCode"] == "2005200"
        assert captured["order_id"] == "order-recovered"
        assert captured["actor"] == "system:dipay_webhook"
        assert resp.json()["state"] == "ESCROW_HELD"


# ---- Remit callback ---------------------------------------------------------


class TestRemitCallback:
    def test_success_flips_ledger_row_and_appends_receipt(self, monkeypatch, client):
        client_, app = client
        row = _stub_ledger_row(partner_ref="r-abc123", order_id="order-1")
        order = StubOrder(id="order-1", brand_id="brand-1")
        brand = StubBrand(id="brand-1")
        _patch_db(app, monkeypatch, row, order, brand)
        _stub_disbursement_status(
            monkeypatch,
            returns={
                "latestTransactionStatus": "00",
                "originalReferenceNo": "DIPAY-REF-9",
                "additionalInfo": {
                    "receiptUrl": "https://receipt.dipay.id/abc",
                },
            },
        )

        resp = client_.post(
            "/webhooks/dipay/remit",
            content=json.dumps({
                "originalPartnerReferenceNo": "r-abc123",
                "originalReferenceNo": "DIPAY-REF-9",
                "latestTransactionStatus": "00",
                "additionalInfo": {},
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["responseCode"] == "2003600"
        assert out["ok"] is True
        assert out["status"] == "COMPLETED"
        from models.escrow_ledger import EscrowEntryStatus
        assert row.status == EscrowEntryStatus.COMPLETED
        assert row.external_ref == "DIPAY-REF-9"
        assert "receipt: https://receipt.dipay.id/abc" in (row.description or "")

    def test_failure_marks_row_failed_with_reason(self, monkeypatch, client):
        client_, app = client
        row = _stub_ledger_row(partner_ref="r-abc123", order_id="order-1")
        order = StubOrder(id="order-1", brand_id="brand-1")
        brand = StubBrand(id="brand-1")
        _patch_db(app, monkeypatch, row, order, brand)
        _stub_disbursement_status(
            monkeypatch,
            returns={
                "latestTransactionStatus": "06",
                "additionalInfo": {"failedReason": "Account closed"},
            },
        )

        resp = client_.post(
            "/webhooks/dipay/remit",
            content=json.dumps({
                "originalPartnerReferenceNo": "r-abc123",
                "latestTransactionStatus": "06",
                "additionalInfo": {"failedReason": "Account closed"},
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["responseCode"] == "2003600"
        assert out["status"] == "FAILED"
        from models.escrow_ledger import EscrowEntryStatus
        assert row.status == EscrowEntryStatus.FAILED
        assert "failed: Account closed" in (row.description or "")

    def test_paying_keeps_row_pending_and_refreshes_external_ref(
        self, monkeypatch, client,
    ):
        client_, app = client
        row = _stub_ledger_row(partner_ref="r-abc123", order_id="order-1")
        order = StubOrder(id="order-1", brand_id="brand-1")
        brand = StubBrand(id="brand-1")
        _patch_db(app, monkeypatch, row, order, brand)
        _stub_disbursement_status(
            monkeypatch,
            returns={
                "latestTransactionStatus": "02",
                "originalReferenceNo": "DIPAY-REF-2",
            },
        )

        resp = client_.post(
            "/webhooks/dipay/remit",
            content=json.dumps({
                "originalPartnerReferenceNo": "r-abc123",
                "latestTransactionStatus": "02",
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["responseCode"] == "2003600"
        assert out["status"] == "pending"
        from models.escrow_ledger import EscrowEntryStatus
        assert row.status == EscrowEntryStatus.PENDING
        assert row.external_ref == "DIPAY-REF-2"

    def test_body_status_used_when_status_api_transiently_fails(
        self, monkeypatch, client,
    ):
        client_, app = client
        row = _stub_ledger_row(partner_ref="r-abc123", order_id="order-1")
        order = StubOrder(id="order-1", brand_id="brand-1")
        brand = StubBrand(id="brand-1")
        _patch_db(app, monkeypatch, row, order, brand)
        _stub_disbursement_status(
            monkeypatch,
            raises=DipayError(503, {"responseMessage": "upstream down"}),
        )

        resp = client_.post(
            "/webhooks/dipay/remit",
            content=json.dumps({
                "originalPartnerReferenceNo": "r-abc123",
                "originalReferenceNo": "DIPAY-REF-8",
                "latestTransactionStatus": "06",
                "additionalInfo": {"failedReason": "Insufficient balance"},
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["responseCode"] == "2003600"
        assert out["status"] == "FAILED"
        assert "failed: Insufficient balance" in (row.description or "")

    def test_400_on_missing_partner_ref(self, monkeypatch, client):
        client_, app = client
        _patch_db(app, monkeypatch)

        resp = client_.post(
            "/webhooks/dipay/remit",
            content=json.dumps({"latestTransactionStatus": "00"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_404_when_no_ledger_row(self, monkeypatch, client):
        client_, app = client
        _patch_db(app, monkeypatch, None)
        _stub_disbursement_status(monkeypatch)

        resp = client_.post(
            "/webhooks/dipay/remit",
            content=json.dumps({
                "originalPartnerReferenceNo": "r-ghost",
                "latestTransactionStatus": "00",
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 404
        assert "unknown dipay disbursement" in resp.json()["detail"].lower()


# ---- Inbound Webhook Signature Tests ---------------------------------------


class TestDipayWebhookSignature:
    # /v1.0/qr/qr-mpm-notify is Dipay's documented SNAP path; Dipay signs
    # whichever path it posts to.
    @pytest.mark.parametrize("path", ["/webhooks/dipay/qris", "/v1.0/qr/qr-mpm-notify"])
    def test_signature_verified_when_key_configured(self, monkeypatch, client, path):
        from config import settings
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")

        monkeypatch.setattr(settings, "dipay_callback_public_key", pub_pem, raising=False)
        monkeypatch.setattr(settings, "environment", "development")

        client_, app = client
        brand = StubBrand()
        order = StubOrder(id="ord-1", total_idr=100_000)
        _patch_db(app, monkeypatch)
        _stub_resolve(monkeypatch, brand=brand, order=order)
        _stub_query_qris(monkeypatch)
        _stub_mark_paid(monkeypatch)

        body_bytes = _qris_body("q0f0e8a2e123", "00")
        body_str = body_bytes.decode("utf-8")
        import hashlib
        body_hash = hashlib.sha256(body_str.encode("utf-8")).hexdigest()
        ts = "2026-09-23T10:00:00.000+07:00"
        method = "POST"
        string_to_sign = f"{method}:{path}:{body_hash}:{ts}"
        sig = private_key.sign(
            string_to_sign.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        ).hex()

        resp = client_.post(
            path,
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "x-signature": sig,
                "x-timestamp": ts,
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["responseCode"] == "2005200"

    def test_signature_over_forwarded_full_url_is_accepted(self, monkeypatch, client):
        """Dipay may sign the public callback URL rather than the path; the
        proxy's x-forwarded-proto/host reconstruct it."""
        import base64
        import hashlib
        from config import settings
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")
        monkeypatch.setattr(settings, "dipay_callback_public_key", pub_pem, raising=False)
        monkeypatch.setattr(settings, "environment", "production")

        client_, app = client
        _patch_db(app, monkeypatch)
        _stub_resolve(monkeypatch, brand=StubBrand(), order=StubOrder(id="ord-1", total_idr=100_000))
        _stub_query_qris(monkeypatch)
        _stub_mark_paid(monkeypatch)

        path = "/v1.0/qr/qr-mpm-notify"
        body_bytes = _qris_body("q0f0e8a2e123", "00")
        ts = "2026-10-28T06:08:09+07:00"
        signed = (
            f"POST:https://api.example.test{path}:"
            f"{hashlib.sha256(body_bytes).hexdigest()}:{ts}"
        )
        sig = base64.b64encode(
            private_key.sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256())
        ).decode()

        resp = client_.post(
            path,
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "x-signature": sig,
                "x-timestamp": ts,
                "x-forwarded-proto": "https",
                "x-forwarded-host": "api.example.test",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["responseCode"] == "2005200"

    def test_invalid_signature_raises_401(self, monkeypatch, client):
        from config import settings
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")

        monkeypatch.setattr(settings, "dipay_callback_public_key", pub_pem, raising=False)
        monkeypatch.setattr(settings, "environment", "development")

        client_, app = client
        _patch_db(app, monkeypatch)

        resp = client_.post(
            "/webhooks/dipay/qris",
            content=_qris_body("q0f0e8a2e123", "00"),
            headers={
                "Content-Type": "application/json",
                "x-signature": "deadbeef" * 8,
                "x-timestamp": "2026-09-23T10:00:00.000+07:00",
            },
        )
        assert resp.status_code == 401
        assert resp.json() == {"responseCode": "4015200", "responseMessage": "Unauthorized. Invalid Signature"}

    def test_invalid_signature_logs_reason_without_sensitive_data(self, monkeypatch, client, caplog):
        from config import settings
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")
        monkeypatch.setattr(settings, "dipay_callback_public_key", pub_pem, raising=False)
        monkeypatch.setattr(settings, "environment", "production")

        client_, app = client
        _patch_db(app, monkeypatch)
        body = _qris_body("q-sensitive-ref", "00")
        signature = "deadbeef" * 8
        with caplog.at_level("WARNING", logger="beli_aman_bap.webhooks_dipay"):
            resp = client_.post(
                "/v1.0/qr/qr-mpm-notify",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "x-signature": signature,
                    "x-timestamp": "2026-09-23T10:00:00.000+07:00",
                    "x-partner-id": "partner-id",
                    "x-external-id": "external-id",
                    "channel-id": "DPAPI",
                },
            )
        assert resp.status_code == 401
        text = " ".join(caplog.messages)
        assert "signature_verification_mismatch" in text
        assert signature not in text
        assert body.decode() not in text
        assert "q-sensitive-ref" not in text

    def test_full_callback_debug_log_redacts_sensitive_fields(self, monkeypatch, client, caplog):
        from config import settings

        monkeypatch.setattr(settings, "dipay_callback_public_key", "invalid", raising=False)
        monkeypatch.setattr(settings, "environment", "production")
        monkeypatch.setattr(settings, "dipay_debug_log_full_callback", True, raising=False)

        client_, app = client
        _patch_db(app, monkeypatch)
        body = json.dumps({
            "originalPartnerReferenceNo": "q-debug-1",
            "customerPan": "4111111111111111",
            "merchantName": "Debug Merchant",
            "additionalInfo": {"customerPAN": "4111111111111111", "issuer": "BCA"},
        }).encode()
        with caplog.at_level("WARNING", logger="beli_aman_bap.webhooks_dipay"):
            resp = client_.post(
                "/v1.0/qr/qr-mpm-notify",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "x-signature": "c2ln",
                    "x-timestamp": "2026-09-23T10:00:00+07:00",
                    "x-customer-pan": "4111111111111111",
                },
            )
        assert resp.status_code == 401
        text = " ".join(caplog.messages)
        assert "TEMPORARY FULL DIPAY QRIS CALLBACK" in text
        assert "q-debug-1" in text
        assert "c2ln" in text
        assert "4111111111111111" not in text
        assert "Debug Merchant" in text
        assert "BCA" in text

    def test_full_callback_debug_log_disabled_by_default(self, monkeypatch, client, caplog):
        from config import settings

        monkeypatch.setattr(settings, "dipay_callback_public_key", "invalid", raising=False)
        monkeypatch.setattr(settings, "environment", "production")
        monkeypatch.setattr(settings, "dipay_debug_log_full_callback", False, raising=False)

        client_, app = client
        _patch_db(app, monkeypatch)
        with caplog.at_level("WARNING", logger="beli_aman_bap.webhooks_dipay"):
            client_.post(
                "/v1.0/qr/qr-mpm-notify",
                content=_qris_body("q-debug-2", "00"),
                headers={"x-signature": "c2ln", "x-timestamp": "2026-09-23T10:00:00+07:00"},
            )
        assert "TEMPORARY FULL DIPAY QRIS CALLBACK" not in " ".join(caplog.messages)

    def test_remit_signature_verified_when_key_configured(self, monkeypatch, client):
        from config import settings
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")

        monkeypatch.setattr(settings, "dipay_callback_public_key", pub_pem, raising=False)
        monkeypatch.setattr(settings, "environment", "development")

        client_, app = client
        row = _stub_ledger_row(partner_ref="r-abc123", order_id="order-1")
        order = StubOrder(id="order-1", brand_id="brand-1")
        brand = StubBrand(id="brand-1")
        _patch_db(app, monkeypatch, row, order, brand)
        _stub_disbursement_status(
            monkeypatch,
            returns={
                "latestTransactionStatus": "00",
                "originalReferenceNo": "DIPAY-REF-9",
            },
        )

        body_dict = {
            "originalPartnerReferenceNo": "r-abc123",
            "originalReferenceNo": "DIPAY-REF-9",
            "latestTransactionStatus": "00",
            "additionalInfo": {},
        }
        body_bytes = json.dumps(body_dict).encode()
        body_str = body_bytes.decode("utf-8")
        import hashlib
        body_hash = hashlib.sha256(body_str.encode("utf-8")).hexdigest()
        ts = "2026-09-23T10:00:00.000+07:00"
        path = "/webhooks/dipay/remit"
        method = "POST"
        string_to_sign = f"{method}:{path}:{body_hash}:{ts}"
        sig = private_key.sign(
            string_to_sign.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        ).hex()

        resp = client_.post(
            path,
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "x-signature": sig,
                "x-timestamp": ts,
            },
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["responseCode"] == "2003600"
        assert out["status"] == "COMPLETED"

    def test_missing_signature_in_production_raises_401(self, monkeypatch, client):
        from config import settings

        monkeypatch.setattr(settings, "dipay_callback_public_key", None, raising=False)
        monkeypatch.setattr(settings, "environment", "production")

        client_, app = client
        _patch_db(app, monkeypatch)

        resp = client_.post(
            "/webhooks/dipay/qris",
            content=_qris_body("q0f0e8a2e123", "00"),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 401
        assert resp.json() == {"responseCode": "4015200", "responseMessage": "Unauthorized. Invalid Signature"}
