"""Tests for the ``service_category_id`` passthrough on carriers.get_rates.

PR4 adds an optional kwarg to ``services.carriers.get_rates`` that, for
Jubelio brands, is forwarded to ``services.jubelio.get_rates``. Biteship
ignores the kwarg (Biteship returns all categories in one call).
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
    )


@pytest.mark.asyncio
async def test_jubelio_passes_service_category_id_through(monkeypatch):
    brand = _stub_brand(jubelio_enabled=True)
    captured: dict = {}

    async def _fake_get_rates(
        *,
        origin_zipcode,
        destination_zipcode,
        items,
        origin_area_id=None,
        destination_area_id=None,
        origin_coordinate=None,
        destination_coordinate=None,
        total_value=None,
        service_category_id=None,
    ):
        captured["service_category_id"] = service_category_id
        return [{"courier_name": "JNE"}]

    monkeypatch.setattr(jubelio_service, "get_rates", _fake_get_rates)

    await carriers.get_rates(
        brand=brand,
        destination_postal_code="17425",
        items=[{"name": "x", "value": 100, "weight": 100, "quantity": 1}],
        service_category_id=4,
    )

    assert captured["service_category_id"] == 4


@pytest.mark.asyncio
async def test_jubelio_without_kwarg_keeps_default_none(monkeypatch):
    """Backwards-compat smoke: callers that don't pass the kwarg still work."""
    brand = _stub_brand(jubelio_enabled=True)
    captured: dict = {}

    async def _fake_get_rates(
        *,
        origin_zipcode,
        destination_zipcode,
        items,
        origin_area_id=None,
        destination_area_id=None,
        origin_coordinate=None,
        destination_coordinate=None,
        total_value=None,
        service_category_id=None,
    ):
        captured["service_category_id"] = service_category_id
        return []

    monkeypatch.setattr(jubelio_service, "get_rates", _fake_get_rates)

    await carriers.get_rates(
        brand=brand,
        destination_postal_code="17425",
        items=[{"name": "x", "value": 100, "weight": 100, "quantity": 1}],
    )

    assert captured["service_category_id"] is None
