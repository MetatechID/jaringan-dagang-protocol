"""Tests for the ``services.carriers.cancel`` seam.

Covers dispatch over Jubelio (Biteship support is skipped — Biteship has no
cancel implementation, and the seam lets it raise so PR2's scope stays
narrow). Contract v1.8 §3.2 specifies cancel-by-awb_code:

    POST /shipments/cancel
    {"cancel_reason": "...", "awb_code": "..."}

Two known rejection messages (per §3.2 500-Response-Attributes) are mapped
to distinguishable ``ShippingError`` subclasses so the router can return
caller-facing 409 codes.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

import pytest

from services import carriers  # noqa: E402
from services import jubelio as jubelio_service  # noqa: E402


def _stub_brand(*, jubelio_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id="brand-id",
        slug="safiya",
        jubelio_enabled=jubelio_enabled,
        jubelio_origin_address={"zipcode": "12940"},
        biteship_origin_address={"postal_code": "12940"},
    )


def _stub_order(
    *,
    carrier: str = "jubelio",
    awb: str = "AWB-1",
    shipment_id: str | None = "1281",
    state: str = "FULFILLING",
) -> SimpleNamespace:
    return SimpleNamespace(
        id="order-1",
        brand_id="brand-id",
        state=state,
        carrier=carrier,
        fulfillment_awb=awb,
        fulfillment_tracking_url="https://track",
        jubelio_shipment_id=shipment_id,
        biteship_order_id=None,
        shipped_at=SimpleNamespace(__class__=type("X", (), {})),
        items=[],
    )


class TestJubelioHappyPath:
    @pytest.mark.asyncio
    async def test_cancel_dispatches_to_jubelio_with_awb_code(self, monkeypatch):
        order = _stub_order()
        brand = _stub_brand(jubelio_enabled=True)
        captured: dict = {}

        async def _fake_cancel(*, awb_code, reason="Barang belum siap"):
            captured["awb_code"] = awb_code
            captured["reason"] = reason
            return {"status": "cancel successful", "awb_code": awb_code}

        monkeypatch.setattr(jubelio_service, "cancel_shipment", _fake_cancel)

        result = await carriers.cancel(brand=brand, order=order, reason="tester cancel")

        assert captured["awb_code"] == "AWB-1"
        assert captured["reason"] == "tester cancel"
        # Result envelope the router relies on.
        assert result["carrier"] == "jubelio"
        assert result["awb"] == "AWB-1"
        assert result["status"] == "cancel successful"


class TestJubelioRejections:
    @pytest.mark.asyncio
    async def test_courier_does_not_accept_cancel_raises_distinct_error(self, monkeypatch):
        order = _stub_order()
        brand = _stub_brand()

        async def _fake_cancel(*, awb_code, reason="x"):
            raise jubelio_service.ShippingError(
                "Jubelio cancel 500: "
                '{"message":"the courier do not accept cancellation"}'
            )

        monkeypatch.setattr(jubelio_service, "cancel_shipment", _fake_cancel)

        with pytest.raises(carriers.CourierRejection) as ei:
            await carriers.cancel(brand=brand, order=order, reason="x")
        assert "the courier do not accept cancellation" in str(ei.value)

    @pytest.mark.asyncio
    async def test_too_close_to_pickup_raises_distinct_error(self, monkeypatch):
        order = _stub_order()
        brand = _stub_brand()

        async def _fake_cancel(*, awb_code, reason="x"):
            raise jubelio_service.ShippingError(
                "Jubelio cancel 500: "
                '{"message":"too close with pickup schedule"}'
            )

        monkeypatch.setattr(jubelio_service, "cancel_shipment", _fake_cancel)

        with pytest.raises(carriers.PickupWindowClosed) as ei:
            await carriers.cancel(brand=brand, order=order, reason="x")
        assert "too close with pickup schedule" in str(ei.value)

    @pytest.mark.asyncio
    async def test_generic_shipping_error_propagates(self, monkeypatch):
        order = _stub_order()
        brand = _stub_brand()

        async def _fake_cancel(*, awb_code, reason="x"):
            raise jubelio_service.ShippingError("Jubelio cancel 401: invalid creds")

        monkeypatch.setattr(jubelio_service, "cancel_shipment", _fake_cancel)

        with pytest.raises(carriers.ShippingError) as ei:
            await carriers.cancel(brand=brand, order=order, reason="x")
        # Is NOT one of the classified rejections.
        assert not isinstance(ei.value, carriers.CourierRejection)
        assert not isinstance(ei.value, carriers.PickupWindowClosed)


class TestDispatchGuards:
    @pytest.mark.asyncio
    async def test_biteship_carrier_raises_not_supported(self, monkeypatch):
        """Biteship has no cancel; the seam raises a dedicated error so the
        router can return 501 / a clear message rather than silently hanging.

        In production ``active_carrier()`` returns 'biteship' whenever the
        brand has ``jubelio_enabled=False`` AND ``settings.default_carrier``
        is 'biteship'. We stub the dispatch to return 'biteship' directly,
        which is the only way to exercise this branch in isolation (without
        rewriting process-wide config).
        """
        order = _stub_order(carrier="biteship", awb="B-1", shipment_id=None)
        order.biteship_order_id = "bs-1"
        brand = _stub_brand(jubelio_enabled=False)
        monkeypatch.setattr(
            "services.carriers.active_carrier",
            lambda _brand: "biteship",
        )
        with pytest.raises(carriers.CancelNotSupported) as ei:
            await carriers.cancel(brand=brand, order=order, reason="x")
        assert "biteship" in str(ei.value).lower()

    @pytest.mark.asyncio
    async def test_missing_awb_raises_value_error(self, monkeypatch):
        order = _stub_order(awb=None)
        brand = _stub_brand()
        # Make sure dispatch lands on jubelio so it's the AWB check that fires,
        # not the Biteship guard.
        monkeypatch.setattr(
            "services.carriers.active_carrier",
            lambda _brand: "jubelio",
        )
        with pytest.raises(ValueError):
            await carriers.cancel(brand=brand, order=order, reason="x")

    @pytest.mark.asyncio
    async def test_no_brand_picks_jubelio_when_default_carrier(self, monkeypatch):
        """active_carrier picks based on settings.default_carrier when no
        brand is provided; with default_carrier='jubelio' and no brand we
        should still attempt the cancel (the upstream router handles
        order→brand resolution separately, but the seam should still
        cooperate when called without a brand)."""
        order = _stub_order()

        async def _fake_cancel(*, awb_code, reason="x"):
            return {"status": "cancel successful", "awb_code": awb_code}

        monkeypatch.setattr(jubelio_service, "cancel_shipment", _fake_cancel)
        monkeypatch.setattr(
            "services.carriers.active_carrier",
            lambda _brand: "jubelio",
        )
        result = await carriers.cancel(brand=None, order=order, reason="x")
        assert result["carrier"] == "jubelio"
