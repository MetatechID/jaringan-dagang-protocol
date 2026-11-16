"""D+3 release: seller-reported fulfillment starts the clock, the sweep releases,
and Midtrans brands release for manual payout instead of refusing.

In-memory sqlite with the whole schema; the seller bridge is faked.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)
os.environ.setdefault("FIREBASE_SERVICE_ACCOUNT_JSON", '{"type":"service_account","project_id":"test"}')

ADMIN = {"X-Admin-Token": "test-admin"}


@pytest.fixture
def env(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import models  # noqa: F401  (registers every table)
    from config import settings
    from database import get_db
    from models.base import Base
    from models.brand import Brand
    from models.profile import BeliAmanProfile
    from routers.orders import router as orders_router
    from services import seller_bridge

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def _bootstrap():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as db:
            db.add(BeliAmanProfile(id="buyer-1", email="buyer@example.com"))
            db.add(Brand(id="brand-1", slug="safiyafood", name="Safiya", bpp_id="safiya.bpp",
                         payment_provider="midtrans"))
            await db.commit()

    asyncio.run(_bootstrap())
    monkeypatch.setattr(settings, "admin_token", "test-admin")
    patched: list[tuple[str, str]] = []

    async def fake_patch(*, order_id, escrow_status):
        patched.append((order_id, escrow_status))
        return True

    monkeypatch.setattr(seller_bridge, "patch_escrow_status", fake_patch)

    async def override_get_db():
        async with Session() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    app = FastAPI()
    app.include_router(orders_router)
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client, Session, patched


def add_order(Session, oid: str, state, *, auto_release_at=None):
    from models.order import Order

    async def _add():
        async with Session() as db:
            db.add(Order(id=oid, profile_id="buyer-1", brand_id="brand-1", state=state,
                         items=[{"sku": "SAF-1", "qty": 1}], subtotal_idr=79000, shipping_idr=3600,
                         total_idr=82600, bap_id="beli-aman", auto_release_at=auto_release_at,
                         delivered_at=auto_release_at and auto_release_at - timedelta(days=3)))
            await db.commit()

    asyncio.run(_add())


def load(Session, oid):
    from sqlalchemy import select

    from models.escrow_ledger import EscrowLedger
    from models.order import Order

    async def _load():
        async with Session() as db:
            order = (await db.execute(select(Order).where(Order.id == oid))).scalar_one()
            ledger = (await db.execute(select(EscrowLedger).where(EscrowLedger.order_id == oid))).scalars().all()
            return order, ledger

    return asyncio.run(_load())


def test_seller_shipped_then_delivered_starts_the_clock_once(env):
    from models.order import OrderState

    client, Session, _ = env
    add_order(Session, "o-1", OrderState.ESCROW_HELD)
    r = client.post("/api/v1/orders/o-1/seller-fulfillment", json={"event": "shipped"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "FULFILLING" and r.json()["auto_release_at"] is None

    r = client.post("/api/v1/orders/o-1/seller-fulfillment", json={"event": "delivered"}, headers=ADMIN)
    body = r.json()
    assert body["state"] == "RECEIVED" and body["auto_release_at"]
    again = client.post("/api/v1/orders/o-1/seller-fulfillment", json={"event": "delivered"}, headers=ADMIN).json()
    # The clock never moves (sqlite drops the offset on reload; Postgres keeps it).
    assert again["auto_release_at"][:19] == body["auto_release_at"][:19]


def test_delivered_straight_from_held(env):
    from models.order import OrderState

    client, Session, _ = env
    add_order(Session, "o-2", OrderState.ESCROW_HELD)
    body = client.post("/api/v1/orders/o-2/seller-fulfillment", json={"event": "delivered"}, headers=ADMIN).json()
    assert body["state"] == "RECEIVED" and body["auto_release_at"]


def test_closed_or_disputed_orders_are_left_alone(env):
    from models.order import OrderState

    client, Session, _ = env
    add_order(Session, "o-3", OrderState.DISPUTED)
    body = client.post("/api/v1/orders/o-3/seller-fulfillment", json={"event": "delivered"}, headers=ADMIN).json()
    assert body["state"] == "DISPUTED" and body["auto_release_at"] is None


def test_needs_the_admin_token(env):
    client, _, _ = env
    r = client.post("/api/v1/orders/o-x/seller-fulfillment", json={"event": "shipped"})
    assert r.status_code == 403


def test_sweep_releases_due_midtrans_orders_for_manual_payout(env):
    from models.escrow_ledger import EscrowEntryStatus
    from models.order import OrderState

    _, Session, patched = env
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    add_order(Session, "due", OrderState.RECEIVED, auto_release_at=past)
    add_order(Session, "later", OrderState.RECEIVED, auto_release_at=past + timedelta(days=2))

    import importlib.util

    spec = importlib.util.spec_from_file_location("release_due", os.path.join(_BAP_DIR, "scripts", "release-due-orders.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert asyncio.run(mod.run(dry_run=True, session_factory=Session)) == {"due": 1}
    assert asyncio.run(mod.run(session_factory=Session)) == {"released": 1}
    order, ledger = load(Session, "due")
    assert order.state == OrderState.ESCROW_RELEASED and order.released_at is not None
    assert len(ledger) == 1 and ledger[0].status == EscrowEntryStatus.PENDING
    assert ledger[0].external_ref is None and "manual payout" in ledger[0].description
    assert patched == [("due", "released")]
    assert load(Session, "later")[0].state == OrderState.RECEIVED
    assert asyncio.run(mod.run(session_factory=Session)) == {}  # nothing left
