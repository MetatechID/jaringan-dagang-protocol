"""Tests for the region + service-category pass-throughs.

Contract v1.8 §4.1 and §5.1–§5.5. These are thin wrappers — same shape as
``get_shipment_by_awb`` (PR3). Tests assert URL, headers, return shape,
and that non-2xx raises ``ShippingError``.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

import pytest

from services import jubelio as jubelio_service


def _fake_async_client(monkeypatch, *, status: int, body):
    """Install a FakeAsyncClient capturing the (url, headers, params) of the
    last request, returning ``status``/``body`` from get()."""

    captured: dict = {}

    async def _fake_headers():
        return {"authorization": "Bearer t", "Content-Type": "application/json"}

    class _FakeResp:
        def __init__(self):
            self.status_code = status
            self.text = "" if status < 400 else str(body)

        def json(self):
            return body

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url, headers=None, params=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["params"] = params
            return _FakeResp()

    monkeypatch.setattr(jubelio_service, "_auth_headers", _fake_headers)
    monkeypatch.setattr(jubelio_service.httpx, "AsyncClient", _FakeAsyncClient)
    return captured


# ---- Service categories §4.1 ----------------------------------------------


class TestServiceCategories:
    @pytest.mark.asyncio
    async def test_happy_path(self, monkeypatch):
        sample = [
            {"service_category_id": 1, "name": "REGULER"},
            {"service_category_id": 4, "name": "INSTANT"},
        ]
        captured: dict = {}

        async def _fake_headers():
            return {"authorization": "Bearer t", "Content-Type": "application/json"}

        class _Resp:
            status_code = 200
            text = ""
            def json(self): return sample

        class _Client:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def get(self, url, headers=None, params=None):
                captured["url"] = url
                return _Resp()

        monkeypatch.setattr(jubelio_service, "_auth_headers", _fake_headers)
        monkeypatch.setattr(jubelio_service.httpx, "AsyncClient", _Client)

        result = await jubelio_service.get_service_categories()

        assert captured["url"].endswith("/services/categories")
        assert result is sample  # pass-through returns resp.json()

    @pytest.mark.asyncio
    async def test_error_raises(self, monkeypatch):
        with pytest.raises(jubelio_service.ShippingError) as ei:
            # Reuse the generic 4xx path via _fake_async_client.
            captured = _fake_async_client(
                monkeypatch, status=500, body={"message": "boom"},
            )
            await jubelio_service.get_service_categories()
        assert "500" in str(ei.value)


# ---- Regions §5.1–§5.5 ----------------------------------------------------


class TestRegions:
    @pytest.mark.asyncio
    async def test_regions_no_query(self, monkeypatch):
        captured = _fake_async_client(monkeypatch, status=200, body=[])
        result = await jubelio_service.get_regions()
        assert captured["url"].endswith("/regions")
        assert captured["params"] is None
        assert result == []

    @pytest.mark.asyncio
    async def test_regions_with_name_query_passes_through(self, monkeypatch):
        captured = _fake_async_client(monkeypatch, status=200, body=[])
        result = await jubelio_service.get_regions(name="jakarta")
        assert captured["url"].endswith("/regions")
        assert captured["params"] == {"name": "jakarta"}

    @pytest.mark.asyncio
    async def test_provinces(self, monkeypatch):
        captured = _fake_async_client(
            monkeypatch, status=200,
            body=[{"province_id": "11", "name": "ACEH"}],
        )
        result = await jubelio_service.get_provinces()
        assert captured["url"].endswith("/region/provinces")
        assert result == [{"province_id": "11", "name": "ACEH"}]

    @pytest.mark.asyncio
    async def test_cities(self, monkeypatch):
        captured = _fake_async_client(
            monkeypatch, status=200,
            body=[{"city_id": "1105", "province_id": "11", "name": "KAB. ACEH BARAT"}],
        )
        result = await jubelio_service.get_cities("11")
        assert captured["url"].endswith("/region/cities/11")
        assert result[0]["city_id"] == "1105"

    @pytest.mark.asyncio
    async def test_districts(self, monkeypatch):
        captured = _fake_async_client(
            monkeypatch, status=200,
            body=[{"district_id": "110507", "city_id": "1105", "name": "Bubon"}],
        )
        result = await jubelio_service.get_districts("1105")
        assert captured["url"].endswith("/region/districts/1105")
        assert result[0]["district_id"] == "110507"

    @pytest.mark.asyncio
    async def test_areas(self, monkeypatch):
        captured = _fake_async_client(
            monkeypatch, status=200,
            body=[{
                "area_id": "1105072002",
                "district_id": "110507",
                "name": "Alue Bagok",
                "zipcode": "22394",
            }],
        )
        result = await jubelio_service.get_areas("110507")
        assert captured["url"].endswith("/region/areas/110507")
        assert result[0]["area_id"] == "1105072002"
        assert result[0]["zipcode"] == "22394"

    @pytest.mark.asyncio
    async def test_areas_404_raises(self, monkeypatch):
        with pytest.raises(jubelio_service.ShippingError) as ei:
            _fake_async_client(
                monkeypatch, status=404,
                body={"message": "district not found"},
            )
            await jubelio_service.get_areas("99999")
        assert "404" in str(ei.value)
