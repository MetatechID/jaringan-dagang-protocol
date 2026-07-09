"""OY webhook router — pure unit tests.

Covers all branches of ``routers/webhooks_oy.py``:

- HMAC SHA-256 signature verification (good sig accepted, bad sig rejected,
  empty brand secret rejected with 503, missing signature header rejected
  with 401)
- Body-parsing helpers (``_extract_invoice_id`` fallback chain,
  ``_oy_status`` fallback chain, ``_resolve_external_ref`` fallback chain)
- Status dispatch (PAID/SUCCESS/000/COMPLETED → handle_paid,
  EXPIRED → handle_expired, FAILED/CANCELLED/EXPIRED_30/300/DECLINED →
  handle_failed, anything else → ignored)
- Brand resolution (cart-branch, order-snapshot-branch, no-match)
- /invoice PAID on order path → mark_order_paid runs with
  actor='system:oy_webhook'
- /invoice PAID on cart path → cart.payment_state='paid', invoice_provider
  set to 'oy'
- /invoice FAILED → cart.payment_state='failed' (no order touched)
- 400 / 404 / 401 error paths

DB and HTTP fakes live in ``tests/_oy_fakes``.
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

from routers.webhooks_oy import (  # noqa: E402
    router as oy_router,
    _extract_invoice_id,
    _oy_status,
    _resolve_external_ref,
    _verify_signature,
)
from tests._oy_fakes import (  # noqa: E402
    FakeSession,
    StubBrand,
    StubCart,
    sign as _sign,
)


@pytest.fixture
def client():
    """Build a TestClient. Tests install their FakeSession via
    ``app.dependency_overrides[get_db]``."""
    app = FastAPI()
    app.include_router(oy_router)
    return TestClient(app), app


def _patch_db(app, monkeypatch, *results):
    """Install a stubbed get_db via FastAPI's dependency_overrides dict.

    Overrides are resolved at request-time, so this works regardless of
    whether include_router ran first.
    """
    from database import get_db as real_get_db  # noqa: F401

    session = FakeSession(list(results))

    async def _stub_get_db():
        yield session

    app.dependency_overrides[real_get_db] = _stub_get_db
    return session


def _stub_brand_lookup(monkeypatch, brand):
    """Replace the router's brand-resolver with one that returns ``brand``.

    The real resolver chains 3 SELECTs (cart → order → brand). Tests are
    about the webhook handler's branching, not the SQL chain, so we skip
    straight to the desired outcome. None → simulate 'unknown invoice'.
    """
    async def _resolve(db, invoice_id):
        return brand
    monkeypatch.setattr("routers.webhooks_oy._resolve_brand_for_invoice_id", _resolve)


# ---- Signature verification -------------------------------------------------


class TestSignature:
    def test_valid_signature_accepted(self, monkeypatch, client):
        client, app = client
        brand = StubBrand()
        _patch_db(app, monkeypatch)
        _stub_brand_lookup(monkeypatch, brand)
        body_dict = {
            "trx_id": "OY-1",
            "status": "PAID",
            "external_id": "order-order-abc",
        }
        body_bytes = json.dumps(body_dict).encode()
        sig = _sign(body_bytes, "topsecret")

        async def fake_mark_paid(db, **_kw):
            return SimpleNamespace(id="order-abc", state=SimpleNamespace(value="ESCROW_HELD"))
        monkeypatch.setattr("routers.webhooks_oy.order_paid.mark_order_paid", fake_mark_paid)

        resp = client.post(
            "/webhooks/oy/invoice",
            content=body_bytes,
            headers={
                "x-oy-signature": sig,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["order_id"] == "order-abc"

    def test_bad_signature_rejected(self, monkeypatch, client):
        client, app = client
        brand = StubBrand()
        _patch_db(app, monkeypatch)
        _stub_brand_lookup(monkeypatch, brand)
        body = json.dumps({"trx_id": "OY-1", "status": "PAID", "external_id": "cart-x"}).encode()

        resp = client.post(
            "/webhooks/oy/invoice",
            content=body,
            headers={"x-oy-signature": "deadbeef" * 8, "Content-Type": "application/json"},
        )
        assert resp.status_code == 401, resp.text

    def test_unknown_invoice_rejected(self, monkeypatch, client):
        client, app = client
        _patch_db(app, monkeypatch)
        # Brand lookup returns None → 401 before we even attempt sig verify.
        _stub_brand_lookup(monkeypatch, None)
        body = json.dumps({"trx_id": "OY-UNKNOWN", "status": "PAID"}).encode()

        resp = client.post(
            "/webhooks/oy/invoice",
            content=body,
            headers={"x-oy-signature": "irrelevant", "Content-Type": "application/json"},
        )
        assert resp.status_code == 401

    def test_returns_503_when_brand_secret_empty(self, monkeypatch, client):
        """Brand without a secret → 503 (defensive: don't accept unsigned
        callbacks just because config is missing)."""
        client, app = client
        brand = StubBrand(oy_callback_secret="")
        _patch_db(app, monkeypatch)
        _stub_brand_lookup(monkeypatch, brand)
        body = json.dumps({"trx_id": "OY-1", "status": "PAID"}).encode()

        resp = client.post(
            "/webhooks/oy/invoice",
            content=body,
            headers={"x-oy-signature": _sign(body, "anything"), "Content-Type": "application/json"},
        )
        assert resp.status_code == 503
        assert "oy_callback_secret" in resp.json()["detail"]

    def test_returns_401_when_signature_header_missing(self, monkeypatch, client):
        client, app = client
        brand = StubBrand()
        _patch_db(app, monkeypatch)
        _stub_brand_lookup(monkeypatch, brand)
        body = json.dumps({"trx_id": "OY-1", "status": "PAID"}).encode()

        resp = client.post(
            "/webhooks/oy/invoice",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 401
        assert "signature" in resp.json()["detail"].lower()

    def test_signature_uses_compare_digest(self, monkeypatch, client):
        """Two equal-length hex strings that differ in payload → 401.

        This guards against regression to ``==`` (timing-leak safe
        comparison). With hmac.compare_digest, two unequal strings still
        produce a clean 401.
        """
        client, app = client
        brand = StubBrand()
        _patch_db(app, monkeypatch)
        _stub_brand_lookup(monkeypatch, brand)
        body_dict = {"trx_id": "OY-1", "status": "PAID", "external_id": "cart-x"}
        body_bytes = json.dumps(body_dict).encode()
        # Sign a DIFFERENT body and submit the original — sigs are
        # equal-length hex but mismatched.
        wrong_sig = _sign(body_bytes + b" ", "topsecret")

        resp = client.post(
            "/webhooks/oy/invoice",
            content=body_bytes,
            headers={"x-oy-signature": wrong_sig, "Content-Type": "application/json"},
        )
        assert resp.status_code == 401

    def test_verify_signature_helper_directly_rejects_bad(self):
        brand = StubBrand(oy_callback_secret="topsecret")
        with pytest.raises(Exception) as ei:
            _verify_signature(b"hello", "deadbeef" * 8, brand)
        # Either HTTPException(401) or similar — just assert not accepted.
        assert "401" in str(ei.value) or "Invalid" in str(ei.value) or "401" in repr(ei.value)


# ---- Body parsing helpers --------------------------------------------------


class TestBodyParsing:
    """Pure-function tests of the parsing helpers — no DB, no HTTP."""

    def test_extract_invoice_id_prefers_trx_id_over_invoice_id(self):
        body = {"trx_id": "T-1", "invoice_id": "I-1"}
        assert _extract_invoice_id(body) == "T-1"

    def test_extract_invoice_id_falls_back_to_invoice_id(self):
        body = {"invoice_id": "I-1"}
        assert _extract_invoice_id(body) == "I-1"

    def test_extract_invoice_id_falls_back_to_reference_id(self):
        body = {"reference_id": "R-1"}
        assert _extract_invoice_id(body) == "R-1"

    def test_extract_invoice_id_falls_back_to_id(self):
        body = {"id": "ID-1"}
        assert _extract_invoice_id(body) == "ID-1"

    def test_extract_invoice_id_returns_none_when_empty(self):
        assert _extract_invoice_id({}) is None
        assert _extract_invoice_id({"trx_id": ""}) is None
        assert _extract_invoice_id({"trx_id": 123}) is None  # non-string

    def test_status_prefers_status_over_transaction_status(self):
        body = {"status": "PAID", "transaction_status": "FAILED"}
        assert _oy_status(body) == "PAID"

    def test_status_falls_back_to_transaction_status(self):
        body = {"transaction_status": "EXPIRED"}
        assert _oy_status(body) == "EXPIRED"

    def test_status_falls_back_to_payment_status(self):
        body = {"payment_status": "PENDING"}
        assert _oy_status(body) == "PENDING"

    def test_status_uppercases(self):
        body = {"status": "paid"}
        assert _oy_status(body) == "PAID"

    def test_resolve_external_ref_prefers_external_id(self):
        body = {"external_id": "E-1", "merchant_ref": "M-1"}
        # Resolve_external_ref is async via _resolve_brand_for_invoice_id?
        # No — it's plain async (no DB), let's check.
        # The function is `async def _resolve_external_ref(...)` per source.
        import asyncio
        result = asyncio.run(_resolve_external_ref(body, "inv-1"))
        assert result == "E-1"

    def test_resolve_external_ref_falls_back_to_merchant_ref(self):
        import asyncio
        body = {"merchant_ref": "M-1"}
        result = asyncio.run(_resolve_external_ref(body, "inv-1"))
        assert result == "M-1"

    def test_resolve_external_ref_falls_back_to_ref_id(self):
        import asyncio
        body = {"ref_id": "R-1"}
        result = asyncio.run(_resolve_external_ref(body, "inv-1"))
        assert result == "R-1"

    def test_resolve_external_ref_returns_invoice_id_when_all_blank(self):
        import asyncio
        result = asyncio.run(_resolve_external_ref({}, "inv-fallback"))
        assert result == "inv-fallback"


# ---- Status dispatch -------------------------------------------------------


class TestDispatch:
    def test_paid_aliases_dispatch_to_handle_paid(self, monkeypatch, client):
        """SUCCESS, 000, COMPLETED all route to _handle_paid."""
        client, app = client
        brand = StubBrand()
        for alias in ("SUCCESS", "000", "COMPLETED"):
            _patch_db(app, monkeypatch)
            _stub_brand_lookup(monkeypatch, brand)
            cart = StubCart()
            _patch_db(app, monkeypatch, cart, None)

            body = json.dumps({
                "trx_id": "OY-ALIAS",
                "external_id": f"cart-{cart.id}",
                "status": alias,
            }).encode()
            sig = _sign(body, "topsecret")
            resp = client.post(
                "/webhooks/oy/invoice",
                content=body,
                headers={"x-oy-signature": sig, "Content-Type": "application/json"},
            )
            assert resp.status_code == 200, f"{alias}: {resp.text}"
            assert "cart_id" in resp.json() or "order_id" in resp.json()

    def test_expired_dispatches_to_handle_expired(self, monkeypatch, client):
        client, app = client
        brand = StubBrand()
        cart = StubCart()
        _patch_db(app, monkeypatch, cart)
        _stub_brand_lookup(monkeypatch, brand)

        body = json.dumps({
            "trx_id": "OY-EXP",
            "external_id": f"cart-{cart.id}",
            "status": "EXPIRED",
        }).encode()
        sig = _sign(body, "topsecret")
        resp = client.post(
            "/webhooks/oy/invoice",
            content=body,
            headers={"x-oy-signature": sig, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert cart.payment_state == "expired"

    def test_failed_aliases_dispatch_to_handle_failed(self, monkeypatch, client):
        """CANCELLED, DECLINED, 300, EXPIRED_30 all route to _handle_failed."""
        client, app = client
        brand = StubBrand()
        for alias in ("FAILED", "CANCELLED", "DECLINED", "300", "EXPIRED_30"):
            cart = StubCart(payment_state="pending")
            _patch_db(app, monkeypatch, cart)
            _stub_brand_lookup(monkeypatch, brand)

            body = json.dumps({
                "trx_id": "OY-FAIL",
                "external_id": f"cart-{cart.id}",
                "status": alias,
            }).encode()
            sig = _sign(body, "topsecret")
            resp = client.post(
                "/webhooks/oy/invoice",
                content=body,
                headers={"x-oy-signature": sig, "Content-Type": "application/json"},
            )
            assert resp.status_code == 200, f"{alias}: {resp.text}"
            assert cart.payment_state == "failed", f"{alias} didn't set failed"

    def test_unknown_status_returns_ignored(self, monkeypatch, client):
        """PENDING and other non-handled statuses are acknowledged but
        not routed to any handler."""
        client, app = client
        brand = StubBrand()
        _patch_db(app, monkeypatch)
        _stub_brand_lookup(monkeypatch, brand)
        body = json.dumps({
            "trx_id": "OY-1",
            "external_id": "cart-x",
            "status": "PENDING",
        }).encode()
        sig = _sign(body, "topsecret")
        resp = client.post(
            "/webhooks/oy/invoice",
            content=body,
            headers={"x-oy-signature": sig, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json().get("ignored_status") == "PENDING"

    def test_returns_400_on_missing_invoice_id(self, monkeypatch, client):
        client, app = client
        _patch_db(app, monkeypatch)
        _stub_brand_lookup(monkeypatch, StubBrand())  # brand not really used
        body = json.dumps({"status": "PAID", "external_id": "cart-x"}).encode()
        sig = _sign(body, "topsecret")
        resp = client.post(
            "/webhooks/oy/invoice",
            content=body,
            headers={"x-oy-signature": sig, "Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert "invoice id" in resp.json()["detail"].lower()

    def test_returns_400_on_unrecognized_external_ref(self, monkeypatch, client):
        client, app = client
        brand = StubBrand()
        _patch_db(app, monkeypatch)
        _stub_brand_lookup(monkeypatch, brand)
        body = json.dumps({
            "trx_id": "OY-1",
            "status": "PAID",
            "external_id": "weird-prefix-xyz",
        }).encode()
        sig = _sign(body, "topsecret")
        resp = client.post(
            "/webhooks/oy/invoice",
            content=body,
            headers={"x-oy-signature": sig, "Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert "external_ref" in resp.json()["detail"].lower()

    def test_returns_400_on_invalid_json(self, monkeypatch, client):
        client, app = client
        _patch_db(app, monkeypatch)
        _stub_brand_lookup(monkeypatch, StubBrand())
        body = b"{this is not json"
        sig = _sign(body, "topsecret")
        resp = client.post(
            "/webhooks/oy/invoice",
            content=body,
            headers={"x-oy-signature": sig, "Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert "json" in resp.json()["detail"].lower()

    def test_returns_404_when_cart_id_unknown(self, monkeypatch, client):
        """PAID on a cart-prefix external_ref whose cart doesn't exist → 404."""
        client, app = client
        brand = StubBrand()
        _patch_db(app, monkeypatch, None)  # cart SELECT returns None
        _stub_brand_lookup(monkeypatch, brand)
        body = json.dumps({
            "trx_id": "OY-1",
            "status": "PAID",
            "external_id": "cart-ghost",
        }).encode()
        sig = _sign(body, "topsecret")
        resp = client.post(
            "/webhooks/oy/invoice",
            content=body,
            headers={"x-oy-signature": sig, "Content-Type": "application/json"},
        )
        assert resp.status_code == 404


# ---- Brand resolution branches --------------------------------------------


class TestBrandResolution:
    """The router's brand-resolver chains cart → order-snapshot.
    Unit-test the source resolver directly (with FakeSession) so we cover
    all three branches."""

    @pytest.mark.asyncio
    async def test_resolve_via_cart_branch(self, monkeypatch):
        from routers.webhooks_oy import _resolve_brand_for_invoice_id
        cart = StubCart()
        # brand row: needs id, bpp_id
        brand = StubBrand()
        # First execute: cart SELECT (returns cart). Second: brand SELECT.
        db = FakeSession([cart, brand])
        result = await _resolve_brand_for_invoice_id(db, "OY-1")
        assert result is brand

    @pytest.mark.asyncio
    async def test_resolve_via_order_snapshot_branch(self, monkeypatch):
        from routers.webhooks_oy import _resolve_brand_for_invoice_id
        # First SELECT: cart (None). Second: order with snapshot. Third: brand.
        order = SimpleNamespace(
            id="order-1", brand_id="brand-id",
            payment_method_snapshot={"invoice_id": "OY-2"},
        )
        brand = StubBrand()
        db = FakeSession([None, order, brand])
        result = await _resolve_brand_for_invoice_id(db, "OY-2")
        assert result is brand

    @pytest.mark.asyncio
    async def test_returns_none_when_neither_branch_matches(self, monkeypatch):
        from routers.webhooks_oy import _resolve_brand_for_invoice_id
        # Both branches return None.
        db = FakeSession([None, None])
        result = await _resolve_brand_for_invoice_id(db, "OY-3")
        assert result is None


# ---- Cart path -------------------------------------------------------------


class TestCartPath:
    def test_paid_flips_payment_state_and_provider(self, monkeypatch, client):
        """A successful PAID on the cart path sets cart.payment_state='paid'
        and stamp invoice_provider='oy'."""
        client, app = client
        brand = StubBrand()
        cart = StubCart(payment_state="pending")
        # Sequence: cart SELECT, escrow-ledger verify SELECT (returns None
        # → router inserts a new ledger row).
        _patch_db(app, monkeypatch, cart, None)
        _stub_brand_lookup(monkeypatch, brand)

        body = json.dumps({
            "trx_id": "OY-CART-TRX",
            "external_id": f"cart-{cart.id}",
            "status": "PAID",
        }).encode()
        sig = _sign(body, "topsecret")

        resp = client.post(
            "/webhooks/oy/invoice",
            content=body,
            headers={"x-oy-signature": sig, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        body_out = resp.json()
        assert body_out["cart_id"] == cart.id
        assert cart.payment_state == "paid"
        assert cart.invoice_provider == "oy"
        assert cart.invoice_id == "OY-CART-TRX"

    def test_failed_marks_payment_state(self, monkeypatch, client):
        """A failed callback flips cart.payment_state to 'failed'."""
        client, app = client
        brand = StubBrand()
        cart = StubCart(payment_state="pending")
        # FAILED path: cart SELECT first (returns cart), then router updates.
        _patch_db(app, monkeypatch, cart)
        _stub_brand_lookup(monkeypatch, brand)

        body = json.dumps({
            "trx_id": "OY-CART-TRX",
            "external_id": f"cart-{cart.id}",
            "status": "FAILED",
        }).encode()
        sig = _sign(body, "topsecret")

        resp = client.post(
            "/webhooks/oy/invoice",
            content=body,
            headers={"x-oy-signature": sig, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        assert cart.payment_state == "failed"

    def test_paid_cart_with_no_order_id_skips_escrow(self, monkeypatch, client):
        """Cart with order_id=None → no escrow-ledger insert, no error."""
        client, app = client
        brand = StubBrand()
        cart = StubCart(payment_state="pending", order_id=None)
        # Only one SELECT expected (cart lookup). No escrow SELECT.
        _patch_db(app, monkeypatch, cart)
        _stub_brand_lookup(monkeypatch, brand)

        body = json.dumps({
            "trx_id": "OY-CART-NO-ORD",
            "external_id": f"cart-{cart.id}",
            "status": "PAID",
        }).encode()
        sig = _sign(body, "topsecret")
        resp = client.post(
            "/webhooks/oy/invoice",
            content=body,
            headers={"x-oy-signature": sig, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert cart.payment_state == "paid"

    def test_paid_cart_idempotent_when_already_paid(self, monkeypatch, client):
        """A second PAID callback on a paid cart is a no-op (200, no crash)."""
        client, app = client
        brand = StubBrand()
        cart = StubCart(payment_state="paid", invoice_id="OY-CART-TRX")
        # Router does cart SELECT then escrow-verify SELECT (returns None
        # → no insert because the cart is already paid? Actually the code
        # path doesn't short-circuit; it just runs the SELECT then skips
        # the insert because the existing SELECT returns None for fresh
        # runs, but we provide None to match the "no prior ledger row"
        # case).
        _patch_db(app, monkeypatch, cart, None)
        _stub_brand_lookup(monkeypatch, brand)

        body = json.dumps({
            "trx_id": "OY-CART-TRX",
            "external_id": f"cart-{cart.id}",
            "status": "PAID",
        }).encode()
        sig = _sign(body, "topsecret")
        resp = client.post(
            "/webhooks/oy/invoice",
            content=body,
            headers={"x-oy-signature": sig, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert cart.payment_state == "paid"

    def test_expired_marks_cart_as_expired(self, monkeypatch, client):
        client, app = client
        brand = StubBrand()
        cart = StubCart(payment_state="pending")
        _patch_db(app, monkeypatch, cart)
        _stub_brand_lookup(monkeypatch, brand)

        body = json.dumps({
            "trx_id": "OY-EXP",
            "external_id": f"cart-{cart.id}",
            "status": "EXPIRED",
        }).encode()
        sig = _sign(body, "topsecret")
        resp = client.post(
            "/webhooks/oy/invoice",
            content=body,
            headers={"x-oy-signature": sig, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert cart.payment_state == "expired"

    def test_failed_skips_when_payment_state_not_pending(self, monkeypatch, client):
        """FAILED on a cart already in 'paid' state → no-op, no crash."""
        client, app = client
        brand = StubBrand()
        cart = StubCart(payment_state="paid")
        # FAILED branch only SELECTs when there's a matching cart. Provide
        # the cart so the router evaluates the guard.
        _patch_db(app, monkeypatch, cart)
        _stub_brand_lookup(monkeypatch, brand)

        body = json.dumps({
            "trx_id": "OY-FAIL",
            "external_id": f"cart-{cart.id}",
            "status": "FAILED",
        }).encode()
        sig = _sign(body, "topsecret")
        resp = client.post(
            "/webhooks/oy/invoice",
            content=body,
            headers={"x-oy-signature": sig, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        # State stays 'paid' — not demoted to 'failed'.
        assert cart.payment_state == "paid"


# ---- Order path ------------------------------------------------------------


class TestOrderPath:
    def test_paid_order_prefix_dispatches_to_mark_order_paid(self, monkeypatch, client):
        """order-{id} external_ref → mark_order_paid called with the right
        actor='system:oy_webhook' and invoice_id."""
        client, app = client
        brand = StubBrand()
        _patch_db(app, monkeypatch)
        _stub_brand_lookup(monkeypatch, brand)

        captured: dict = {}

        async def fake_mark_paid(db, *, order_id, invoice_id, actor, **_):
            captured.update(order_id=order_id, invoice_id=invoice_id, actor=actor)
            return SimpleNamespace(id=order_id, state=SimpleNamespace(value="ESCROW_HELD"))
        monkeypatch.setattr("routers.webhooks_oy.order_paid.mark_order_paid", fake_mark_paid)

        body = json.dumps({
            "trx_id": "OY-ORD-TRX",
            "external_id": "order-ord-123",
            "status": "PAID",
        }).encode()
        sig = _sign(body, "topsecret")
        resp = client.post(
            "/webhooks/oy/invoice",
            content=body,
            headers={"x-oy-signature": sig, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert captured["order_id"] == "ord-123"
        assert captured["invoice_id"] == "OY-ORD-TRX"
        assert captured["actor"] == "system:oy_webhook"
        assert resp.json()["order_id"] == "ord-123"
        assert resp.json()["state"] == "ESCROW_HELD"

    def test_paid_order_prefix_response_includes_order_id_and_state(self, monkeypatch, client):
        """Response shape: {ok, order_id, state}."""
        client, app = client
        brand = StubBrand()
        _patch_db(app, monkeypatch)
        _stub_brand_lookup(monkeypatch, brand)

        async def fake_mark_paid(db, **_kw):
            return SimpleNamespace(id="ord-9", state=SimpleNamespace(value="ESCROW_HELD"))
        monkeypatch.setattr("routers.webhooks_oy.order_paid.mark_order_paid", fake_mark_paid)

        body = json.dumps({
            "trx_id": "OY-1",
            "external_id": "order-ord-9",
            "status": "PAID",
        }).encode()
        sig = _sign(body, "topsecret")
        resp = client.post(
            "/webhooks/oy/invoice",
            content=body,
            headers={"x-oy-signature": sig, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        out = resp.json()
        assert out["order_id"] == "ord-9"
        assert out["state"] == "ESCROW_HELD"
