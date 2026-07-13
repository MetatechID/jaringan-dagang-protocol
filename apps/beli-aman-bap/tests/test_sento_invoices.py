"""Sento invoice module — pure unit tests.

Covers ``services/sento_invoices.py`` (mock-mode + real-path dispatch,
vendor-neutral cart columns, snapshot persistence for orders).

DB and HTTP layer fakes are imported from ``tests/_sento_fakes``. We
monkeypatch ``sento_client.create_invoice`` so no network is hit.

Mirrors ``tests/test_oy_invoices.py`` shape, with the Sento API surface:
- ``create_invoice`` returns ``{status, url, payment_link_id}``
- ``partner_tx_id`` is the Sento equivalent of OY's ``external_id``
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

import pytest
from fastapi import HTTPException

from services import sento_invoices  # noqa: E402
from tests._sento_fakes import (  # noqa: E402
    FakeSession,
    StubBrand,
    StubOrder,
)


class _StubCart:
    """sento_invoices-specific Cart shape."""

    def __init__(self, *, total_idr: int, billing: dict | None = None, order_id=None):
        self.id = "cart-test-id"
        self.bpp_id = "safiya.bpp.jaringan-dagang.id"
        self.order_id = order_id
        self.quote_json = {"total_idr": total_idr} if total_idr else {}
        self.items_json = [{"sku_id": "SKU-1", "qty": 2}]
        self.billing_json = billing or {"email": "buyer@example.com", "name": "Buyer Test"}
        self.invoice_id = None
        self.invoice_provider = None
        self.qr_image_url = None


def _make_session(brand: SimpleNamespace) -> FakeSession:
    """Session that returns ``brand`` from any execute() call."""
    return FakeSession([brand])


class TestCreateInvoiceForCart:
    @pytest.mark.asyncio
    async def test_real_path_writes_renamed_columns(self, monkeypatch):
        """Happy path: sento_client returns a body, we persist vendor-neutral keys."""
        cart = _StubCart(total_idr=250_000)
        db = _make_session(StubBrand())

        captured: dict = {}

        async def fake_create_invoice(**kwargs):
            captured.update(kwargs)
            return {
                "status": True,
                "url": "https://pay.sento.id/x",
                "payment_link_id": "PL-1234",
            }

        monkeypatch.setattr(sento_invoices.sento_client, "create_invoice", fake_create_invoice)

        response = await sento_invoices.create_invoice_for_cart(db, cart)

        assert response["status"] is True
        assert response["url"] == "https://pay.sento.id/x"
        assert cart.invoice_id == "PL-1234"
        assert cart.invoice_provider == "sento"
        assert cart.qr_image_url == "https://pay.sento.id/x"
        # Per-Brand creds threaded through, master env fallback unused.
        assert captured["api_key"] == "brand-key"
        assert captured["username"] == "brand-user"
        assert captured["partner_tx_id"] == f"cart-{cart.id}"
        assert captured["amount_idr"] == 250_000
        assert captured["sender_name"] == "Buyer Test"

    @pytest.mark.asyncio
    async def test_real_path_falls_back_to_partner_tx_id_when_no_payment_link_id(
        self, monkeypatch
    ):
        """If Sento returns only ``partner_tx_id`` (no ``payment_link_id``), use it as invoice_id."""
        cart = _StubCart(total_idr=100_000)
        db = _make_session(StubBrand())

        async def fake_create_invoice(**_kwargs):
            return {"status": True, "url": "https://pay.sento.id/x"}

        monkeypatch.setattr(sento_invoices.sento_client, "create_invoice", fake_create_invoice)
        await sento_invoices.create_invoice_for_cart(db, cart)
        # partner_tx_id we sent → fell back to it for cart.invoice_id
        assert cart.invoice_id == f"cart-{cart.id}"

    @pytest.mark.asyncio
    async def test_mock_mode_when_brand_missing(self, monkeypatch):
        cart = _StubCart(total_idr=100_000)
        db = _make_session(brand=None)

        async def fake_create_invoice(**_kwargs):
            raise AssertionError("real Sento must NOT be called in mock mode")

        monkeypatch.setattr(sento_invoices.sento_client, "create_invoice", fake_create_invoice)

        response = await sento_invoices.create_invoice_for_cart(db, cart)
        assert response["mock"] is True
        assert cart.invoice_id == f"sento-dev-{cart.id}"
        assert cart.invoice_provider == "sento"
        assert cart.qr_image_url.endswith(f"/api/mock-checkout/{cart.invoice_id}")

    @pytest.mark.asyncio
    async def test_mock_mode_when_brand_provider_not_sento(self, monkeypatch):
        cart = _StubCart(total_idr=50_000)
        db = _make_session(StubBrand(payment_provider="xendit"))

        async def fake_create_invoice(**_kwargs):
            raise AssertionError("real Sento must NOT fire when provider=xendit")

        monkeypatch.setattr(sento_invoices.sento_client, "create_invoice", fake_create_invoice)

        response = await sento_invoices.create_invoice_for_cart(db, cart)
        assert response["mock"] is True
        assert cart.invoice_id.startswith("sento-dev-")

    @pytest.mark.asyncio
    async def test_real_path_raises_http_409_when_amount_zero(self, monkeypatch):
        """Real-path guard: a 0-total cart must not silently create an invoice."""
        cart = _StubCart(total_idr=0)
        db = _make_session(StubBrand())

        async def fake_create_invoice(**_kwargs):
            raise AssertionError("real Sento must NOT fire when amount is 0")

        monkeypatch.setattr(sento_invoices.sento_client, "create_invoice", fake_create_invoice)

        with pytest.raises(HTTPException) as ei:
            await sento_invoices.create_invoice_for_cart(db, cart)
        assert ei.value.status_code == 409

    @pytest.mark.asyncio
    async def test_mock_invoice_id_uses_order_id_when_set(self, monkeypatch):
        cart = _StubCart(total_idr=10_000, order_id="ord-from-buyer")
        db = _make_session(brand=None)

        async def fake_create_invoice(**_kwargs):
            raise AssertionError("mock mode → no Sento call")

        monkeypatch.setattr(sento_invoices.sento_client, "create_invoice", fake_create_invoice)

        response = await sento_invoices.create_invoice_for_cart(db, cart)
        assert response["mock"] is True
        assert cart.invoice_id == "sento-dev-ord-from-buyer"

    @pytest.mark.asyncio
    async def test_payer_fields_use_billing_fallbacks(self, monkeypatch):
        cart = _StubCart(
            total_idr=100_000,
            billing={"contact_email": "fb@example.com", "display_name": "Fallback"},
        )
        db = _make_session(StubBrand())

        captured: dict = {}

        async def fake_create_invoice(**kwargs):
            captured.update(kwargs)
            return {"status": True, "url": "u", "payment_link_id": "P"}

        monkeypatch.setattr(sento_invoices.sento_client, "create_invoice", fake_create_invoice)
        await sento_invoices.create_invoice_for_cart(db, cart)
        assert captured["email"] == "fb@example.com"
        assert captured["sender_name"] == "Fallback"


class TestCreateInvoiceForOrder:
    @pytest.mark.asyncio
    async def test_writes_generic_snapshot_keys(self, monkeypatch):
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder()

        async def fake_create_invoice(**_kwargs):
            return {
                "status": True,
                "url": "https://pay.sento.id/ord",
                "payment_link_id": "PL-9999",
            }

        monkeypatch.setattr(sento_invoices.sento_client, "create_invoice", fake_create_invoice)

        response = await sento_invoices.create_invoice_for_order(db, order)

        assert response["payment_link_id"] == "PL-9999"
        snap = order.payment_method_snapshot
        assert snap["type"] == "sento_invoice"
        assert snap["payment_provider"] == "sento"
        assert snap["invoice_id"] == "PL-9999"
        assert snap["invoice_url"] == "https://pay.sento.id/ord"

    @pytest.mark.asyncio
    async def test_real_path_collapses_items_into_description(self, monkeypatch):
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder(items=[
            {"name": "Kopi", "qty": 2, "unit_price_idr": 25_000},
            {"name": "Gula", "qty": 1, "unit_price_idr": 15_000},
        ])

        captured: dict = {}

        async def fake_create_invoice(**kwargs):
            captured.update(kwargs)
            return {"status": True, "url": "u", "payment_link_id": "P"}

        monkeypatch.setattr(sento_invoices.sento_client, "create_invoice", fake_create_invoice)
        await sento_invoices.create_invoice_for_order(db, order)
        # Items folded into description (Sento's create endpoint doesn't take items).
        assert "Kopi x2" in captured["description"]
        assert "Gula x1" in captured["description"]

    @pytest.mark.asyncio
    async def test_real_path_partner_tx_id_starts_with_order(self, monkeypatch):
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder(id="order-abc")

        captured: dict = {}

        async def fake_create_invoice(**kwargs):
            captured.update(kwargs)
            return {"status": True, "url": "u", "payment_link_id": "P"}

        monkeypatch.setattr(sento_invoices.sento_client, "create_invoice", fake_create_invoice)
        await sento_invoices.create_invoice_for_order(db, order)
        assert captured["partner_tx_id"] == "order-order-abc"


class TestMockModeMatrix:
    """`_mock_mode` truth table: True when brand is None, OR provider != 'sento',
    OR no key (env or per-Brand). Otherwise real-path."""

    @pytest.mark.asyncio
    async def test_mock_when_brand_is_none(self, monkeypatch):
        cart = _StubCart(total_idr=10_000)
        db = _make_session(brand=None)

        async def fake_create_invoice(**_kwargs):
            raise AssertionError("mock-mode: no Sento call")

        monkeypatch.setattr(sento_invoices.sento_client, "create_invoice", fake_create_invoice)
        response = await sento_invoices.create_invoice_for_cart(db, cart)
        assert response["mock"] is True

    @pytest.mark.asyncio
    async def test_real_when_provider_sento_and_brand_key_only(self, monkeypatch):
        cart = _StubCart(total_idr=10_000)
        db = _make_session(StubBrand(sento_api_key="brand-only"))

        called = []

        async def fake_create_invoice(**kwargs):
            called.append(kwargs)
            return {"status": True, "url": "u", "payment_link_id": "P"}

        monkeypatch.setattr(sento_invoices.sento_client, "create_invoice", fake_create_invoice)
        response = await sento_invoices.create_invoice_for_cart(db, cart)
        assert response.get("mock") is not True
        assert len(called) == 1
        assert called[0]["api_key"] == "brand-only"

    @pytest.mark.asyncio
    async def test_real_when_provider_sento_and_env_key_only(self, monkeypatch):
        cart = _StubCart(total_idr=10_000)
        db = _make_session(StubBrand(sento_api_key=""))

        called = []

        async def fake_create_invoice(**kwargs):
            called.append(kwargs)
            return {"status": True, "url": "u", "payment_link_id": "P"}

        fake_settings = SimpleNamespace(
            sento_api_key="env-only",
            sento_default_username="env-user",
            sento_callback_base_url="http://x",
            mock_checkout_public_base="http://x",
        )
        monkeypatch.setattr(sento_invoices, "settings", fake_settings)
        monkeypatch.setattr(sento_invoices.sento_client, "create_invoice", fake_create_invoice)
        response = await sento_invoices.create_invoice_for_cart(db, cart)
        assert response.get("mock") is not True
        assert len(called) == 1

    @pytest.mark.asyncio
    async def test_mock_when_provider_sento_but_neither_key(self, monkeypatch):
        cart = _StubCart(total_idr=10_000)
        db = _make_session(StubBrand(sento_api_key=""))

        async def fake_create_invoice(**_kwargs):
            raise AssertionError("mock-mode: no Sento call")

        fake_settings = SimpleNamespace(
            sento_api_key="",
            sento_default_username="",
            sento_callback_base_url="http://x",
            mock_checkout_public_base="http://x",
        )
        monkeypatch.setattr(sento_invoices, "settings", fake_settings)
        monkeypatch.setattr(sento_invoices.sento_client, "create_invoice", fake_create_invoice)
        response = await sento_invoices.create_invoice_for_cart(db, cart)
        assert response["mock"] is True