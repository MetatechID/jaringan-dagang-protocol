"""Tests for /api/v1/brands/{slug}/payouts GET + PUT.

Covers the dynamic provider/courier surface added in the Payouts admin
refactor: ``payment_provider`` (xendit/sento), ``courier_provider``
(biteship/jubelio) and ``jubelio_origin_address`` roundtrip +
shape validation.

Most tests use a pure stub brand (no DB, no async) because
``_payouts_view`` and ``_validate_jubelio_origin`` are pure functions.
Only the end-to-end happy path runs through a sqlite-backed FastAPI
TestClient to assert the PUT body whitelist + DB roundtrip actually
work end to end.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)


# ---------------------------------------------------------------------------
# Stub brand — _payouts_view only needs the columns it reads.
# ---------------------------------------------------------------------------


def _brand(**overrides) -> SimpleNamespace:
    base = dict(
        slug="antarestar",
        xendit_sub_account_id=None,
        xendit_disbursement_bank_code=None,
        xendit_disbursement_bank_account=None,
        xendit_disbursement_holder_name=None,
        sento_disbursement_bank_code=None,
        sento_disbursement_bank_account=None,
        sento_disbursement_holder_name=None,
        biteship_origin_address=None,
        biteship_default_courier=None,
        payment_provider="xendit",
        jubelio_enabled=False,
        jubelio_origin_address=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# _payouts_view — pure function, no DB
# ---------------------------------------------------------------------------


class TestPayoutsView:
    def test_defaults_when_brand_row_is_fresh(self):
        from routers.brands import _payouts_view

        view = _payouts_view(_brand())
        assert view["slug"] == "antarestar"
        assert view["payment_provider"] == "xendit"
        assert view["courier_provider"] == "biteship"
        assert view["jubelio_origin_address"] is None
        assert view["xendit_disbursement_bank_account_masked"] is None
        assert view["sento_disbursement_bank_account_masked"] is None

    def test_mask_hides_xendit_account_number(self):
        from routers.brands import _payouts_view

        view = _payouts_view(_brand(xendit_disbursement_bank_account="1234567890"))
        assert view["xendit_disbursement_bank_account_masked"] == "•••• 7890"

    def test_mask_short_xendit_account_returns_unchanged(self):
        """Account numbers < 5 chars are not masked (no last-4 to show)."""
        from routers.brands import _payouts_view

        view = _payouts_view(_brand(xendit_disbursement_bank_account="1234"))
        assert view["xendit_disbursement_bank_account_masked"] == "1234"

    def test_payment_provider_sento_round_trips(self):
        from routers.brands import _payouts_view

        view = _payouts_view(_brand(payment_provider="sento"))
        assert view["payment_provider"] == "sento"

    def test_payment_provider_unknown_value_falls_back_to_xendit(self):
        """Mirrors the dispatch fallback at routers/checkout.py:146."""
        from routers.brands import _payouts_view

        view = _payouts_view(_brand(payment_provider="oy"))
        assert view["payment_provider"] == "xendit"

    def test_payment_provider_none_falls_back_to_xendit(self):
        from routers.brands import _payouts_view

        view = _payouts_view(_brand(payment_provider=None))
        assert view["payment_provider"] == "xendit"

    def test_courier_provider_jubelio_when_jubelio_enabled(self):
        from routers.brands import _payouts_view

        view = _payouts_view(_brand(jubelio_enabled=True))
        assert view["courier_provider"] == "jubelio"

    def test_jubelio_origin_address_round_trips(self):
        from routers.brands import _payouts_view

        origin = {
            "name": "HQ",
            "phone": "+62123",
            "address": "Jl. Test 1",
            "area_id": "12345",
            "zipcode": "40115",
            "coordinate": [-6.2, 106.8],
        }
        view = _payouts_view(_brand(jubelio_origin_address=origin))
        assert view["jubelio_origin_address"] == origin


# ---------------------------------------------------------------------------
# _validate_jubelio_origin — pure function, no DB
# ---------------------------------------------------------------------------


class TestValidateJubelioOrigin:
    def test_none_returns_none(self):
        from routers.brands import _validate_jubelio_origin

        assert _validate_jubelio_origin(None) is None

    def test_empty_dict_returns_empty_dict(self):
        from routers.brands import _validate_jubelio_origin

        assert _validate_jubelio_origin({}) == {}

    def test_known_keys_pass(self):
        from fastapi import HTTPException

        from routers.brands import _validate_jubelio_origin

        origin = {
            "name": "HQ",
            "phone": "+62123",
            "email": "x@y.id",
            "address": "Jl. Test 1",
            "area_id": "12345",
            "zipcode": "40115",
            "coordinate": [0.0, 0.0],
        }
        assert _validate_jubelio_origin(origin) == origin

    def test_unknown_key_rejected_422(self):
        from fastapi import HTTPException

        from routers.brands import _validate_jubelio_origin

        with pytest.raises(HTTPException) as ei:
            _validate_jubelio_origin({"foo": 1})
        assert ei.value.status_code == 422
        assert "unknown keys" in ei.value.detail

    def test_non_dict_rejected_422(self):
        from fastapi import HTTPException

        from routers.brands import _validate_jubelio_origin

        with pytest.raises(HTTPException) as ei:
            _validate_jubelio_origin("not a dict")
        assert ei.value.status_code == 422

    def test_coordinate_string_rejected_422(self):
        from fastapi import HTTPException

        from routers.brands import _validate_jubelio_origin

        with pytest.raises(HTTPException) as ei:
            _validate_jubelio_origin({"coordinate": "bogus"})
        assert ei.value.status_code == 422
        assert "coordinate" in ei.value.detail

    def test_coordinate_wrong_length_rejected_422(self):
        from fastapi import HTTPException

        from routers.brands import _validate_jubelio_origin

        with pytest.raises(HTTPException) as ei:
            _validate_jubelio_origin({"coordinate": [1.0]})
        assert ei.value.status_code == 422

    def test_coordinate_nan_rejected_422(self):
        from fastapi import HTTPException

        from routers.brands import _validate_jubelio_origin

        with pytest.raises(HTTPException) as ei:
            _validate_jubelio_origin({"coordinate": ["NaN", 0]})
        assert ei.value.status_code == 422

    def test_coordinate_string_values_rejected_422(self):
        from fastapi import HTTPException

        from routers.brands import _validate_jubelio_origin

        with pytest.raises(HTTPException) as ei:
            _validate_jubelio_origin({"coordinate": ["abc", "def"]})
        assert ei.value.status_code == 422


# ---------------------------------------------------------------------------
# put_payouts end-to-end — sqlite-backed FastAPI test client
# ---------------------------------------------------------------------------
#
# Only the PUT body whitelist + DB roundtrip need a real database. Use the
# minimal in-memory sqlite harness so we exercise the same code path as
# production (Pydantic validation → PayoutsIn → handler → brand row).
# pytest-aio+aiosqlite handle async under the sync TestClient automatically.
#


@pytest.fixture
def client():
    """Fresh sqlite-backed FastAPI app with the brand router mounted."""
    import asyncio
    import uuid

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from database import get_db
    from deps import get_current_profile
    from models.base import Base
    from models.brand import Brand
    from models.profile import BeliAmanProfile
    from models.store_membership import StoreMembership
    from routers.brands import router as brands_router

    # Postgres JSONB is not representable in sqlite (column-level). For this
    # one test that needs a real Brand row, swap the dialect compiler so
    # JSONB renders as JSON. Production dispatch is unchanged.
    from sqlalchemy.dialects.sqlite import base as _sqlite_base

    if not getattr(_sqlite_base.SQLiteTypeCompiler, "_visit_JSONB_patched", False):
        _sqlite_base.SQLiteTypeCompiler.visit_JSONB = (
            _sqlite_base.SQLiteTypeCompiler.visit_JSON
        )
        _sqlite_base.SQLiteTypeCompiler._visit_JSONB_patched = True

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def _bootstrap():
        async with engine.begin() as conn:
            await conn.run_sync(
                Base.metadata.create_all,
                tables=[
                    Brand.__table__,
                    BeliAmanProfile.__table__,
                    StoreMembership.__table__,
                ],
                checkfirst=True,
            )

    asyncio.run(_bootstrap())

    async def _override_db():
        async with Session() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    def _fake_profile(
        authorization: str | None = None,
        db: AsyncSession = None,
    ) -> BeliAmanProfile:
        return BeliAmanProfile(
            id=str(uuid.uuid4()),
            is_super_admin=True,
            email="admin@test",
        )

    app = FastAPI()
    app.include_router(brands_router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_profile] = _fake_profile

    async def _seed(slug: str = "antarestar", name: str = "Antarestar") -> None:
        async with Session() as s:
            s.add(Brand(slug=slug, name=name, bpp_id=f"{slug}.bpp"))
            await s.commit()

    async def _fetch(slug: str):
        from sqlalchemy import select

        async with Session() as s:
            return (
                await s.execute(select(Brand).where(Brand.slug == slug))
            ).scalar_one_or_none()

    return TestClient(app), Session, _seed, _fetch


def _put(tc, slug, payload):
    return tc.put(f"/api/v1/brands/{slug}/payouts", json=payload)


def test_put_payouts_toggles_payment_provider_in_db(client):
    """End-to-end: PUT flips brand.payment_provider and view reflects it."""
    import asyncio

    tc, _Session, seed, _fetch = client
    asyncio.run(seed())

    r = _put(tc, "antarestar", {"payment_provider": "sento"})
    assert r.status_code == 200, r.text
    assert r.json()["payment_provider"] == "sento"

    brand = asyncio.run(_fetch("antarestar"))
    assert brand is not None and brand.payment_provider == "sento"


def test_put_payouts_garbage_payment_provider_falls_back_in_db(client):
    """End-to-end: PUT ``"oy"`` falls back to ``"xendit"`` on the row."""
    import asyncio

    tc, _Session, seed, _fetch = client
    asyncio.run(seed())

    r = _put(tc, "antarestar", {"payment_provider": "oy"})
    assert r.status_code == 200, r.text
    assert r.json()["payment_provider"] == "xendit"

    brand = asyncio.run(_fetch("antarestar"))
    assert brand is not None and brand.payment_provider == "xendit"


def test_put_payouts_garbage_courier_falls_back_in_db(client):
    """End-to-end: PUT unknown courier keeps ``jubelio_enabled`` False."""
    import asyncio

    tc, _Session, seed, _fetch = client
    asyncio.run(seed())

    r = _put(tc, "antarestar", {"courier_provider": "fancourier"})
    assert r.status_code == 200, r.text
    assert r.json()["courier_provider"] == "biteship"

    brand = asyncio.run(_fetch("antarestar"))
    assert brand is not None and brand.jubelio_enabled is False


def test_put_payouts_preserves_hidden_fields(client):
    """Switching PG/courier does not wipe other columns on the row."""
    import asyncio

    tc, _Session, seed, _fetch = client
    asyncio.run(seed())

    setup = {
        "xendit_sub_account_id": "64a1b2c3d4e5f67890123456",
        "xendit_disbursement_bank_code": "BCA",
        "biteship_origin_address": {"contact_name": "WH Jakarta"},
    }
    assert _put(tc, "antarestar", setup).status_code == 200

    r = _put(tc, "antarestar", {
        "payment_provider": "sento",
        "courier_provider": "jubelio",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["xendit_sub_account_id"] == "64a1b2c3d4e5f67890123456"
    assert body["xendit_disbursement_bank_code"] == "BCA"
    assert body["biteship_origin_address"] == {"contact_name": "WH Jakarta"}


def test_put_payouts_jubelio_origin_null_clears_field(client):
    """``jubelio_origin_address: null`` clears the column."""
    import asyncio

    tc, Session, seed, _fetch = client
    asyncio.run(seed())

    _put(tc, "antarestar", {
        "jubelio_origin_address": {"name": "HQ", "coordinate": [0, 0]}
    })

    r = _put(tc, "antarestar", {"jubelio_origin_address": None})
    assert r.status_code == 200, r.text
    assert r.json()["jubelio_origin_address"] is None

    from models.brand import Brand
    from sqlalchemy import select

    async def _val():
        async with Session() as s:
            return (
                await s.execute(select(Brand).where(Brand.slug == "antarestar"))
            ).scalar_one().jubelio_origin_address

    assert asyncio.run(_val()) is None