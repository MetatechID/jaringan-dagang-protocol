"""Dipay invoice module — pure unit tests.

Covers ``services/dipay_invoices.py`` (mock-mode + real-path dispatch,
vendor-neutral cart columns, snapshot persistence for orders).

DB and HTTP layer fakes are imported from ``tests/_dipay_fakes``. We
monkeypatch ``dipay_client.create_qris`` so no network is hit.

Mirrors ``tests/test_sento_invoices.py`` shape, with the Dipay QRIS MPM
API surface:
- ``create_qris`` returns ``{responseCode, responseMessage, qrContent, …}``
- ``partnerReferenceNo`` is the Dipay equivalent of Sento's ``partner_tx_id``
  (SNAP ref, ≤32 chars, ``q-`` prefix)
- Output of ``create_invoice_for_*`` is normalized to ``{id, invoice_url,
  qris_image_url, qris_content}`` — the PNG URL is OUR renderer
  (``{qr_public_base}/api/v1/qris/{ref}.png``).
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

from services import dipay_invoices  # noqa: E402
from tests._dipay_fakes import (  # noqa: E402
    FakeSession,
    StubBrand,
    StubOrder,
)


class _StubCart:
    """dipay_invoices-specific Cart shape."""

    def __init__(self, *, total_idr: int, order_id=None):
        self.id = "cart-test-id"
        self.bpp_id = "safiya.bpp.jaringan-dagang.id"
        self.order_id = order_id
        self.quote_json = {"total_idr": total_idr} if total_idr else {}
        self.items_json = [{"sku_id": "SKU-1", "qty": 2}]
        self.invoice_id = None
        self.invoice_provider = None
        self.qr_image_url = None
        self.qris_image_url = None
        self.qris_content = None
        self.expires_at = None


def _qris_response(qr_content="00020101021226…5303360…5802ID"):
    """Canonical QRIS MPM generate response shape."""
    return {
        "responseCode": "2004700",
        "responseMessage": "Success",
        "referenceNo": "REF-9999",
        "partnerReferenceNo": "q-carttestid",
        "qrContent": qr_content,
    }


def _make_session(brand: SimpleNamespace) -> FakeSession:
    return FakeSession([brand])


def _fake_settings(**overrides) -> SimpleNamespace:
    base = dict(
        environment="test",
        dipay_base_url="https://api.dipay.id",
        dipay_client_key="env-client-key",
        dipay_client_secret="env-client-secret",
        dipay_private_key_b64="test-private-key-b64",
        dipay_private_key_path="",
        dipay_merchant_id="M-ENV",
        qr_public_base="https://api.beli-aman.metatech.id",
        mock_checkout_public_base="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def _async_returning(value, **_kwargs):
    return value


class TestCreateInvoiceForCart:
    @pytest.fixture(autouse=True)
    def _deterministic_settings(self, monkeypatch):
        """Pin settings so ambient .env / env vars can't flip mock-mode."""
        s = _fake_settings()
        monkeypatch.setattr(dipay_invoices, "settings", s)
        monkeypatch.setattr(dipay_invoices.dipay_client, "settings", s)

    @pytest.mark.asyncio
    async def test_real_path_persists_ref_before_api_call(self, monkeypatch):
        """cart.invoice_id must be set BEFORE create_qris fires — a racing
        duplicate /confirm then reuses the same partnerReferenceNo."""
        cart = _StubCart(total_idr=250_000)
        db = _make_session(StubBrand())

        captured: dict = {}
        seen_invoice_id_at_call: list = []

        async def fake_create_qris(**kwargs):
            captured.update(kwargs)
            seen_invoice_id_at_call.append((cart.invoice_id, cart.invoice_provider))
            return _qris_response()

        monkeypatch.setattr(
            dipay_invoices.dipay_client, "create_qris", fake_create_qris,
        )

        response = await dipay_invoices.create_invoice_for_cart(db, cart)

        # Race guard: ref + provider were already persisted when the API fired.
        assert seen_invoice_id_at_call == [("q-carttestid", "dipay")]
        assert captured["partner_reference_no"] == "q-carttestid"
        assert captured["amount_idr"] == 250_000
        assert (captured.get("merchant_id") or captured["config"].merchant_id) == "M-123"
        assert captured["validity_period"]
        assert len(captured["validity_period"]) == 25
        # Normalized response + persisted columns.
        assert response["id"] == "q-carttestid"
        png = "https://api.beli-aman.metatech.id/api/v1/qris/q-carttestid.png"
        assert response["invoice_url"] == png
        assert response["expires_at"] == captured["validity_period"]
        assert cart.invoice_id == "q-carttestid"
        assert cart.invoice_provider == "dipay"
        assert cart.qr_image_url == png
        assert cart.qris_image_url == png
        assert cart.qris_content == _qris_response()["qrContent"]
        assert len(cart.invoice_id) <= 32

    @pytest.mark.asyncio
    async def test_real_path_qris_content_missing_is_rejected(self, monkeypatch):
        """A response without qrContent is malformed upstream and must be rejected."""
        cart = _StubCart(total_idr=100_000)
        db = _make_session(StubBrand())

        async def fake_create_qris(**_kwargs):
            return {"responseCode": "2004700", "responseMessage": "Success"}

        monkeypatch.setattr(
            dipay_invoices.dipay_client, "create_qris", fake_create_qris,
        )
        with pytest.raises(dipay_invoices.DipayError, match="missing qrContent"):
            await dipay_invoices.create_invoice_for_cart(db, cart)

    @pytest.mark.asyncio
    async def test_mock_mode_when_brand_missing(self, monkeypatch):
        cart = _StubCart(total_idr=100_000)
        db = _make_session(brand=None)

        async def fake_create_qris(**_kwargs):
            raise AssertionError("real Dipay must NOT be called in mock mode")

        monkeypatch.setattr(
            dipay_invoices.dipay_client, "create_qris", fake_create_qris,
        )

        response = await dipay_invoices.create_invoice_for_cart(db, cart)
        assert response["mock"] is True
        assert cart.invoice_id == f"dipay-dev-{cart.id}"
        assert cart.invoice_provider == "dipay"
        assert cart.qr_image_url.endswith(f"/api/mock-checkout/{cart.invoice_id}")

    @pytest.mark.asyncio
    async def test_mock_mode_when_brand_provider_not_dipay(self, monkeypatch):
        cart = _StubCart(total_idr=50_000)
        db = _make_session(StubBrand(payment_provider="xendit"))

        async def fake_create_qris(**_kwargs):
            raise AssertionError("real Dipay must NOT fire when provider=xendit")

        monkeypatch.setattr(
            dipay_invoices.dipay_client, "create_qris", fake_create_qris,
        )

        response = await dipay_invoices.create_invoice_for_cart(db, cart)
        assert response["mock"] is True
        assert cart.invoice_id.startswith("dipay-dev-")

    @pytest.mark.asyncio
    async def test_mock_mode_when_no_keys_anywhere(self, monkeypatch):
        cart = _StubCart(total_idr=10_000)
        db = _make_session(StubBrand(dipay_client_key=""))
        s = _fake_settings(dipay_client_key="")
        monkeypatch.setattr(dipay_invoices, "settings", s)
        monkeypatch.setattr(dipay_invoices.dipay_client, "settings", s)

        async def fake_create_qris(**_kwargs):
            raise AssertionError("mock-mode: no Dipay call")

        monkeypatch.setattr(
            dipay_invoices.dipay_client, "create_qris", fake_create_qris,
        )
        response = await dipay_invoices.create_invoice_for_cart(db, cart)
        assert response["mock"] is True
        assert cart.invoice_id.startswith("dipay-dev-")

    @pytest.mark.asyncio
    async def test_real_path_when_env_key_only(self, monkeypatch):
        cart = _StubCart(total_idr=10_000)
        db = _make_session(StubBrand(dipay_client_key=""))
        s = _fake_settings(dipay_client_key="env-only")
        monkeypatch.setattr(dipay_invoices, "settings", s)
        monkeypatch.setattr(dipay_invoices.dipay_client, "settings", s)

        called = []

        async def fake_create_qris(**kwargs):
            called.append(kwargs)
            return _qris_response()

        monkeypatch.setattr(
            dipay_invoices.dipay_client, "create_qris", fake_create_qris,
        )
        response = await dipay_invoices.create_invoice_for_cart(db, cart)
        assert response.get("mock") is not True
        assert len(called) == 1

    @pytest.mark.asyncio
    async def test_real_path_raises_http_409_when_amount_zero(self, monkeypatch):
        cart = _StubCart(total_idr=0)
        db = _make_session(StubBrand())

        async def fake_create_qris(**_kwargs):
            raise AssertionError("real Dipay must NOT fire when amount is 0")

        monkeypatch.setattr(
            dipay_invoices.dipay_client, "create_qris", fake_create_qris,
        )

        with pytest.raises(HTTPException) as ei:
            await dipay_invoices.create_invoice_for_cart(db, cart)
        assert ei.value.status_code == 409

    @pytest.mark.asyncio
    async def test_mock_invoice_id_uses_order_id_when_set(self, monkeypatch):
        cart = _StubCart(total_idr=10_000, order_id="ord-from-buyer")
        db = _make_session(brand=None)

        async def fake_create_qris(**_kwargs):
            raise AssertionError("mock mode → no Dipay call")

        monkeypatch.setattr(
            dipay_invoices.dipay_client, "create_qris", fake_create_qris,
        )

        response = await dipay_invoices.create_invoice_for_cart(db, cart)
        assert response["mock"] is True
        assert cart.invoice_id == "dipay-dev-ord-from-buyer"


class TestCreateInvoiceForOrder:
    @pytest.fixture(autouse=True)
    def _deterministic_settings(self, monkeypatch):
        s = _fake_settings()
        monkeypatch.setattr(dipay_invoices, "settings", s)
        monkeypatch.setattr(dipay_invoices.dipay_client, "settings", s)

    @pytest.mark.asyncio
    async def test_writes_snapshot_keys_incl_partner_ref(self, monkeypatch):
        """The snapshot must carry partner_ref (the callback lookup key),
        invoice_id, invoice_url AND qris_content after the response."""
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder()

        async def fake_create_qris(**_kwargs):
            return _qris_response(qr_content="EMVCO-PAYLOAD-1")

        monkeypatch.setattr(
            dipay_invoices.dipay_client, "create_qris", fake_create_qris,
        )

        response = await dipay_invoices.create_invoice_for_order(db, order)

        snap = order.payment_method_snapshot
        assert snap["type"] == "dipay_qris"
        assert snap["payment_provider"] == "dipay"
        assert snap["partner_ref"] == "q-ordertestid"
        assert snap["invoice_id"] == "q-ordertestid"
        png = "https://api.beli-aman.metatech.id/api/v1/qris/q-ordertestid.png"
        assert snap["invoice_url"] == png
        assert snap["qris_image_url"] == png
        assert snap["qris_content"] == "EMVCO-PAYLOAD-1"
        assert snap["expires_at"] is not None
        assert len(snap["expires_at"]) == 25
        assert response["id"] == "q-ordertestid"
        assert response["invoice_url"] == png
        assert response["expires_at"] == snap["expires_at"]

    @pytest.mark.asyncio
    async def test_real_path_persists_snapshot_before_api_call(self, monkeypatch):
        """partner_ref must be on the snapshot when create_qris fires — if
        we crash mid-create the webhook can still resolve the order."""
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder()

        seen: list = []

        async def fake_create_qris(**_kwargs):
            seen.append(dict(order.payment_method_snapshot or {}))
            return _qris_response()

        monkeypatch.setattr(
            dipay_invoices.dipay_client, "create_qris", fake_create_qris,
        )
        await dipay_invoices.create_invoice_for_order(db, order)
        assert seen[0]["partner_ref"] == "q-ordertestid"
        assert seen[0]["payment_provider"] == "dipay"

    @pytest.mark.asyncio
    async def test_real_path_folds_items_into_description_snapshot(self, monkeypatch):
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder(items=[
            {"name": "Kopi", "qty": 2, "unit_price_idr": 25_000},
            {"name": "Gula", "qty": 1, "unit_price_idr": 15_000},
        ])

        async def fake_create_qris(**_kwargs):
            return _qris_response()

        monkeypatch.setattr(
            dipay_invoices.dipay_client, "create_qris", fake_create_qris,
        )
        await dipay_invoices.create_invoice_for_order(db, order)
        desc = order.payment_method_snapshot["description"]
        assert "Kopi x2" in desc
        assert "Gula x1" in desc
        assert "order-test-id" in desc

    @pytest.mark.asyncio
    async def test_buyer_email_takes_precedence_over_shipping_address(self, monkeypatch):
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder()  # shipping_address carries "buyer@example.com"

        monkeypatch.setattr(
            dipay_invoices.dipay_client, "create_qris",
            lambda **_kwargs: _async_returning(_qris_response()),
        )
        await dipay_invoices.create_invoice_for_order(
            db, order, buyer_email="auth-user@example.com",
        )
        snap = order.payment_method_snapshot
        assert snap["email"] == "auth-user@example.com"
        assert snap["sender_name"] == "Buyer"

    @pytest.mark.asyncio
    async def test_falls_back_to_shipping_address_email_when_no_buyer_email(self, monkeypatch):
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder()

        monkeypatch.setattr(
            dipay_invoices.dipay_client, "create_qris",
            lambda **_kwargs: _async_returning(_qris_response()),
        )
        await dipay_invoices.create_invoice_for_order(db, order)
        snap = order.payment_method_snapshot
        assert snap["email"] == "buyer@example.com"

    @pytest.mark.asyncio
    async def test_mock_branch_writes_dipay_qris_snapshot(self, monkeypatch):
        brand = StubBrand(payment_provider="xendit")
        db = _make_session(brand)
        order = StubOrder()

        async def fake_create_qris(**_kwargs):
            raise AssertionError("mock-mode: no Dipay call")

        monkeypatch.setattr(
            dipay_invoices.dipay_client, "create_qris", fake_create_qris,
        )

        response = await dipay_invoices.create_invoice_for_order(db, order)
        snap = order.payment_method_snapshot
        assert snap["type"] == "dipay_qris"
        assert snap["payment_provider"] == "dipay"
        assert snap["invoice_id"] == f"dipay-dev-{order.id}"
        assert snap["invoice_url"].endswith(f"/api/mock-checkout/dipay-dev-{order.id}")
        assert response["mock"] is True

    @pytest.mark.asyncio
    async def test_partner_ref_is_snap_compliant(self, monkeypatch):
        """partnerReferenceNo ≤ 32 chars — long UUID order ids must fit."""
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder(id="0f0e8a2e-1c3b-4d5e-6f70-8a9b0c1d2e3f")

        captured: dict = {}

        async def fake_create_qris(**kwargs):
            captured.update(kwargs)
            return _qris_response()

        monkeypatch.setattr(
            dipay_invoices.dipay_client, "create_qris", fake_create_qris,
        )
        await dipay_invoices.create_invoice_for_order(db, order)
        ref = captured["partner_reference_no"]
        assert len(ref) <= 32
        assert ref == order.payment_method_snapshot["partner_ref"]

    @pytest.mark.asyncio
    async def test_preflight_reservation_has_creating_status_and_no_phantom_urls(self, monkeypatch):
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder()

        seen: list = []

        async def fake_create_qris(**_kwargs):
            seen.append(dict(order.payment_method_snapshot or {}))
            return _qris_response(qr_content="EMVCO-VALID-1")

        monkeypatch.setattr(
            dipay_invoices.dipay_client, "create_qris", fake_create_qris,
        )
        await dipay_invoices.create_invoice_for_order(db, order)
        assert seen[0]["invoice_status"] == "creating"
        assert seen[0]["partner_ref"] == "q-ordertestid"
        assert "invoice_url" not in seen[0]
        assert "qris_image_url" not in seen[0]
        assert "qris_content" not in seen[0]

    @pytest.mark.asyncio
    async def test_success_promotes_snapshot_to_ready_with_qris_content(self, monkeypatch):
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder()

        monkeypatch.setattr(
            dipay_invoices.dipay_client, "create_qris",
            lambda **_kwargs: _async_returning(_qris_response(qr_content="EMVCO-READY-1")),
        )
        res = await dipay_invoices.create_invoice_for_order(db, order)
        snap = order.payment_method_snapshot
        assert snap["invoice_status"] == "ready"
        assert snap["qris_content"] == "EMVCO-READY-1"
        assert snap["invoice_url"].endswith("/q-ordertestid.png")
        assert snap["qris_image_url"].endswith("/q-ordertestid.png")
        assert res["qris_content"] == "EMVCO-READY-1"
        assert res["invoice_url"] == snap["invoice_url"]

    @pytest.mark.asyncio
    async def test_upstream_failure_marks_snapshot_failed_without_phantom_urls(self, monkeypatch):
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder()

        async def fake_create_qris(**_kwargs):
            raise dipay_invoices.DipayError(
                401, {"responseCode": "4017300", "responseMessage": "Unauthorized. Unknown Client"},
            )

        monkeypatch.setattr(
            dipay_invoices.dipay_client, "create_qris", fake_create_qris,
        )
        with pytest.raises(dipay_invoices.DipayError):
            await dipay_invoices.create_invoice_for_order(db, order)

        snap = order.payment_method_snapshot
        assert snap["invoice_status"] == "failed"
        assert snap["failure_code"] == 401
        assert snap["partner_ref"] == "q-ordertestid"
        assert "invoice_url" not in snap
        assert "qris_image_url" not in snap
        assert "qris_content" not in snap

    @pytest.mark.asyncio
    async def test_retry_after_failure_reuses_deterministic_partner_ref(self, monkeypatch):
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder()

        # Step 1: failure
        async def fake_fail(**_kwargs):
            raise dipay_invoices.DipayError(401, {"responseCode": "4017300"})

        monkeypatch.setattr(dipay_invoices.dipay_client, "create_qris", fake_fail)
        with pytest.raises(dipay_invoices.DipayError):
            await dipay_invoices.create_invoice_for_order(db, order)
        assert order.payment_method_snapshot["invoice_status"] == "failed"

        # Step 2: retry on same order succeeds
        db2 = _make_session(brand)
        monkeypatch.setattr(
            dipay_invoices.dipay_client, "create_qris",
            lambda **_kwargs: _async_returning(_qris_response(qr_content="EMVCO-RETRY-OK")),
        )
        res = await dipay_invoices.create_invoice_for_order(db2, order)
        snap = order.payment_method_snapshot
        assert snap["invoice_status"] == "ready"
        assert snap["partner_ref"] == "q-ordertestid"
        assert snap["qris_content"] == "EMVCO-RETRY-OK"
        assert res["id"] == "q-ordertestid"

    @pytest.mark.asyncio
    async def test_ready_snapshot_returns_idempotently_without_calling_create_qris(self, monkeypatch):
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder()
        order.payment_method_snapshot = {
            "type": "dipay_qris",
            "payment_provider": "dipay",
            "invoice_status": "ready",
            "invoice_id": "q-ordertestid",
            "partner_ref": "q-ordertestid",
            "invoice_url": "https://api.beli-aman.metatech.id/api/v1/qris/q-ordertestid.png",
            "qris_image_url": "https://api.beli-aman.metatech.id/api/v1/qris/q-ordertestid.png",
            "qris_content": "00020101021226...READY",
            "expires_at": "2026-09-24T12:00:00+07:00",
        }

        async def explode(**_kwargs):
            raise AssertionError("create_qris should not be called when snapshot is already ready")

        monkeypatch.setattr(dipay_invoices.dipay_client, "create_qris", explode)
        res = await dipay_invoices.create_invoice_for_order(db, order)
        assert res["id"] == "q-ordertestid"
        assert res["qris_content"] == "00020101021226...READY"
        assert res["invoice_url"] == "https://api.beli-aman.metatech.id/api/v1/qris/q-ordertestid.png"

    @pytest.mark.asyncio
    async def test_reservation_db_returns_ready_if_minted_concurrently(self, monkeypatch):
        brand = StubBrand()
        db = _make_session(brand)
        order = StubOrder()

        reserved_order = StubOrder()
        reserved_order.payment_method_snapshot = {
            "type": "dipay_qris",
            "payment_provider": "dipay",
            "invoice_status": "ready",
            "invoice_id": "q-ordertestid",
            "partner_ref": "q-ordertestid",
            "invoice_url": "https://api.beli-aman.metatech.id/api/v1/qris/q-ordertestid.png",
            "qris_image_url": "https://api.beli-aman.metatech.id/api/v1/qris/q-ordertestid.png",
            "qris_content": "CONCURRENT-READY",
            "expires_at": "2026-09-24T12:00:00+07:00",
        }
        reservation_db = FakeSession([reserved_order])

        async def explode(**_kwargs):
            raise AssertionError("create_qris must not be called when reservation_db finds ready invoice")

        monkeypatch.setattr(dipay_invoices.dipay_client, "create_qris", explode)
        res = await dipay_invoices.create_invoice_for_order(
            db, order, reservation_db=reservation_db,
        )
        assert res["qris_content"] == "CONCURRENT-READY"
        assert order.payment_method_snapshot["qris_content"] == "CONCURRENT-READY"


class TestMockModeMatrix:
    """``_mock_mode`` truth table: True when brand is None, OR provider !=
    'dipay', OR no client key (env or per-Brand). Otherwise real-path."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "brand_kwargs,env_key,expected_mock",
        [
            (None, "", True),                                # no brand row
            ({"payment_provider": "sento"}, "env", True),    # wrong provider
            ({"dipay_client_key": ""}, "", True),            # no keys at all
            ({"dipay_client_key": ""}, "env-key", False),    # env key only
            ({"dipay_client_key": "brand"}, "", False),      # brand key only
            ({}, "env-key", False),                          # both
        ],
    )
    async def test_matrix(self, monkeypatch, brand_kwargs, env_key, expected_mock):
        brand = StubBrand(**brand_kwargs) if brand_kwargs is not None else None
        s = _fake_settings(dipay_client_key=env_key)
        monkeypatch.setattr(dipay_invoices, "settings", s)
        monkeypatch.setattr(dipay_invoices.dipay_client, "settings", s)
        assert dipay_invoices._mock_mode(brand) is expected_mock
