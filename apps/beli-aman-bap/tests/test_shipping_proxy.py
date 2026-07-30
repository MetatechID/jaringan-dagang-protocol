"""Tests for the region + service-category proxy endpoints in
``routers/shipping.py``.

These endpoints are thin pass-throughs that surface
``services.jubelio`` reference data to the SDK without exposing the
carrier auth directly. Tests assert:

- 200 + JSON body on happy path (each endpoint returns the service
  payload unchanged).
- 502 when the service raises ``ShippingError`` (carrier-side trouble).
- The existing ``POST /rates`` accepts the new optional
  ``service_category_id`` and threads it through to ``carriers.get_rates``.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.shipping import router as shipping_router  # noqa: E402
from services import carriers as carriers_module  # noqa: E402
from services import jubelio as jubelio_service  # noqa: E402


# ---- Catalog fixtures (mirroring tests/test_brands_payouts.py) -------------


def _fake_catalog():
    """Return a minimal catalog used by POST /rates for SKU→weight/value."""
    return [
        {"sku": "X1", "name": "Test", "price_idr": 10_000, "weight_grams": 200},
    ]


@pytest.fixture(autouse=True)
def _seed_catalog(monkeypatch):
    """Stub catalog_service.list_products so POST /rates can resolve SKUs."""

    async def _fake_list(slug):
        return _fake_catalog()

    from services import catalog as catalog_service
    monkeypatch.setattr(catalog_service, "list_products", _fake_list)


@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(shipping_router)
    # Stub get_db with a session that returns None for the brand SELECT
    # (POST /rates only needs the row to forward ``jubelio_enabled`` and
    # the origin zip; both routes fall back to Biteship defaults on None).
    from database import get_db as real_get_db
    from tests._jubelio_fakes import FakeSession

    async def _stub_db():
        yield FakeSession([None])  # brand SELECT → None

    a.dependency_overrides[real_get_db] = _stub_db
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


def _stub_service(monkeypatch, *, name: str, body):
    """Stub ``jubelio_service.<name>`` to return ``body`` and capture calls."""
    captured: dict = {}

    async def _fake(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return body

    monkeypatch.setattr(jubelio_service, name, _fake)
    return captured


def _stub_service_raises(monkeypatch, *, name: str):
    """Stub ``jubelio_service.<name>`` to raise ShippingError. The error
    message echoes the operation name so callers can identify which proxy
    endpoint logged a 502 — same shape the real service would produce."""

    async def _fake(*args, **kwargs):
        raise jubelio_service.ShippingError(f"Jubelio {name} 500: boom")

    monkeypatch.setattr(jubelio_service, name, _fake)


# ---- Happy paths ----------------------------------------------------------


class TestProxyHappyPaths:
    def test_courier_categories(self, monkeypatch, client):
        sample = [
            {"service_category_id": 1, "name": "REGULER"},
            {"service_category_id": 4, "name": "INSTANT"},
        ]
        _stub_service(monkeypatch, name="get_service_categories", body=sample)
        resp = client.get("/api/v1/shipping/courier-categories")
        assert resp.status_code == 200, resp.text
        assert resp.json() == sample

    def test_regions_no_query(self, monkeypatch, client):
        sample = [{"name": "Tebet Timur, Jakarta", "area_id": "3174011001"}]
        _stub_service(monkeypatch, name="get_regions", body=sample)
        resp = client.get("/api/v1/shipping/regions")
        assert resp.status_code == 200
        assert resp.json() == sample

    def test_regions_with_name_query(self, monkeypatch, client):
        captured = _stub_service(
            monkeypatch, name="get_regions",
            body=[{"name": "Menteng, Jakarta"}],
        )
        resp = client.get("/api/v1/shipping/regions?name=jakarta")
        assert resp.status_code == 200
        assert captured["kwargs"] == {"name": "jakarta"}

    def test_provinces(self, monkeypatch, client):
        sample = [{"province_id": "11", "name": "ACEH"}]
        _stub_service(monkeypatch, name="get_provinces", body=sample)
        resp = client.get("/api/v1/shipping/regions/provinces")
        assert resp.status_code == 200
        assert resp.json() == sample

    def test_cities(self, monkeypatch, client):
        captured = _stub_service(
            monkeypatch, name="get_cities",
            body=[{"city_id": "1105", "name": "KAB. ACEH BARAT"}],
        )
        resp = client.get("/api/v1/shipping/regions/cities/11")
        assert resp.status_code == 200
        assert captured["args"] == ("11",)

    def test_districts(self, monkeypatch, client):
        captured = _stub_service(
            monkeypatch, name="get_districts",
            body=[{"district_id": "110507"}],
        )
        resp = client.get("/api/v1/shipping/regions/districts/1105")
        assert resp.status_code == 200
        assert captured["args"] == ("1105",)

    def test_areas(self, monkeypatch, client):
        captured = _stub_service(
            monkeypatch, name="get_areas",
            body=[{"area_id": "1105072002", "name": "Alue Bagok"}],
        )
        resp = client.get("/api/v1/shipping/regions/areas/110507")
        assert resp.status_code == 200
        assert captured["args"] == ("110507",)


# ---- Error path ------------------------------------------------------------


class TestProxyErrors:
    def test_service_error_returns_502(self, monkeypatch, client):
        _stub_service_raises(monkeypatch, name="get_provinces")
        resp = client.get("/api/v1/shipping/regions/provinces")
        assert resp.status_code == 502
        assert "provinces" in resp.json()["detail"].lower()


# ---- /rates — service_category_id passthrough -----------------------------


class TestRatesPassthrough:
    def test_post_rates_accepted_service_category_id(self, monkeypatch, client):
        """When the request body includes service_category_id, the handler
        threads it to carriers.get_rates."""
        captured: dict = {}

        async def _fake_get_rates(
            *, brand, destination_postal_code, items,
            total_value=None, service_category_id=None,
        ):
            captured["service_category_id"] = service_category_id
            captured["destination_postal_code"] = destination_postal_code
            return [{"courier_name": "JNE", "price": 12000}]

        monkeypatch.setattr(carriers_module, "get_rates", _fake_get_rates)

        resp = client.post(
            "/api/v1/shipping/rates",
            json={
                "brand_slug": "safiya",
                "destination_postal_code": "17425",
                "items": [{"sku": "X1", "qty": 1}],
                "service_category_id": 4,
            },
        )
        assert resp.status_code == 200, resp.text
        assert captured["service_category_id"] == 4

    def test_post_rates_without_kwarg_keeps_default(self, monkeypatch, client):
        captured: dict = {}

        async def _fake_get_rates(
            *, brand, destination_postal_code, items,
            total_value=None, service_category_id=None,
        ):
            captured["service_category_id"] = service_category_id
            return []

        monkeypatch.setattr(carriers_module, "get_rates", _fake_get_rates)

        resp = client.post(
            "/api/v1/shipping/rates",
            json={
                "brand_slug": "safiya",
                "destination_postal_code": "17425",
                "items": [{"sku": "X1", "qty": 1}],
            },
        )
        assert resp.status_code == 200, resp.text
        assert captured["service_category_id"] is None
