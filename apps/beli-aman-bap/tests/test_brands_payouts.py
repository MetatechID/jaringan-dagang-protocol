"""Tests for /api/v1/brands/{slug}/payouts GET + PUT.

Covers the unified payout-bank-accounts surface: ``bank_accounts`` CRUD
(create/edit via ``bank_accounts``, delete via ``delete_bank_account_ids``,
single-active via ``active_bank_account_id``), plus the dynamic
provider/courier surface: ``payment_provider`` (xendit/sento/oy/dipay),
``courier_provider`` (biteship/jubelio) and ``jubelio_origin_address``
roundtrip + shape validation.

Most tests use a pure stub brand (no DB, no async) because
``_payouts_view`` and ``_validate_jubelio_origin`` are pure functions.
Only the bank-account CRUD + PUT whitelist tests run through a
sqlite-backed FastAPI TestClient to assert the DB roundtrip actually
works end to end.
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
        dipay_client_key=None,
        dipay_client_secret=None,
        dipay_private_key_b64=None,
        dipay_merchant_id=None,
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

        view = _payouts_view(_brand(), [])
        assert view["slug"] == "antarestar"
        assert view["payment_provider"] == "xendit"
        assert view["courier_provider"] == "biteship"
        assert view["jubelio_origin_address"] is None
        assert view["bank_accounts"] == []
        assert view["dipay_client_key_configured"] is False
        assert view["dipay_client_secret_configured"] is False
        assert view["dipay_private_key_configured"] is False

    def test_dipay_secrets_are_masked_as_configuration_flags(self):
        from routers.brands import _payouts_view

        view = _payouts_view(_brand(
            dipay_client_key="client-key-1234",
            dipay_client_secret="never-return-me",
            dipay_private_key_b64="never-return-key",
        ), [])
        assert view["dipay_client_key_masked"] == "•••• 1234"
        assert view["dipay_client_key_configured"] is True
        assert view["dipay_client_secret_configured"] is True
        assert view["dipay_private_key_configured"] is True
        assert "dipay_client_secret" not in view
        assert "dipay_private_key_b64" not in view

    def test_bank_accounts_are_masked_with_labels_and_support(self):
        from routers.brands import _payouts_view

        account = SimpleNamespace(
            id="acc-1",
            bank_code="014",
            account_number="1234567890",
            holder_name="Budi",
            is_active=True,
        )
        view = _payouts_view(_brand(), [account])
        assert view["bank_accounts"] == [
            {
                "id": "acc-1",
                "bank_code": "014",
                "bank_label": "BCA",
                "account_number_masked": "•••• 7890",
                "holder_name": "Budi",
                "is_active": True,
                "supported_providers": ["xendit", "sento", "dipay"],
            }
        ]

    def test_mask_short_account_number_returns_unchanged(self):
        """Account numbers < 5 chars are not masked (no last-4 to show)."""
        from routers.brands import _payouts_view

        account = SimpleNamespace(
            id="acc-1",
            bank_code="014",
            account_number="1234",
            holder_name="Budi",
            is_active=False,
        )
        view = _payouts_view(_brand(), [account])
        assert view["bank_accounts"][0]["account_number_masked"] == "1234"

    def test_bank_keys_expose_shared_picker(self):
        from routers.brands import _payouts_view

        view = _payouts_view(_brand(), [])
        keys = {k["code"]: k for k in view["bank_keys"]}
        assert keys["014"]["label"] == "BCA"
        assert keys["014"]["supported"] == ["xendit", "sento", "dipay"]
        # A Dipay-only digital bank is supported by exactly one gateway.
        assert keys["535"]["supported"] == ["dipay"]

    def test_payment_provider_sento_round_trips(self):
        from routers.brands import _payouts_view

        view = _payouts_view(_brand(payment_provider="sento"), [])
        assert view["payment_provider"] == "sento"

    def test_payment_provider_oy_is_allowed(self):
        from routers.brands import _payouts_view

        view = _payouts_view(_brand(payment_provider="oy"), [])
        assert view["payment_provider"] == "oy"

    def test_payment_provider_unknown_value_falls_back_to_xendit(self):
        from routers.brands import _payouts_view

        view = _payouts_view(_brand(payment_provider="unknown"), [])
        assert view["payment_provider"] == "xendit"

    def test_payment_provider_none_falls_back_to_xendit(self):
        from routers.brands import _payouts_view

        view = _payouts_view(_brand(payment_provider=None), [])
        assert view["payment_provider"] == "xendit"

    def test_courier_provider_jubelio_when_jubelio_enabled(self):
        from routers.brands import _payouts_view

        view = _payouts_view(_brand(jubelio_enabled=True), [])
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
        view = _payouts_view(_brand(jubelio_origin_address=origin), [])
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
# Bank-account CRUD + the PUT body whitelist need a real database. Use the
# minimal in-memory sqlite harness so we exercise the same code path as
# production (Pydantic validation → PayoutsIn → handler → rows).


@pytest.fixture
def client():
    """Fresh sqlite-backed FastAPI app with the brand router mounted."""
    import asyncio
    import uuid

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import (
        async_sessionmaker,
        create_async_engine,
    )

    from database import get_db
    from deps import get_current_profile
    from models.base import Base
    from models.brand import Brand
    from models.payout_bank_account import PayoutBankAccount
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
                    PayoutBankAccount.__table__,
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

    async def _fetch_brand(slug: str):
        from sqlalchemy import select

        async with Session() as s:
            return (
                await s.execute(select(Brand).where(Brand.slug == slug))
            ).scalar_one_or_none()

    async def _fetch_accounts(slug: str) -> list:
        from sqlalchemy import select

        async with Session() as s:
            brand = (
                await s.execute(select(Brand).where(Brand.slug == slug))
            ).scalar_one()
            return (
                await s.execute(
                    select(PayoutBankAccount)
                    .where(PayoutBankAccount.brand_id == brand.id)
                    .order_by(PayoutBankAccount.created_at)
                )
            ).scalars().all()

    return TestClient(app), Session, _seed, _fetch_brand, _fetch_accounts


def _put(tc, slug, payload):
    return tc.put(f"/api/v1/brands/{slug}/payouts", json=payload)


def test_put_payouts_toggles_payment_provider_in_db(client):
    """End-to-end: PUT flips brand.payment_provider and view reflects it."""
    import asyncio

    tc, _Session, seed, _fetch_brand, _fetch_accounts = client
    asyncio.run(seed())

    r = _put(tc, "antarestar", {"payment_provider": "sento"})
    assert r.status_code == 200, r.text
    assert r.json()["payment_provider"] == "sento"

    brand = asyncio.run(_fetch_brand("antarestar"))
    assert brand is not None and brand.payment_provider == "sento"


def test_put_payouts_oy_remains_allowed(client):
    import asyncio

    tc, _Session, seed, _fetch_brand, _fetch_accounts = client
    asyncio.run(seed())

    r = _put(tc, "antarestar", {"payment_provider": "oy"})
    assert r.status_code == 200, r.text
    assert r.json()["payment_provider"] == "oy"

    brand = asyncio.run(_fetch_brand("antarestar"))
    assert brand is not None and brand.payment_provider == "oy"


def test_put_payouts_rejects_unknown_provider(client):
    import asyncio

    tc, _Session, seed, _fetch_brand, _fetch_accounts = client
    asyncio.run(seed())
    r = _put(tc, "antarestar", {"payment_provider": "bogus"})
    assert r.status_code == 422


def test_put_payouts_garbage_courier_falls_back_in_db(client):
    """End-to-end: PUT unknown courier keeps ``jubelio_enabled`` False."""
    import asyncio

    tc, _Session, seed, _fetch_brand, _fetch_accounts = client
    asyncio.run(seed())

    r = _put(tc, "antarestar", {"courier_provider": "fancourier"})
    assert r.status_code == 200, r.text
    assert r.json()["courier_provider"] == "biteship"

    brand = asyncio.run(_fetch_brand("antarestar"))
    assert brand is not None and brand.jubelio_enabled is False


def test_put_payouts_preserves_hidden_fields(client):
    """Switching PG/courier does not wipe other columns on the row."""
    import asyncio

    tc, _Session, seed, _fetch_brand, _fetch_accounts = client
    asyncio.run(seed())

    setup = {
        "xendit_sub_account_id": "64a1b2c3d4e5f67890123456",
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
    assert body["biteship_origin_address"] == {"contact_name": "WH Jakarta"}


def test_put_payouts_jubelio_origin_null_clears_field(client):
    """``jubelio_origin_address: null`` clears the column."""
    import asyncio

    tc, Session, seed, _fetch_brand, _fetch_accounts = client
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


# ---------------------------------------------------------------------------
# Bank-account CRUD end-to-end
# ---------------------------------------------------------------------------


def test_bank_account_create_and_set_active(client):
    """Create two accounts, activate the second — first deactivates."""
    import asyncio

    tc, _Session, seed, _fetch_brand, fetch_accounts = client
    asyncio.run(seed())

    r = _put(tc, "antarestar", {"bank_accounts": [
        {"bank_code": "014", "account_number": "111222333", "holder_name": "Budi"},
        {"bank_code": "008", "account_number": "444555666", "holder_name": "Ani"},
    ]})
    assert r.status_code == 200, r.text
    accounts = r.json()["bank_accounts"]
    assert [a["is_active"] for a in accounts] == [False, False]

    first_id = accounts[0]["id"]
    second_id = accounts[1]["id"]
    r = _put(tc, "antarestar", {"active_bank_account_id": second_id})
    assert r.status_code == 200, r.text
    accounts = {a["id"]: a for a in r.json()["bank_accounts"]}
    assert accounts[first_id]["is_active"] is False
    assert accounts[second_id]["is_active"] is True

    rows = asyncio.run(fetch_accounts("antarestar"))
    assert len(rows) == 2


def test_bank_account_edit_keeps_account_number_when_omitted(client):
    """Editing with no account_number preserves the stored (write-only) one."""
    import asyncio

    tc, _Session, seed, _fetch_brand, fetch_accounts = client
    asyncio.run(seed())

    r = _put(tc, "antarestar", {"bank_accounts": [
        {"bank_code": "014", "account_number": "111222333", "holder_name": "Budi"},
    ]})
    account_id = r.json()["bank_accounts"][0]["id"]

    # GET returns only the masked number; PUT with just a holder edit.
    r = _put(tc, "antarestar", {"bank_accounts": [
        {"id": account_id, "holder_name": "Budi Santoso"},
    ]})
    assert r.status_code == 200, r.text
    accounts = r.json()["bank_accounts"]
    assert accounts[0]["holder_name"] == "Budi Santoso"
    assert accounts[0]["account_number_masked"] == "•••• 2333"

    rows = asyncio.run(fetch_accounts("antarestar"))
    assert rows[0].account_number == "111222333"


def test_bank_account_edit_replaces_account_number(client):
    import asyncio

    tc, _Session, seed, _fetch_brand, fetch_accounts = client
    asyncio.run(seed())

    r = _put(tc, "antarestar", {"bank_accounts": [
        {"bank_code": "014", "account_number": "111222333", "holder_name": "Budi"},
    ]})
    account_id = r.json()["bank_accounts"][0]["id"]

    r = _put(tc, "antarestar", {"bank_accounts": [
        {"id": account_id, "account_number": "999888777"},
    ]})
    assert r.status_code == 200, r.text
    rows = asyncio.run(fetch_accounts("antarestar"))
    assert rows[0].account_number == "999888777"


def test_bank_account_delete(client):
    import asyncio

    tc, _Session, seed, _fetch_brand, fetch_accounts = client
    asyncio.run(seed())

    r = _put(tc, "antarestar", {"bank_accounts": [
        {"bank_code": "014", "account_number": "111222333", "holder_name": "Budi"},
        {"bank_code": "008", "account_number": "444555666", "holder_name": "Ani"},
    ]})
    ids = [a["id"] for a in r.json()["bank_accounts"]]

    r = _put(tc, "antarestar", {"delete_bank_account_ids": [ids[0]]})
    assert r.status_code == 200, r.text
    assert [a["id"] for a in r.json()["bank_accounts"]] == [ids[1]]

    rows = asyncio.run(fetch_accounts("antarestar"))
    assert [row.id for row in rows] == [ids[1]]


def test_bank_account_delete_missing_id_404(client):
    import asyncio

    tc, _Session, seed, _fetch_brand, _fetch_accounts = client
    asyncio.run(seed())
    r = _put(tc, "antarestar", {"delete_bank_account_ids": ["nope"]})
    assert r.status_code == 404


def test_bank_account_active_unknown_id_404(client):
    import asyncio

    tc, _Session, seed, _fetch_brand, _fetch_accounts = client
    asyncio.run(seed())
    r = _put(tc, "antarestar", {"active_bank_account_id": "nope"})
    assert r.status_code == 404


def test_bank_account_unknown_bank_code_422(client):
    import asyncio

    tc, _Session, seed, _fetch_brand, _fetch_accounts = client
    asyncio.run(seed())
    r = _put(tc, "antarestar", {"bank_accounts": [
        {"bank_code": "XXX", "account_number": "111222333"},
    ]})
    assert r.status_code == 422


def test_bank_account_create_requires_account_number(client):
    import asyncio

    tc, _Session, seed, _fetch_brand, _fetch_accounts = client
    asyncio.run(seed())
    r = _put(tc, "antarestar", {"bank_accounts": [{"bank_code": "014"}]})
    assert r.status_code == 422


def test_bank_account_create_requires_bank_code(client):
    import asyncio

    tc, _Session, seed, _fetch_brand, _fetch_accounts = client
    asyncio.run(seed())
    r = _put(tc, "antarestar", {"bank_accounts": [{"account_number": "111"}]})
    assert r.status_code == 422


def test_dipay_provider_requires_active_bank_account(client):
    """The dipay 422 gate now keys off the unified active account."""
    import asyncio

    tc, _Session, seed, _fetch_brand, fetch_accounts = client
    asyncio.run(seed())

    # No accounts at all → 422.
    r = _put(tc, "antarestar", {"payment_provider": "dipay"})
    assert r.status_code == 422

    # Adding an account (inactive) is still not enough.
    r = _put(tc, "antarestar", {"bank_accounts": [
        {"bank_code": "014", "account_number": "111222333"},
    ]})
    account_id = r.json()["bank_accounts"][0]["id"]
    r = _put(tc, "antarestar", {"payment_provider": "dipay"})
    assert r.status_code == 422

    # Activate it → gate passes (creds resolve via env in this env-less
    # test app: resolve_config is exercised by dipay-specific tests).
    r = _put(tc, "antarestar", {"active_bank_account_id": account_id})
    assert r.status_code == 200, r.text
    # payment_provider is not flipped here because Dipay credentials are
    # incomplete in the test env — assert the bank-account part worked.
    rows = asyncio.run(fetch_accounts("antarestar"))
    assert rows[0].is_active is True


def test_active_account_survives_other_puts(client):
    """A PUT touching other fields must not disturb the active flag."""
    import asyncio

    tc, _Session, seed, _fetch_brand, fetch_accounts = client
    asyncio.run(seed())

    r = _put(tc, "antarestar", {"bank_accounts": [
        {"bank_code": "014", "account_number": "111222333"},
    ]})
    account_id = r.json()["bank_accounts"][0]["id"]
    _put(tc, "antarestar", {"active_bank_account_id": account_id})
    _put(tc, "antarestar", {"biteship_default_courier": "jne:reg"})

    rows = asyncio.run(fetch_accounts("antarestar"))
    assert rows[0].is_active is True
