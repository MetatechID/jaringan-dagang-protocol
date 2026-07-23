"""Sento invoice module — pure unit tests.

Covers ``services/sento_invoices.py`` (mock-mode + real-path dispatch,
vendor-neutral cart columns, snapshot persistence for orders).

DB and HTTP layer fakes are imported from ``tests/_sento_fakes``. We
monkeypatch ``sento_client.create_invoice`` so no network is hit.

Mirrors ``tests/test_oy_invoices.py`` shape, with the Sento Payment
Link API surface:
- ``create_invoice`` returns ``{status, url, payment_link_id, tx_ref_number}``
- ``partner_tx_id`` is the Sento equivalent of OY's ``external_id``
- Output of ``create_invoice_for_*`` is normalized to ``{id, invoice_url}``
  plus optional ``qris_image_url`` + ``expires_at``.
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
        self.qris_image_url = None


def _link_response(
    payment_link_id="PL-1234",
    url="https://pay.sento.id/x",
    expiration="2026-07-14 12:00:00",
):
    """Canonical Payment Link create response shape."""
    return {
        "status": True,
        "url": url,
        "payment_link_id": payment_link_id,
        "tx_ref_number": f"tx-{payment_link_id}",
    }


def _make_session(brand: SimpleNamespace) -> FakeSession:
    return FakeSession([brand])


class TestCreateInvoiceForCart:
    @pytest.mark.asyncio
    async def test_real_path_writes_renamed_columns(self, monkeypatch):
        """Happy path: sento_client.create_invoice returns a body, we
        persist vendor-neutral keys + the qris image url."""
        cart = _StubCart(total_idr=250_000)
        db = _make_session(StubBrand())

        captured: dict = {}

        async def fake_create_invoice(**kwargs):
            captured.update(kwargs)
            return _link_response(
                payment_link_id="PL-1234",
                url="https://pay.sento.id/img",
            )

        monkeypatch.setattr(
            sento_invoices.sento_client, "create_invoice", fake_create_invoice,
        )

        response = await sento_invoices.create_invoice_for_cart(db, cart)

        # Normalized response shape mirrors oy_invoices / xendit_invoices.
        assert response["id"] == "PL-1234"
        assert response["invoice_url"] == "https://pay.sento.id/img"
        # cart.invoice_id stores partner_tx_id (which Sento echoes back in
        # the callback body). The Sento payment_link_id (PL-1234) is
        # normalized into response["id"] above.
        assert cart.invoice_id == "cart-cart-test-id"
        assert cart.invoice_provider == "sento"
        # cart.qr_image_url is the buyer-facing payment URL — Payment
        # Link's hosted checkout page (the QR renders inside it).
        assert cart.qr_image_url == "https://pay.sento.id/img"
        assert cart.qris_image_url is None
        # Per-Brand creds threaded through, master env fallback unused.
        assert captured["api_key"] == "brand-key"
        assert captured["username"] == "brand-user"
        assert captured["partner_tx_id"] == f"cart-{cart.id}"
        assert captured["amount_idr"] == 250_000

    @pytest.mark.asyncio
    async def test_real_path_falls_back_to_partner_tx_id_when_no_link_id(
        self, monkeypatch
    ):
        """If Sento returns no ``payment_link_id`` (defensive), use partner_tx_id."""
        cart = _StubCart(total_idr=100_000)
        db = _make_session(StubBrand())

        async def fake_create_invoice(**_kwargs):
            return {"status": True, "url": "u"}

        monkeypatch.setattr(
            sento_invoices.sento_client, "create_invoice", fake_create_invoice,
        )
        await sento_invoices.create_invoice_for_cart(db, cart)
        assert cart.invoice_id == f"cart-{cart.id}"

    @pytest.mark.asyncio
    async def test_mock_mode_when_brand_missing(self, monkeypatch):
        cart = _StubCart(total_idr=100_000)
        db = _make_session(brand=None)

        async def fake_create_invoice(**_kwargs):
            raise AssertionError("real Sento must NOT be called in mock mode")

        monkeypatch.setattr(
            sento_invoices.sento_client, "create_invoice", fake_create_invoice,
        )

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

        monkeypatch.setattr(
            sento_invoices.sento_client, "create_invoice", fake_create_invoice,
        )

        response = await sento_invoices.create_invoice_for_cart(db, cart)
        assert response["mock"] is True
        assert cart.invoice_id.startswith("sento-dev-")

    @pytest.mark.asyncio
    async def test_real_path_raises_http_409_when_amount_zero(self, monkeypatch):
        cart = _StubCart(total_idr=0)
        db = _make_session(StubBrand())

        async def fake_create_invoice(**_kwargs):
            raise AssertionError("real Sento must NOT fire when amount is 0")

        monkeypatch.setattr(
            sento_invoices.sento_client, "create_invoice", fake_create_invoice,
        )

        with pytest.raises(HTTPException) as ei:
            await sento_invoices.create_invoice_for_cart(db, cart)
        assert ei.value.status_code == 409

    @pytest.mark.asyncio
    async def test_mock_invoice_id_uses_order_id_when_set(self, monkeypatch):
        cart = _StubCart(total_idr=10_000, order_id="ord-from-buyer")
        db = _make_session(brand=None)

        async def fake_create_invoice(**_kwargs):
            raise AssertionError("mock mode → no Sento call")

        monkeypatch.setattr(
            sento_invoices.sento_client, "create_invoice", fake_create_invoice,
        )

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
            return _link_response()

        monkeypatch.setattr(
            sento_invoices.sento_client, "create_invoice", fake_create_invoice,
        )
        await sento_invoices.create_invoice_for_cart(db, cart)
        # Payment Link uses sender_name (not sender_email like Payment Routing).
        assert captured["sender_name"] == "Fallback"


class TestCreateInvoiceForOrder:
    @pytest.mark.asyncio
    async def test_writes_generic_snapshot_keys(self, monkeypatch):
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder()

        async def fake_create_invoice(**_kwargs):
            return _link_response(
                payment_link_id="PL-9999",
                url="https://pay.sento.id/ord",
            )

        monkeypatch.setattr(
            sento_invoices.sento_client, "create_invoice", fake_create_invoice,
        )

        response = await sento_invoices.create_invoice_for_order(db, order)

        assert response["id"] == "PL-9999"
        assert response["invoice_url"] == "https://pay.sento.id/ord"
        snap = order.payment_method_snapshot
        # snapshot type is sento_invoice for Payment Link.
        assert snap["type"] == "sento_invoice"
        assert snap["payment_provider"] == "sento"
        assert snap["invoice_id"] == "PL-9999"
        assert snap["invoice_url"] == "https://pay.sento.id/ord"

    @pytest.mark.asyncio
    async def test_real_path_passes_description_to_create_invoice(self, monkeypatch):
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder(items=[
            {"name": "Kopi", "qty": 2, "unit_price_idr": 25_000},
            {"name": "Gula", "qty": 1, "unit_price_idr": 15_000},
        ])

        captured: dict = {}

        async def fake_create_invoice(**kwargs):
            captured.update(kwargs)
            return _link_response()

        monkeypatch.setattr(
            sento_invoices.sento_client, "create_invoice", fake_create_invoice,
        )
        await sento_invoices.create_invoice_for_order(db, order)
        # Items collapsed into the description field of create_invoice.
        desc = captured["description"]
        assert "Kopi x2" in desc
        assert "Gula x1" in desc

    @pytest.mark.asyncio
    async def test_real_path_partner_tx_id_starts_with_order(self, monkeypatch):
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder(id="order-abc")

        captured: dict = {}

        async def fake_create_invoice(**kwargs):
            captured.update(kwargs)
            return _link_response()

        monkeypatch.setattr(
            sento_invoices.sento_client, "create_invoice", fake_create_invoice,
        )
        await sento_invoices.create_invoice_for_order(db, order)
        assert captured["partner_tx_id"] == "order-order-abc"

    @pytest.mark.asyncio
    async def test_buyer_email_takes_precedence_over_shipping_address(self, monkeypatch):
        """The router passes profile.email as buyer_email; it must win over
        any email stashed on order.shipping_address (which advance_to_authed
        doesn't populate anyway, but the precedence protects callers that do)."""
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder()  # shipping_address carries "buyer@example.com"

        captured: dict = {}

        async def fake_create_invoice(**kwargs):
            captured.update(kwargs)
            return _link_response()

        monkeypatch.setattr(
            sento_invoices.sento_client, "create_invoice", fake_create_invoice,
        )
        await sento_invoices.create_invoice_for_order(
            db, order, buyer_email="auth-user@example.com",
        )
        assert captured["email"] == "auth-user@example.com"

    @pytest.mark.asyncio
    async def test_falls_back_to_shipping_address_email_when_no_buyer_email(self, monkeypatch):
        """When buyer_email is None (phone-only profile, or old callers), fall
        back to order.shipping_address["email"] as before — non-breaking."""
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder()  # shipping_address carries "buyer@example.com"

        captured: dict = {}

        async def fake_create_invoice(**kwargs):
            captured.update(kwargs)
            return _link_response()

        monkeypatch.setattr(
            sento_invoices.sento_client, "create_invoice", fake_create_invoice,
        )
        await sento_invoices.create_invoice_for_order(db, order)
        assert captured["email"] == "buyer@example.com"


class TestMockModeMatrix:
    """`_mock_mode` truth table: True when brand is None, OR provider != 'sento',
    OR no key (env or per-Brand). Otherwise real-path."""

    @pytest.mark.asyncio
    async def test_mock_when_brand_is_none(self, monkeypatch):
        cart = _StubCart(total_idr=10_000)
        db = _make_session(brand=None)

        async def fake_create_invoice(**_kwargs):
            raise AssertionError("mock-mode: no Sento call")

        monkeypatch.setattr(
            sento_invoices.sento_client, "create_invoice", fake_create_invoice,
        )
        response = await sento_invoices.create_invoice_for_cart(db, cart)
        assert response["mock"] is True

    @pytest.mark.asyncio
    async def test_real_when_provider_sento_and_brand_key_only(self, monkeypatch):
        cart = _StubCart(total_idr=10_000)
        db = _make_session(StubBrand(sento_api_key="brand-only"))

        called = []

        async def fake_create_invoice(**kwargs):
            called.append(kwargs)
            return _link_response()

        monkeypatch.setattr(
            sento_invoices.sento_client, "create_invoice", fake_create_invoice,
        )
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
            return _link_response()

        fake_settings = SimpleNamespace(
            sento_api_key="env-only",
            sento_default_username="env-user",
            sento_callback_base_url="http://x",
            mock_checkout_public_base="http://x",
            sento_invoice_duration_seconds=86400,
        )
        monkeypatch.setattr(sento_invoices, "settings", fake_settings)
        monkeypatch.setattr(
            sento_invoices.sento_client, "create_invoice", fake_create_invoice,
        )
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
            sento_invoice_duration_seconds=86400,
        )
        monkeypatch.setattr(sento_invoices, "settings", fake_settings)
        monkeypatch.setattr(
            sento_invoices.sento_client, "create_invoice", fake_create_invoice,
        )
        response = await sento_invoices.create_invoice_for_cart(db, cart)
        assert response["mock"] is True
