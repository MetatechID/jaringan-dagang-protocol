"""Tests for ``services.jubelio.get_shipment_by_awb``.

Contract v1.8 §3.3 — GET /shipments/awb/{awb}. Returns the full shipment
detail document including ``tracking[]`` events, ``latest_status``, ETA,
and both origin / destination resolutions. Used as the polling-fallback
when webhooks are missed (PR3 tracking-refresh endpoint).
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


class TestGetShipmentByAwb:
    @pytest.mark.asyncio
    async def test_happy_path_returns_parsed_json(self, monkeypatch):
        sample = {
            "shipment_id": 1428,
            "awb": "CM8439257324384",
            "latest_status": "ON_DELIVERY",
            "tracking_url": "https://track",
            "live_tracking_url": None,
            "courier_name": "JNE",
            "courier_service_name": "JNE REG",
            "origin_name": "Sender",
            "destination_name": "Buyer",
            "tracking": [
                {
                    "date": "2025-01-01T08:00:00+00:00",
                    "status": "OP3",
                    "status_detail": "FORWARDED",
                }
            ],
        }
        captured: dict = {}

        # Stub auth headers + the httpx.AsyncClient.post call to keep the
        # test offline. We reach into the AsyncClient context manager with
        # a minimal fake.
        async def _fake_headers():
            return {"authorization": "Bearer t", "Content-Type": "application/json"}

        class _FakeResp:
            def __init__(self, status_code, body):
                self.status_code = status_code
                self._body = body
                self.text = "" if status_code < 400 else str(body)

            def json(self):
                return self._body

        class _FakeAsyncClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def get(self, url, headers=None):
                captured["url"] = url
                captured["headers"] = headers
                return _FakeResp(200, sample)

        monkeypatch.setattr(jubelio_service, "_auth_headers", _fake_headers)
        monkeypatch.setattr(jubelio_service.httpx, "AsyncClient", _FakeAsyncClient)

        result = await jubelio_service.get_shipment_by_awb("CM8439257324384")

        assert captured["url"].endswith("/shipments/awb/CM8439257324384")
        assert captured["headers"]["authorization"] == "Bearer t"
        assert result["shipment_id"] == 1428
        assert result["latest_status"] == "ON_DELIVERY"
        assert result["tracking"][0]["status"] == "OP3"

    @pytest.mark.asyncio
    async def test_404_raises_shipping_error(self, monkeypatch):
        async def _fake_headers():
            return {"authorization": "Bearer t", "Content-Type": "application/json"}

        class _FakeResp:
            status_code = 404
            text = '{"code":"NOT_FOUND","message":"AWB tidak ditemukan"}'

            def json(self):
                return {}

        class _FakeAsyncClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def get(self, url, headers=None):
                return _FakeResp()

        monkeypatch.setattr(jubelio_service, "_auth_headers", _fake_headers)
        monkeypatch.setattr(jubelio_service.httpx, "AsyncClient", _FakeAsyncClient)

        with pytest.raises(jubelio_service.ShippingError) as ei:
            await jubelio_service.get_shipment_by_awb("MISSING")
        assert "404" in str(ei.value)
        assert "NOT_FOUND" in str(ei.value)
