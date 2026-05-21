"""Tests for the OTP passwordless-login stack — code generation, store, and
the contact-keyed profile resolver / auto-merge.

These tests use an in-memory SQLite (via ``aiosqlite``) so they don't require
a Postgres instance. Schema is created from ``Base.metadata.create_all`` —
the OTP store + profile auto-merge logic doesn't use Postgres-only types.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)


# ----- pure-function checks (no DB) ------------------------------------------


def test_generate_code_is_six_digits():
    from services import otp_store

    for _ in range(50):
        code = otp_store.generate_code()
        assert len(code) == 6
        assert code.isdigit()


def test_hash_code_is_stable_and_deterministic():
    from services import otp_store

    h1 = otp_store.hash_code("123456")
    h2 = otp_store.hash_code("123456")
    h3 = otp_store.hash_code("123457")
    assert h1 == h2
    assert h1 != h3
    # sha256 hex length
    assert len(h1) == 64


def test_normalize_email():
    from routers.auth import _normalize

    assert _normalize("email", "USER@Example.com") == "user@example.com"
    assert _normalize("email", " user@x.io ") == "user@x.io"
    assert _normalize("email", "notanemail") is None
    assert _normalize("email", "") is None


def test_normalize_phone_e164():
    from routers.auth import _normalize

    # Already E.164
    assert _normalize("wa", "+6281234567890") == "+6281234567890"
    # Local "0…" Indonesian format
    assert _normalize("wa", "081234567890") == "+6281234567890"
    # Bare "62…" prefix
    assert _normalize("wa", "6281234567890") == "+6281234567890"
    # Garbage
    assert _normalize("wa", "abc") is None


# ----- DB-backed tests -------------------------------------------------------


@pytest.fixture
def db_factory():
    """Yield a callable that produces a fresh in-memory async session."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    async def _make():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        # Don't import models.* (some use Postgres-only JSONB which sqlite
        # can't render). Just register what the OTP-login stack actually
        # touches: profiles, otp_codes, store_memberships.
        from models.profile import BeliAmanProfile  # noqa: F401
        from models.otp_code import OtpCode  # noqa: F401
        from models.store_membership import StoreMembership  # noqa: F401
        from models.base import Base

        async with engine.begin() as conn:
            tables = [
                BeliAmanProfile.__table__,
                OtpCode.__table__,
                StoreMembership.__table__,
            ]
            await conn.run_sync(Base.metadata.create_all, tables=tables)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        return engine, Session

    return _make


def _run(coro):
    return asyncio.run(coro)


def test_issue_and_verify_happy_path(db_factory):
    async def go():
        from services import otp_store

        engine, Session = await db_factory()
        async with Session() as db:
            res = await otp_store.issue(db, "wa", "+6281234567890")
            await db.commit()
            assert not res.rate_limited
            assert len(res.code) == 6

            v = await otp_store.verify(db, "wa", "+6281234567890", res.code)
            await db.commit()
            assert v.ok is True

            # second verify with same code → not_found (single-use)
            v2 = await otp_store.verify(db, "wa", "+6281234567890", res.code)
            assert v2.ok is False

        await engine.dispose()

    _run(go())


def test_issue_resend_rate_limited(db_factory):
    async def go():
        from services import otp_store

        engine, Session = await db_factory()
        async with Session() as db:
            r1 = await otp_store.issue(db, "email", "u@x.io")
            await db.commit()
            r2 = await otp_store.issue(db, "email", "u@x.io")
            await db.commit()
            assert not r1.rate_limited
            assert r2.rate_limited
            # The original code still verifies.
            v = await otp_store.verify(db, "email", "u@x.io", r1.code)
            assert v.ok is True
        await engine.dispose()

    _run(go())


def test_verify_wrong_code_increments_attempts(db_factory):
    async def go():
        from services import otp_store
        from sqlalchemy import select
        from models.otp_code import OtpCode

        engine, Session = await db_factory()
        async with Session() as db:
            r = await otp_store.issue(db, "wa", "+6281234567890")
            await db.commit()
            v = await otp_store.verify(db, "wa", "+6281234567890", "000000")
            await db.commit()
            assert not v.ok
            row = (await db.execute(select(OtpCode))).scalar_one()
            assert row.attempts == 1
            # Correct code still works on next try.
            v2 = await otp_store.verify(db, "wa", "+6281234567890", r.code)
            assert v2.ok is True
        await engine.dispose()

    _run(go())


def test_verify_too_many_attempts(db_factory):
    async def go():
        from services import otp_store

        engine, Session = await db_factory()
        async with Session() as db:
            r = await otp_store.issue(db, "wa", "+6281234567890")
            await db.commit()
            for _ in range(otp_store.MAX_ATTEMPTS):
                await otp_store.verify(db, "wa", "+6281234567890", "000000")
                await db.commit()
            # 6th attempt should hit the cap, even with the correct code.
            v = await otp_store.verify(db, "wa", "+6281234567890", r.code)
            assert not v.ok
            assert v.reason == "too_many_attempts"
        await engine.dispose()

    _run(go())


def test_auto_merge_otp_then_otp_same_phone(db_factory):
    """Two OTP sign-ins with the same phone → single profile (last_seen_at updates)."""
    async def go():
        from deps import _get_or_create_profile_by_contact

        engine, Session = await db_factory()
        async with Session() as db:
            p1 = await _get_or_create_profile_by_contact(db, channel="wa", contact="+6281234567890")
            await db.commit()
            first_id = p1.id
            first_seen = p1.last_seen_at
            time.sleep(0.01)
            p2 = await _get_or_create_profile_by_contact(db, channel="wa", contact="+6281234567890")
            await db.commit()
            assert p2.id == first_id
            assert p2.last_seen_at >= first_seen
        await engine.dispose()

    _run(go())


def test_auto_merge_google_then_otp_email_match(db_factory):
    """A Google profile with email=X then OTP login with same email → same profile."""
    async def go():
        from deps import _get_or_create_profile, _get_or_create_profile_by_contact

        engine, Session = await db_factory()
        async with Session() as db:
            g = await _get_or_create_profile(
                db,
                google_sub="g-sub-1",
                email="alice@example.com",
                display_name="Alice",
                photo_url=None,
            )
            await db.commit()
            otp_p = await _get_or_create_profile_by_contact(
                db, channel="email", contact="alice@example.com"
            )
            await db.commit()
            assert otp_p.id == g.id  # same profile
            assert otp_p.google_sub == "g-sub-1"  # google_sub preserved
            assert otp_p.email == "alice@example.com"
        await engine.dispose()

    _run(go())


def test_auto_merge_otp_then_google_email_match(db_factory):
    """OTP email-login creates a profile; later Google sign-in with same email merges."""
    async def go():
        from deps import _get_or_create_profile, _get_or_create_profile_by_contact

        engine, Session = await db_factory()
        async with Session() as db:
            otp_p = await _get_or_create_profile_by_contact(
                db, channel="email", contact="bob@example.com"
            )
            await db.commit()
            g = await _get_or_create_profile(
                db,
                google_sub="g-sub-bob",
                email="bob@example.com",
                display_name="Bob",
                photo_url="http://x/bob.png",
            )
            await db.commit()
            assert g.id == otp_p.id
            assert g.google_sub == "g-sub-bob"
            assert g.display_name == "Bob"
            assert g.photo_url == "http://x/bob.png"
        await engine.dispose()

    _run(go())


def test_distinct_channels_no_collision(db_factory):
    """An OTP for ("email", "+62…") doesn't satisfy ("wa", "+62…")."""
    async def go():
        from services import otp_store

        engine, Session = await db_factory()
        async with Session() as db:
            r = await otp_store.issue(db, "wa", "+6281234567890")
            await db.commit()
            v = await otp_store.verify(db, "email", "+6281234567890", r.code)
            assert not v.ok
            assert v.reason == "not_found"
        await engine.dispose()

    _run(go())
