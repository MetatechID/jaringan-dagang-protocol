"""Tests for the partner API (external merchant payment registration).

Covers:
- ``require_partner`` bearer auth: 401 closed-by-default, brand matching
- Order registration: happy path (mock mode), idempotency 409, validation
- ``mark_partner_order_paid`` / ``mark_partner_order_expired`` semantics
- HMAC signature scheme (build + verify round-trip, negative case)
- Callback payload shape + sender
- Pay page: renders, public poller, mock pay-target indirection
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac as hmac_mod
import json
import os
import re
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import get_db  # noqa: E402
from routers.partner import router as partner_router  # noqa: E402
from routers.pay_page import router as pay_page_router  # noqa: E402
from services import partner_webhook  # noqa: E402
from services.dipay_client import DipayError  # noqa: E402
from services.partner_orders import (  # noqa: E402
    mark_partner_order_expired,
    mark_partner_order_paid,
)

# ---------------------------------------------------------------------------
# Fakes (pattern: tests/_dipay_fakes.py)


class FakeScalars(list):
    """Mimics SQLAlchemy's ScalarResult: iterable + first() + all()."""

    def first(self):
        return self[0] if self else None

    def all(self):
        return list(self)


class FakeExecute:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        value = self._value
        if isinstance(value, list):
            return FakeScalars(value)
        return FakeScalars([value])

    def scalar_one_or_none(self):
        if isinstance(self._value, list):
            return self._value[0] if self._value else None
        return self._value


class FakeSession:
    """Async session whose ``execute`` pops results from a queue."""

    def __init__(self, results):
        self._results = list(results)
        self.committed = False
        self.added: list = []
        self.rolled_back = False

    async def execute(self, *_args, **_kwargs):
        nxt = self._results.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return FakeExecute(nxt)

    async def commit(self):
        self.committed = True

    async def flush(self):
        return None

    async def refresh(self, _obj):
        return None

    async def rollback(self):
        self.rolled_back = True

    def add(self, obj):
        self.added.append(obj)


def stub_partner_brand(**overrides) -> SimpleNamespace:
    base = dict(
        id="brand-id",
        slug="consumerland",
        name="Consumerland",
        bpp_id="consumerland.bpp.jaringan-dagang.id",
        partner_api_key="cl-partner-key-123",
        partner_callback_url="https://consumerland.id/api/tickets/webhooks/oito",
        partner_callback_secret="cl-callback-secret-456",
        xendit_sub_account_id=None,  # mock mode by default
        payment_provider="xendit",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def stub_partner_order(**overrides) -> SimpleNamespace:
    base = dict(
        id="po-123",
        brand_id="brand-id",
        external_order_id="CL26-ABC123",
        amount_idr=1_500_000,
        description="Consumerland 2026 — 1 x Conference Presale",
        buyer={"name": "Budi", "email": "budi@example.com", "phone": "+628123456789"},
        items=[{"name": "Conference Presale", "qty": 1, "unit_price_idr": 1_500_000}],
        status="pending",
        invoice_id=None,
        invoice_provider=None,
        qris_content=None,
        expires_at=None,
        payment_url=None,
        success_url="https://consumerland.id/tickets?paid=CL26-ABC123",
        paid_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _build_partner_app(db, brand, *, override_auth=True) -> TestClient:
    from auth.partner_auth import require_partner

    app = FastAPI()
    app.include_router(partner_router)

    async def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    if override_auth:

        async def _override_brand():
            return brand

        app.dependency_overrides[require_partner] = _override_brand
    return TestClient(app, raise_server_exceptions=False)


_VALID_BODY = {
    "external_order_id": "CL26-ABC123",
    "amount_idr": 1_500_000,
    "description": "Consumerland 2026 — 1 x Conference Presale",
    "buyer": {
        "name": "Budi",
        "email": "budi@example.com",
        "phone": "+628123456789",
    },
    "items": [{"name": "Conference Presale", "qty": 1, "unit_price_idr": 1_500_000}],
    "success_url": "https://consumerland.id/tickets?paid=CL26-ABC123",
}


# ---------------------------------------------------------------------------
# require_partner auth


def test_require_partner_401_when_no_auth_header():
    # Real auth dependency (no override) — empty request must 401 before
    # anything else.
    db = FakeSession([])
    client = _build_partner_app(db, None, override_auth=False)
    resp = client.post("/api/v1/partner/orders", json=_VALID_BODY)
    assert resp.status_code == 401


def test_require_partner_401_when_key_does_not_match():
    # Real auth dependency (no override): one brand row exists, key mismatch.
    brand_row = stub_partner_brand(partner_api_key="other-key")
    db = FakeSession([[brand_row]])
    client = _build_partner_app(db, None, override_auth=False)

    resp = client.post(
        "/api/v1/partner/orders",
        headers={"Authorization": "Bearer wrong-key"},
        json=_VALID_BODY,
    )
    assert resp.status_code == 401


def test_require_partner_matches_brand_with_valid_key(monkeypatch):
    # Real auth dependency: the presented key matches the brand row.
    brand_row = stub_partner_brand()
    db = FakeSession([[brand_row], None])  # auth lookup, then external-id lookup
    captured = {}

    async def fake_create_invoice(**kwargs):
        captured.update(kwargs)
        return {"id": "inv-123", "invoice_url": "https://xendit/inv-123"}

    monkeypatch.setattr(
        "routers.partner.xendit_client", SimpleNamespace(create_invoice=fake_create_invoice)
    )
    client = _build_partner_app(db, None, override_auth=False)

    resp = client.post(
        "/api/v1/partner/orders",
        headers={"Authorization": "Bearer cl-partner-key-123"},
        json=_VALID_BODY,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"


# ---------------------------------------------------------------------------
# Order registration


def test_register_order_happy_path_mock_mode(monkeypatch):
    brand = stub_partner_brand()
    db = FakeSession([None])  # external-id lookup → no existing order
    client = _build_partner_app(db, brand)

    captured = {}

    async def fake_create_invoice(**kwargs):
        captured.update(kwargs)
        return {"id": "inv-123", "invoice_url": "https://xendit/inv-123"}

    monkeypatch.setattr("routers.partner.xendit_client", SimpleNamespace(create_invoice=fake_create_invoice))

    resp = client.post(
        "/api/v1/partner/orders",
        headers={"Authorization": "Bearer cl-partner-key-123"},
        json=_VALID_BODY,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "pending"
    assert data["amount_idr"] == 1_500_000
    assert "/pay/" in data["payment_url"]
    assert data["expires_at"] is not None
    assert len(db.added) == 1
    # Mock mode must NOT have called Xendit.
    assert captured == {}


def test_register_order_idempotency_conflict():
    brand = stub_partner_brand()
    db = FakeSession([stub_partner_order()])  # external-id lookup → existing
    client = _build_partner_app(db, brand)

    resp = client.post(
        "/api/v1/partner/orders",
        headers={"Authorization": "Bearer cl-partner-key-123"},
        json=_VALID_BODY,
    )
    assert resp.status_code == 409


def test_register_order_rejects_zero_amount():
    brand = stub_partner_brand()
    db = FakeSession([])
    client = _build_partner_app(db, brand)

    body = dict(_VALID_BODY, amount_idr=0)
    resp = client.post(
        "/api/v1/partner/orders",
        headers={"Authorization": "Bearer cl-partner-key-123"},
        json=body,
    )
    assert resp.status_code == 422


def test_register_order_rejects_bad_email():
    brand = stub_partner_brand()
    db = FakeSession([])
    client = _build_partner_app(db, brand)

    body = dict(_VALID_BODY, buyer={"name": "B", "email": "not-an-email", "phone": "+62812"})
    resp = client.post(
        "/api/v1/partner/orders",
        headers={"Authorization": "Bearer cl-partner-key-123"},
        json=body,
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# mark paid / expired


def test_mark_paid_flips_pending_to_paid():
    order = stub_partner_order()
    asyncio.run(mark_partner_order_paid(order=order, invoice_id="inv-1"))
    assert order.status == "paid"
    assert order.paid_at is not None
    assert order.invoice_id == "inv-1"


def test_mark_paid_is_idempotent():
    paid_at = datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc)
    order = stub_partner_order(status="paid", paid_at=paid_at)
    asyncio.run(mark_partner_order_paid(order=order, invoice_id="inv-2"))
    assert order.paid_at == paid_at  # unchanged
    assert order.invoice_id == "inv-2"  # backfilled on the idempotent path


def test_mark_paid_never_demotes_expired():
    order = stub_partner_order(status="expired")
    asyncio.run(mark_partner_order_paid(order=order, invoice_id="inv-3"))
    assert order.status == "expired"


def test_mark_expired_only_flips_pending():
    assert asyncio.run(mark_partner_order_expired(stub_partner_order())).status == "expired"
    paid = stub_partner_order(status="paid")
    assert asyncio.run(mark_partner_order_expired(paid)).status == "paid"


# ---------------------------------------------------------------------------
# HMAC signature


def test_signature_roundtrip():
    secret = "cl-callback-secret-456"
    body = json.dumps({"order_id": "CL26-ABC123"}, separators=(",", ":")).encode()
    t = 1728470400
    header = partner_webhook.build_signature_header(secret, body, timestamp=t)

    assert header.startswith(f"t={t},v1=")
    v1 = header.split("v1=")[1]
    expected = hmac_mod.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    assert hmac_mod.compare_digest(v1, expected)


def test_signature_negative_case():
    body = b'{"order_id":"X"}'
    header = partner_webhook.build_signature_header("secret-a", body, timestamp=1)
    v1 = header.split("v1=")[1]
    expected = hmac_mod.new(b"secret-b", b"1." + body, hashlib.sha256).hexdigest()
    assert not hmac_mod.compare_digest(v1, expected)


def test_callback_payload_shape():
    order = stub_partner_order(
        paid_at=datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc),
        invoice_id="inv-77",
    )
    payload = partner_webhook.build_callback_payload(order)
    assert payload == {
        "event": "payment.paid",
        "order_id": "CL26-ABC123",
        "status": "paid",
        "amount": 1_500_000,
        "reference": "inv-77",
        "paid_at": "2026-10-09T08:00:00+00:00",
    }


def test_send_payment_callback_success(monkeypatch):
    posted = {}

    class FakeResp:
        status_code = 200

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None, headers=None):
            posted["url"] = url
            posted["headers"] = headers
            posted["body"] = content
            return FakeResp()

    monkeypatch.setattr(partner_webhook.httpx, "AsyncClient", FakeClient)

    ok = partner_webhook.send_payment_callback(
        url="https://consumerland.id/api/tickets/webhooks/oito",
        secret="cl-callback-secret-456",
        payload={"event": "payment.paid", "order_id": "CL26-ABC123"},
    )
    assert asyncio.run(ok) is True
    assert posted["url"].endswith("/webhooks/oito")
    sig = posted["headers"]["x-oito-signature"]
    # Verify the posted signature against the posted body.
    t = int(sig.split(",")[0][2:])
    v1 = sig.split("v1=")[1]
    expected = hmac_mod.new(
        b"cl-callback-secret-456", f"{t}.".encode() + posted["body"], hashlib.sha256
    ).hexdigest()
    assert hmac_mod.compare_digest(v1, expected)


# ---------------------------------------------------------------------------
# Pay page


def _build_pay_app(db) -> TestClient:
    app = FastAPI()
    app.include_router(pay_page_router)

    async def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    return TestClient(app)


def test_pay_page_renders_for_pending_order():
    order = stub_partner_order(invoice_id="dev-partner-po-123")
    db = FakeSession([order])
    client = _build_pay_app(db)

    resp = client.get("/pay/po-123")
    assert resp.status_code == 200
    assert "Bayar pesanan" in resp.text
    assert "Jaringan Dagang" in resp.text
    assert "Rp 1.500.000" in resp.text


def test_pay_page_paid_shows_success():
    order = stub_partner_order(status="paid", invoice_id="inv-1")
    db = FakeSession([order])
    client = _build_pay_app(db)

    resp = client.get("/pay/po-123")
    assert resp.status_code == 200
    assert "Pembayaran berhasil" in resp.text


def test_pay_page_404_for_unknown_order():
    db = FakeSession([None])
    client = _build_pay_app(db)
    assert client.get("/pay/does-not-exist").status_code == 404


def test_pay_page_status_poller():
    order = stub_partner_order(status="paid", invoice_id="inv-1")
    db = FakeSession([order])
    client = _build_pay_app(db)

    resp = client.get("/pay/po-123/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "paid"
    assert data["amount_idr"] == 1_500_000
    assert data["success_url"].endswith("CL26-ABC123")


def test_pay_target_mock_mode():
    order = stub_partner_order(invoice_id="dev-partner-po-123")
    db = FakeSession([order])
    client = _build_pay_app(db)

    resp = client.get("/pay/po-123/pay-target")
    assert resp.status_code == 200
    assert resp.json()["pay_url"].endswith("/api/mock-checkout/dev-partner-po-123")


# ---------------------------------------------------------------------------
# Dipay QRIS path (Brand.payment_provider == "dipay")


def _dipay_settings() -> SimpleNamespace:
    """Complete env-level Dipay creds — same fallback shape the VM uses
    (brand has no per-brand creds, env bundle is complete)."""
    return SimpleNamespace(
        environment="production",
        dipay_base_url="https://api-b2x-demo.dipay.id/snap/v2.1",
        dipay_client_key="env-client-key",
        dipay_client_secret="env-client-secret",
        dipay_private_key_b64="",
        dipay_private_key_path="/tmp/unused.pem",
        dipay_merchant_id="M-ENV",
        qr_public_base="https://api.beli-aman.metatech.id",
        mock_checkout_public_base="",
        partner_pay_base_url="https://api.beli-aman.metatech.id",
    )


_QRIS_RESPONSE = {
    "responseCode": "2004700",
    "responseMessage": "Success",
    "referenceNo": "REF-1",
    "qrContent": "000201010212265802ID5303360",
}


def test_register_order_dipay_path_mints_qris(monkeypatch):
    from services import dipay_client as dipay_client_mod

    brand = stub_partner_brand(payment_provider="dipay")
    db = FakeSession([None])  # external-id lookup → no existing order
    client = _build_partner_app(db, brand)

    captured = {}

    async def fake_create_qris(**kwargs):
        captured.update(kwargs)
        return dict(_QRIS_RESPONSE)

    monkeypatch.setattr(dipay_client_mod, "settings", _dipay_settings())
    monkeypatch.setattr(dipay_client_mod, "create_qris", fake_create_qris)

    resp = client.post(
        "/api/v1/partner/orders",
        headers={"Authorization": "Bearer cl-partner-key-123"},
        json=_VALID_BODY,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    order = db.added[0]
    ref = order.invoice_id
    # SNAP-compliant ref, and the exact ref the PNG route serves.
    assert re.fullmatch(r"q-[A-Za-z0-9]{1,30}", ref)
    assert order.invoice_provider == "dipay"
    assert order.qris_content == _QRIS_RESPONSE["qrContent"]
    assert order.expires_at is not None
    assert order.payment_url.endswith(f"/pay/{order.id}")
    # Request shape sent to Dipay.
    assert captured["partner_reference_no"] == ref
    assert captured["amount_idr"] == 1_500_000
    assert len(captured["validity_period"]) == 25  # ISO-8601, seconds precision
    # Response shape.
    assert data["status"] == "pending"
    assert "/pay/" in data["payment_url"]
    assert data["expires_at"] is not None
    assert db.committed and not db.rolled_back


def test_register_order_dipay_failure_returns_502_and_rolls_back(monkeypatch):
    from services import dipay_client as dipay_client_mod

    brand = stub_partner_brand(payment_provider="dipay")
    db = FakeSession([None])
    client = _build_partner_app(db, brand)

    async def fake_create_qris(**_kwargs):
        raise DipayError(502, {"responseCode": "5004700", "responseMessage": "Upstream"})

    monkeypatch.setattr(dipay_client_mod, "settings", _dipay_settings())
    monkeypatch.setattr(dipay_client_mod, "create_qris", fake_create_qris)

    resp = client.post(
        "/api/v1/partner/orders",
        headers={"Authorization": "Bearer cl-partner-key-123"},
        json=_VALID_BODY,
    )
    assert resp.status_code == 502
    assert db.rolled_back and not db.committed


def test_pay_page_dipay_renders_inline_qris(monkeypatch):
    import routers.pay_page as pay_page_mod

    order = stub_partner_order(
        invoice_id="q-0f0e8a2e00000000000000000000",
        invoice_provider="dipay",
        qris_content=_QRIS_RESPONSE["qrContent"],
        expires_at=datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc),
    )
    db = FakeSession([order])
    client = _build_pay_app(db)
    monkeypatch.setattr(
        pay_page_mod, "settings",
        SimpleNamespace(qr_public_base="https://api.beli-aman.metatech.id"),
    )

    resp = client.get("/pay/po-123")
    assert resp.status_code == 200
    assert "Scan QRIS" in resp.text
    assert "/api/v1/qris/q-0f0e8a2e00000000000000000000.png" in resp.text
    # Dipay orders never show the Xendit redirect button.
    assert "Bayar Sekarang" not in resp.text


def test_qris_png_resolver_resolves_pending_partner_order():
    from routers.qris import _resolve_qr_content

    po = stub_partner_order(
        invoice_id="q-abc123",
        invoice_provider="dipay",
        qris_content=_QRIS_RESPONSE["qrContent"],
    )
    # Resolver probes orders, then carts, then partner orders.
    db = FakeSession([[], [], [po]])
    content = asyncio.run(_resolve_qr_content(db, "q-abc123"))
    assert content == _QRIS_RESPONSE["qrContent"]


def test_qris_png_resolver_hides_paid_partner_order():
    from routers.qris import _resolve_qr_content

    po = stub_partner_order(
        status="paid", invoice_id="q-abc123", invoice_provider="dipay",
        qris_content=_QRIS_RESPONSE["qrContent"],
    )
    db = FakeSession([[], [], [po]])
    assert asyncio.run(_resolve_qr_content(db, "q-abc123")) is None
