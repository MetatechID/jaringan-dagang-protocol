"""OY invoice module — pure unit tests.

Covers ``services/oy_invoices.py`` (mock-mode + real-path dispatch,
vendor-neutral cart columns, snapshot persistence for orders).

DB and HTTP layer fakes are imported from ``tests/_oy_fakes``. We
monkeypatch ``oy_client.create_invoice`` so no network is hit.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

# When pytest is invoked from the workspace root, sys.path doesn't contain
# this app dir explicitly — pytest imports us as
# `apps.beli_aman_bap.tests.test_oy_invoices` from the workspace root.
# Push the app dir onto sys.path so `from services import oy_invoices`
# resolves.
_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

import pytest
from fastapi import HTTPException

from services import oy_invoices  # noqa: E402
from tests._oy_fakes import (  # noqa: E402
    FakeSession,
    StubBrand,
    StubOrder,
)


class _StubCart:
    """oy_invoices-specific Cart shape. Kept local — these fields
    (items_json, billing_json) differ from the webhook-side StubCart."""

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
        """Happy path: oy_client returns a body, we persist vendor-neutral keys."""
        cart = _StubCart(total_idr=250_000)
        db = _make_session(StubBrand())

        captured: dict = {}

        async def fake_create_invoice(**kwargs):
            captured.update(kwargs)
            return {
                "trx_id": "OY-TRX-1234",
                "checkout_url": "https://oy.example.id/pay/1234",
            }

        monkeypatch.setattr(oy_invoices.oy_client, "create_invoice", fake_create_invoice)

        response = await oy_invoices.create_invoice_for_cart(db, cart)

        assert response["trx_id"] == "OY-TRX-1234"
        assert cart.invoice_id == "OY-TRX-1234"
        assert cart.invoice_provider == "oy"
        assert cart.qr_image_url == "https://oy.example.id/pay/1234"
        # Per-Brand creds threaded through, master env fallback unused.
        assert captured["api_key"] == "brand-key"
        assert captured["username"] == "brand-user"
        assert captured["external_id"] == f"cart-{cart.id}"
        assert captured["amount_idr"] == 250_000
        assert captured["callback_url"].endswith("/webhooks/oy/invoice")

    @pytest.mark.asyncio
    async def test_mock_mode_when_brand_missing(self, monkeypatch):
        cart = _StubCart(total_idr=100_000)
        db = _make_session(brand=None)

        async def fake_create_invoice(**_kwargs):
            raise AssertionError("real OY must NOT be called in mock mode")

        monkeypatch.setattr(oy_invoices.oy_client, "create_invoice", fake_create_invoice)

        response = await oy_invoices.create_invoice_for_cart(db, cart)
        assert response["mock"] is True
        assert cart.invoice_id == f"oy-dev-{cart.id}"
        assert cart.invoice_provider == "oy"
        assert cart.qr_image_url.endswith(f"/api/mock-checkout/{cart.invoice_id}")

    @pytest.mark.asyncio
    async def test_mock_mode_when_brand_provider_not_oy(self, monkeypatch):
        """Brand.payment_provider=='xendit' → mock-mode (xendit_invoices
        would refuse but the dispatcher must not even reach this branch)."""
        cart = _StubCart(total_idr=50_000)
        db = _make_session(StubBrand(payment_provider="xendit"))

        async def fake_create_invoice(**_kwargs):
            raise AssertionError("real OY must NOT fire when provider=xendit")

        monkeypatch.setattr(oy_invoices.oy_client, "create_invoice", fake_create_invoice)

        response = await oy_invoices.create_invoice_for_cart(db, cart)
        assert response["mock"] is True
        assert cart.invoice_id.startswith("oy-dev-")

    @pytest.mark.asyncio
    async def test_real_path_raises_http_409_when_amount_zero(self, monkeypatch):
        """Real-path guard: a 0-total cart must not silently create an invoice."""
        cart = _StubCart(total_idr=0)
        db = _make_session(StubBrand())

        async def fake_create_invoice(**_kwargs):
            raise AssertionError("real OY must NOT fire when amount is 0")

        monkeypatch.setattr(oy_invoices.oy_client, "create_invoice", fake_create_invoice)

        with pytest.raises(HTTPException) as ei:
            await oy_invoices.create_invoice_for_cart(db, cart)
        assert ei.value.status_code == 409

    @pytest.mark.asyncio
    async def test_real_path_response_falls_back_to_id_field(self, monkeypatch):
        """OY sometimes returns only `id` in the response — must still set invoice_id."""
        cart = _StubCart(total_idr=100_000)
        db = _make_session(StubBrand())

        async def fake_create_invoice(**_kwargs):
            return {"id": "OY-ONLY-ID", "payment_url": "https://oy/x"}

        monkeypatch.setattr(oy_invoices.oy_client, "create_invoice", fake_create_invoice)

        await oy_invoices.create_invoice_for_cart(db, cart)
        assert cart.invoice_id == "OY-ONLY-ID"
        assert cart.qr_image_url == "https://oy/x"

    @pytest.mark.asyncio
    async def test_real_path_response_falls_back_to_invoice_url_key(self, monkeypatch):
        """If neither `checkout_url` nor `payment_url` is present, fall back to `invoice_url`."""
        cart = _StubCart(total_idr=100_000)
        db = _make_session(StubBrand())

        async def fake_create_invoice(**_kwargs):
            return {"trx_id": "OY-X", "invoice_url": "https://oy/invoice"}

        monkeypatch.setattr(oy_invoices.oy_client, "create_invoice", fake_create_invoice)

        await oy_invoices.create_invoice_for_cart(db, cart)
        assert cart.qr_image_url == "https://oy/invoice"

    @pytest.mark.asyncio
    async def test_mock_invoice_id_uses_order_id_when_set(self, monkeypatch):
        """Mock-mode invoice id prefers cart.order_id over cart.id."""
        cart = _StubCart(total_idr=10_000, order_id="ord-from-buyer")
        db = _make_session(brand=None)

        async def fake_create_invoice(**_kwargs):
            raise AssertionError("mock mode → no OY call")

        monkeypatch.setattr(oy_invoices.oy_client, "create_invoice", fake_create_invoice)

        response = await oy_invoices.create_invoice_for_cart(db, cart)
        assert response["mock"] is True
        assert cart.invoice_id == "oy-dev-ord-from-buyer"

    @pytest.mark.asyncio
    async def test_payer_fields_use_billing_fallbacks(self, monkeypatch):
        """Billing dict uses `contact_email` / `display_name` as fallbacks."""
        cart = _StubCart(
            total_idr=100_000,
            billing={"contact_email": "fb@example.com", "display_name": "Fallback"},
        )
        db = _make_session(StubBrand())

        captured: dict = {}

        async def fake_create_invoice(**kwargs):
            captured.update(kwargs)
            return {"trx_id": "T", "checkout_url": "u"}

        monkeypatch.setattr(oy_invoices.oy_client, "create_invoice", fake_create_invoice)
        await oy_invoices.create_invoice_for_cart(db, cart)
        assert captured["payer_email"] == "fb@example.com"
        assert captured["payer_name"] == "Fallback"


class TestCreateInvoiceForOrder:
    @pytest.mark.asyncio
    async def test_writes_generic_snapshot_keys(self, monkeypatch):
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder()

        async def fake_create_invoice(**_kwargs):
            return {
                "trx_id": "OY-TRX-9999",
                "checkout_url": "https://oy.example.id/pay/9999",
            }

        monkeypatch.setattr(oy_invoices.oy_client, "create_invoice", fake_create_invoice)

        response = await oy_invoices.create_invoice_for_order(db, order)

        assert response["trx_id"] == "OY-TRX-9999"
        snap = order.payment_method_snapshot
        assert snap["type"] == "oy_invoice"
        assert snap["payment_provider"] == "oy"
        assert snap["invoice_id"] == "OY-TRX-9999"
        assert snap["invoice_url"] == "https://oy.example.id/pay/9999"

    @pytest.mark.asyncio
    async def test_refuses_when_brand_provider_mismatch(self, monkeypatch):
        brand = StubBrand(payment_provider="xendit")
        db = _make_session(brand)
        order = StubOrder()

        async def fake_create_invoice(**_kwargs):
            raise AssertionError("must not call real OY")

        monkeypatch.setattr(oy_invoices.oy_client, "create_invoice", fake_create_invoice)

        with pytest.raises(HTTPException) as ei:
            await oy_invoices.create_invoice_for_order(db, order)
        assert ei.value.status_code == 500

    @pytest.mark.asyncio
    async def test_raises_http_500_when_brand_missing(self, monkeypatch):
        """Order's brand row must exist before we attempt to mint an invoice."""
        db = _make_session(brand=None)
        order = StubOrder()

        async def fake_create_invoice(**_kwargs):
            raise AssertionError("must not call real OY when brand is missing")

        monkeypatch.setattr(oy_invoices.oy_client, "create_invoice", fake_create_invoice)

        with pytest.raises(HTTPException) as ei:
            await oy_invoices.create_invoice_for_order(db, order)
        assert ei.value.status_code == 500

    @pytest.mark.asyncio
    async def test_real_path_collapses_items_into_description(self, monkeypatch):
        """OY doesn't accept an `items` array — items are folded into the description."""
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder(items=[
            {"name": "Kopi", "qty": 2, "unit_price_idr": 25_000},
            {"name": "Gula", "qty": 1, "unit_price_idr": 15_000},
        ])

        captured: dict = {}

        async def fake_create_invoice(**kwargs):
            captured.update(kwargs)
            return {"trx_id": "OY-T", "checkout_url": "u"}

        monkeypatch.setattr(oy_invoices.oy_client, "create_invoice", fake_create_invoice)
        await oy_invoices.create_invoice_for_order(db, order)
        assert "Kopi x2" in captured["description"]
        assert "Gula x1" in captured["description"]
        # `payment_methods` is not in kwargs — OY's contract uses defaults.
        assert "items" not in captured

    @pytest.mark.asyncio
    async def test_real_path_snapshot_falls_back_to_invoice_id_field(self, monkeypatch):
        """If OY returns `id` instead of `trx_id`, snapshot must still be populated."""
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder()

        async def fake_create_invoice(**_kwargs):
            return {"id": "OY-BY-ID", "payment_url": "https://oy/p"}

        monkeypatch.setattr(oy_invoices.oy_client, "create_invoice", fake_create_invoice)
        await oy_invoices.create_invoice_for_order(db, order)

        snap = order.payment_method_snapshot
        assert snap["invoice_id"] == "OY-BY-ID"
        assert snap["invoice_url"] == "https://oy/p"


class TestMockModeMatrix:
    """The `_mock_mode` truth table lives in services/oy_invoices.py:45-56.

    True when: brand is None, OR payment_provider != 'oy', OR no key
    (neither env nor per-Brand). Otherwise real-path.
    """

    @pytest.mark.asyncio
    async def test_mock_when_brand_is_none(self, monkeypatch):
        cart = _StubCart(total_idr=10_000)
        db = _make_session(brand=None)

        async def fake_create_invoice(**_kwargs):
            raise AssertionError("mock-mode: no OY call")

        monkeypatch.setattr(oy_invoices.oy_client, "create_invoice", fake_create_invoice)
        response = await oy_invoices.create_invoice_for_cart(db, cart)
        assert response["mock"] is True

    @pytest.mark.asyncio
    async def test_real_when_provider_oy_and_brand_key_only(self, monkeypatch):
        cart = _StubCart(total_idr=10_000)
        db = _make_session(StubBrand(oy_api_key="brand-only"))

        called = []

        async def fake_create_invoice(**kwargs):
            called.append(kwargs)
            return {"trx_id": "T", "checkout_url": "u"}

        monkeypatch.setattr(oy_invoices.oy_client, "create_invoice", fake_create_invoice)
        response = await oy_invoices.create_invoice_for_cart(db, cart)
        assert "mock" not in response or response.get("mock") is not True
        assert len(called) == 1
        assert called[0]["api_key"] == "brand-only"

    @pytest.mark.asyncio
    async def test_real_when_provider_oy_and_env_key_only(self, monkeypatch):
        cart = _StubCart(total_idr=10_000)
        db = _make_session(StubBrand(oy_api_key=""))

        called = []

        async def fake_create_invoice(**kwargs):
            called.append(kwargs)
            return {"trx_id": "T", "checkout_url": "u"}

        fake_settings = SimpleNamespace(
            oy_api_key="env-only",
            oy_default_username="env-user",
            oy_callback_base_url="http://x",
            mock_checkout_public_base="http://x",
        )
        monkeypatch.setattr(oy_invoices, "settings", fake_settings)
        monkeypatch.setattr(oy_invoices.oy_client, "create_invoice", fake_create_invoice)
        response = await oy_invoices.create_invoice_for_cart(db, cart)
        # The env-key fallback is consumed inside `_request` (oy_client),
        # not visible at the outer caller. Here we only assert the
        # dispatcher chose the real path (no `mock` flag).
        assert response.get("mock") is not True
        assert len(called) == 1

    @pytest.mark.asyncio
    async def test_mock_when_provider_oy_but_neither_key(self, monkeypatch):
        cart = _StubCart(total_idr=10_000)
        db = _make_session(StubBrand(oy_api_key=""))

        async def fake_create_invoice(**_kwargs):
            raise AssertionError("mock-mode: no OY call")

        fake_settings = SimpleNamespace(
            oy_api_key="",
            oy_default_username="",
            oy_callback_base_url="http://x",
            mock_checkout_public_base="http://x",
        )
        monkeypatch.setattr(oy_invoices, "settings", fake_settings)
        monkeypatch.setattr(oy_invoices.oy_client, "create_invoice", fake_create_invoice)
        response = await oy_invoices.create_invoice_for_cart(db, cart)
        assert response["mock"] is True

