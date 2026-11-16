"""POST /api/v1/auth/password/login: operator-created email + password buyers.

Same harness as test_otp_routes_in_process: in-memory sqlite, the auth router
alone, and a fake custom-token mint (no Firebase Admin creds).
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

os.environ.setdefault(
    "FIREBASE_SERVICE_ACCOUNT_JSON", '{"type":"service_account","project_id":"test"}'
)

PASSWORD = "kata-sandi-panjang"


@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from auth import passwords
    from database import get_db
    from models.base import Base
    from models.otp_code import OtpCode  # noqa: F401
    from models.profile import BeliAmanProfile
    from models.store_membership import StoreMembership  # noqa: F401
    from routers.auth import router as auth_router

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def _bootstrap():
        async with engine.begin() as conn:
            await conn.run_sync(
                Base.metadata.create_all,
                tables=[BeliAmanProfile.__table__, OtpCode.__table__, StoreMembership.__table__],
            )
        async with Session() as db:
            db.add(BeliAmanProfile(email="buyer@example.com", display_name="Demo Buyer",
                                   password_hash=passwords.hash_password(PASSWORD)))
            db.add(BeliAmanProfile(email="google@example.com", google_sub="g-1"))
            await db.commit()

    asyncio.run(_bootstrap())
    passwords.login_limiter.reset()
    monkeypatch.setattr("routers.auth.mint_custom_token", lambda uid, claims=None: f"fake-custom-token::{uid}")

    async def override_get_db():
        async with Session() as db:
            yield db

    app = FastAPI()
    app.include_router(auth_router)
    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    passwords.login_limiter.reset()


def login(tc, email="buyer@example.com", password=PASSWORD):
    return tc.post("/api/v1/auth/password/login", json={"email": email, "password": password})


def test_login_returns_a_custom_token_for_the_profile(client):
    r = login(client, email="  Buyer@Example.COM ")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["custom_token"] == f"fake-custom-token::{body['profile']['id']}"
    assert body["profile"]["email"] == "buyer@example.com"
    assert body["profile"]["display_name"] == "Demo Buyer"
    assert "password_hash" not in body["profile"]


@pytest.mark.parametrize(
    "email,password",
    [
        ("buyer@example.com", "wrong-password"),
        ("nobody@example.com", PASSWORD),
        ("google@example.com", PASSWORD),  # Google-only profile has no password
        ("not-an-email", PASSWORD),
    ],
)
def test_failures_are_indistinguishable(client, email, password):
    r = login(client, email, password)
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_credentials"


def test_repeated_failures_are_throttled(client):
    from auth.passwords import MAX_FAILURES

    for _ in range(MAX_FAILURES):
        assert login(client, password="wrong-password").status_code == 401
    r = login(client)  # even the right password, until the window passes
    assert r.status_code == 429
    assert login(client, email="other@example.com", password="x").status_code == 401


def test_success_clears_earlier_failures(client):
    from auth.passwords import MAX_FAILURES

    for _ in range(MAX_FAILURES - 1):
        login(client, password="wrong-password")
    assert login(client).status_code == 200
    for _ in range(MAX_FAILURES - 1):
        login(client, password="wrong-password")
    assert login(client).status_code == 200


def test_hashes_round_trip_and_reject_garbage():
    from auth.passwords import hash_password, verify_password

    h = hash_password(PASSWORD)
    assert h.startswith("scrypt$") and PASSWORD not in h
    assert verify_password(PASSWORD, h)
    assert not verify_password("nope", h)
    assert not verify_password(PASSWORD, None)
    assert not verify_password(PASSWORD, "bcrypt$x")
