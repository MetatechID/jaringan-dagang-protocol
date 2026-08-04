"""Tests for ``GET /api/v1/orders/{order_id}/label-data``.

Returns the printer-friendly envelope the seller-dashboard needs for
Jubelio-booked orders only: combined AWB / courier / sender / recipient /
items / totals. Auth follows the existing admin-token pattern. We do
NOT call any carrier APIs here — the envelope is built straight off the
order row and the brand's jubelio_origin_address.
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from routers.orders import router as orders_router


def _override_admin_token(app, *, allow: bool = True):
    from deps import require_admin_token as real

    async def _stubbed():
        if allow:
            return "admin"
        raise HTTPException(401, "admin token required")

    app.dependency_overrides[real] = _stubbed


def _install_db(app, brand):
    """Stub ``db.execute`` to return ``brand`` for the one Brand lookup the
    label endpoint makes. Order lookup uses ``lock_order_for_update`` so
    that path is overridden per-test."""
    from database import get_db as real_get_db

    class _FakeSession:
        def __init__(self, brand):
            self._brand = brand
            self.commits = 0
            self.commited = False

        async def execute(self, *_args, **_kwargs):
            class _Exec:
                def scalar_one_or_none(self_inner):
                    return self._brand

            return _Exec()

        async def flush(self_inner):
            return None

        async def commit(self_inner):
            self_inner.commited = True
            return None

    session = _FakeSession(brand)

    async def _stub_get_db():
        yield session

    app.dependency_overrides[real_get_db] = _stub_get_db
    return session


@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(orders_router)
    _override_admin_token(a, allow=True)
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


def _stub_lock(monkeypatch, order):
    async def _fake_lock(_db, _order_id):
        return order

    monkeypatch.setattr("routers.orders.lock_order_for_update", _fake_lock)


def _brand(*, jubelio_origin=None, slug="safiyafood"):
    return SimpleNamespace(
        slug=slug,
        name="Safiya Food",
        jubelio_enabled=True,
        jubelio_origin_address=jubelio_origin,
    )


# ---- Auth ------------------------------------------------------------------


class TestAuth:
    def test_requires_admin_token(self, app, client):
        from deps import require_admin_token as real

        async def _deny():
            raise HTTPException(403, "Invalid or missing X-Admin-Token header")

        app.dependency_overrides[real] = _deny
        resp = client.get("/api/v1/orders/order-1/label-data")
        assert resp.status_code == 403


# ---- 4xx error paths -------------------------------------------------------


class TestErrorPaths:
    def test_404_when_order_missing(self, monkeypatch, app, client):
        _brand_with_origin = _brand()
        _install_db(app, _brand_with_origin)
        _stub_lock(monkeypatch, None)

        resp = client.get("/api/v1/orders/missing/label-data")
        assert resp.status_code == 404

    def test_409_when_no_awb(self, monkeypatch, app, client):
        _install_db(app, _brand(jubelio_origin={"zipcode": "40115"}))
        order = SimpleNamespace(
            id="order-1",
            brand_id="brand-id",
            carrier="jubelio",
            fulfillment_awb=None,
            fulfillment_tracking_url="https://track",
            jubelio_shipment_id="1281",
            shipped_at=None,
            items=[],
            shipping_address=None,
            seller_order_ref=None,
            subtotal_idr=0,
            shipping_idr=0,
            fee_idr=0,
            total_idr=0,
            created_at=None,
        )
        _stub_lock(monkeypatch, order)

        resp = client.get("/api/v1/orders/order-1/label-data")
        assert resp.status_code == 409
        assert "AWB" in resp.json()["detail"]

    def test_409_when_carrier_not_jubelio(self, monkeypatch, app, client):
        _install_db(app, _brand(jubelio_origin={"zipcode": "40115"}))
        order = SimpleNamespace(
            id="order-1",
            brand_id="brand-id",
            carrier="biteship",
            fulfillment_awb="BITE123",
            fulfillment_tracking_url="https://track",
            jubelio_shipment_id=None,
            shipped_at=None,
            items=[],
            shipping_address=None,
            seller_order_ref=None,
            subtotal_idr=0,
            shipping_idr=0,
            fee_idr=0,
            total_idr=0,
            created_at=None,
        )
        _stub_lock(monkeypatch, order)

        resp = client.get("/api/v1/orders/order-1/label-data")
        assert resp.status_code == 409
        assert "jubelio" in resp.json()["detail"].lower()

    def test_409_when_origin_missing(self, monkeypatch, app, client):
        _install_db(app, _brand(jubelio_origin=None))
        order = SimpleNamespace(
            id="order-1",
            brand_id="brand-id",
            carrier="jubelio",
            fulfillment_awb="JP123456789",
            fulfillment_tracking_url="https://track",
            jubelio_shipment_id="1281",
            shipped_at=None,
            items=[],
            shipping_address=None,
            seller_order_ref=None,
            subtotal_idr=0,
            shipping_idr=0,
            fee_idr=0,
            total_idr=0,
            created_at=None,
        )
        _stub_lock(monkeypatch, order)

        resp = client.get("/api/v1/orders/order-1/label-data")
        assert resp.status_code == 409
        assert "origin" in resp.json()["detail"].lower()


# ---- Happy path ------------------------------------------------------------


class TestHappyPath:
    def test_returns_full_envelope(self, monkeypatch, app, client):
        origin = {
            "name": "Safiya Warehouse",
            "phone": "+628123456789",
            "email": "[email protected]",
            "address": "Jl. Setiabudi No. 1",
            "area_id": 10293,
            "zipcode": "40115",
        }
        _install_db(app, _brand(jubelio_origin=origin))
        order = SimpleNamespace(
            id="order-1",
            brand_id="brand-id",
            carrier="jubelio",
            fulfillment_awb="JP1234567890",
            fulfillment_tracking_url="https://track.example/awb/JP1234567890",
            jubelio_shipment_id="1281",
            shipped_at=None,
            items=[
                {
                    "sku": "SKU-1",
                    "name": "Sambal Matah 100g",
                    "qty": 2,
                    "unit_price_idr": 25000,
                },
                {
                    "sku": "SKU-2",
                    "name": "Bumbu Bali 200g",
                    "qty": 1,
                    "unit_price_idr": 45000,
                },
            ],
            shipping_address={
                "recipient_name": "Budi",
                "phone_e164": "+628987654321",
                "line1": "Jl. Asia Afrika No. 8",
                "line2": "Lantai 3",
                "kelurahan": "Cikawao",
                "kecamatan": "Lengkong",
                "kota": "Bandung",
                "provinsi": "Jawa Barat",
                "postal_code": "40261",
                "courier": {
                    "courier_code": "21",
                    "courier_service_code": "22",
                    "courier_service_name": "JNE REG",
                    "price_idr": 18000,
                    "duration": "2-3 day",
                },
            },
            seller_order_ref="SO-42",
            subtotal_idr=95000,
            shipping_idr=18000,
            fee_idr=0,
            total_idr=113000,
            created_at="2026-05-01T10:00:00+00:00",
        )
        _stub_lock(monkeypatch, order)

        resp = client.get("/api/v1/orders/order-1/label-data")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # Carrier + AWB metadata
        assert body["carrier"] == "jubelio"
        assert body["awb"] == "JP1234567890"
        assert body["shipment_id"] == "1281"
        assert body["order_id"] == "order-1"
        assert body["seller_order_ref"] == "SO-42"

        # Sender
        assert body["sender"]["store_name"] == "Safiya Food"
        assert body["sender"]["name"] == "Safiya Warehouse"
        assert body["sender"]["phone"] == "+628123456789"
        assert body["sender"]["email"] == "[email protected]"
        assert body["sender"]["address"] == "Jl. Setiabudi No. 1"
        assert body["sender"]["zipcode"] == "40115"

        # Recipient
        assert body["recipient"]["name"] == "Budi"
        assert body["recipient"]["phone"] == "+628987654321"
        assert body["recipient"]["postal_code"] == "40261"
        assert body["recipient"]["address"] == "Jl. Asia Afrika No. 8, Lantai 3"
        assert body["recipient"]["kelurahan"] == "Cikawao"
        assert body["recipient"]["kecamatan"] == "Lengkong"
        assert body["recipient"]["kota"] == "Bandung"
        assert body["recipient"]["provinsi"] == "Jawa Barat"

        # Courier/service snapshot
        assert body["courier"]["courier_code"] == "21"
        assert body["courier"]["courier_service_code"] == "22"
        assert body["courier"]["courier_service_name"] == "JNE REG"

        # Items reduced to {sku, name, qty}
        assert body["items"] == [
            {"sku": "SKU-1", "name": "Sambal Matah 100g", "qty": 2},
            {"sku": "SKU-2", "name": "Bumbu Bali 200g", "qty": 1},
        ]

        # COD defaults
        assert body["is_cod"] is False
        assert body["cod_amount_idr"] == 0

        # Totals from BAP
        assert body["subtotal_idr"] == 95000
        assert body["shipping_idr"] == 18000
        assert body["total_idr"] == 113000

    def test_handles_missing_courier_service_name(self, monkeypatch, app, client):
        origin = {"name": "S", "phone": "1", "address": "x", "zipcode": "40115"}
        _install_db(app, _brand(jubelio_origin=origin))
        order = SimpleNamespace(
            id="order-1",
            brand_id="brand-id",
            carrier="jubelio",
            fulfillment_awb="JP1",
            fulfillment_tracking_url=None,
            jubelio_shipment_id=None,
            shipped_at=None,
            items=[],
            shipping_address={
                "recipient_name": "X",
                "phone_e164": "1",
                "postal_code": "1",
                "courier": {"courier_code": "21"},
            },
            seller_order_ref=None,
            subtotal_idr=0,
            shipping_idr=0,
            fee_idr=0,
            total_idr=0,
            created_at=None,
        )
        _stub_lock(monkeypatch, order)

        resp = client.get("/api/v1/orders/order-1/label-data")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["courier"]["courier_code"] == "21"
        assert body["courier"]["courier_service_code"] is None
        assert body["courier"]["courier_service_name"] is None
        assert body["shipment_id"] is None
        assert body["tracking_url"] is None

    def test_address_falls_back_to_phone(self, monkeypatch, app, client):
        origin = {"name": "S", "phone": "1", "address": "x", "zipcode": "40115"}
        _install_db(app, _brand(jubelio_origin=origin))
        order = SimpleNamespace(
            id="order-1",
            brand_id="brand-id",
            carrier="jubelio",
            fulfillment_awb="JP1",
            fulfillment_tracking_url="t",
            jubelio_shipment_id=None,
            shipped_at=None,
            items=[],
            shipping_address={
                "recipient_name": "X",
                "phone": "+62812",
                "postal_code": "40261",
            },
            seller_order_ref=None,
            subtotal_idr=0,
            shipping_idr=0,
            fee_idr=0,
            total_idr=0,
            created_at=None,
        )
        _stub_lock(monkeypatch, order)

        resp = client.get("/api/v1/orders/order-1/label-data")
        assert resp.status_code == 200, resp.text
        assert resp.json()["recipient"]["phone"] == "+62812"
