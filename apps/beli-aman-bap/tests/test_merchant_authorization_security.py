"""Real route/SQL ACL regressions; Firebase and carriers stay offline."""
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from unittest.mock import AsyncMock

import deps
from database import get_db
from models.base import Base
from models.brand import Brand
from models.order import Order, OrderState
from models.order_event import OrderEvent
from models.escrow_ledger import EscrowLedger
from models.partner_order import PartnerOrder
from models.profile import BeliAmanProfile
from models.store_membership import StoreMembership, StoreRole
from routers import orders, partner
from services import carriers


@compiles(JSONB, "sqlite")
def sqlite_jsonb(_type, _compiler, **_kw):
    return "JSON"


@pytest_asyncio.fixture
async def api(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [m.__table__ for m in (BeliAmanProfile, Brand, Order, OrderEvent,
                                  EscrowLedger, PartnerOrder, StoreMembership)]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        db.add_all([BeliAmanProfile(id=p, is_super_admin=p == "admin")
                    for p in ("staff", "owner", "admin", "outsider", "pending")])
        for slug in ("a", "b"):
            db.add(Brand(id=slug, slug=slug, name=slug, bpp_id=slug,
                         jubelio_origin_address={"zipcode": "12345", "address": "Origin"}))
            db.add(Order(id=slug, brand_id=slug, profile_id="outsider",
                         state=OrderState.ESCROW_HELD, items=[], subtotal_idr=100,
                         total_idr=100, shipping_address={"name": "Buyer"}))
            db.add(PartnerOrder(id=slug, brand_id=slug, external_order_id=slug,
                                amount_idr=100, description=slug, status="paid"))
        for p, role in (("staff", StoreRole.STAFF), ("owner", StoreRole.OWNER)):
            # Duplicate ACL rows must not duplicate collection entries.
            for n in range(2):
                db.add(StoreMembership(profile_id=p, invited_email=p + "@example.com",
                                       store_id=f"{p}-{n}", store_slug="a", role=role))
        db.add(StoreMembership(invited_email="pending@example.com", store_id="invite",
                               store_slug="a"))
        await db.commit()

        async def session():
            try:
                yield db
            finally:
                await db.rollback()

        def verify(token):
            if token not in {"staff", "owner", "admin", "outsider", "pending"}:
                raise ValueError("Token verification failed")
            return {"sub": token, "firebase": {"sign_in_provider": "custom"}}

        monkeypatch.setattr(deps, "verify_id_token", verify)
        book = AsyncMock(return_value={"carrier": "jubelio", "awb": "awb", "external_id": "shipment"})
        monkeypatch.setattr(carriers, "book", book)
        book.transition = AsyncMock(wraps=orders.transition)
        monkeypatch.setattr(orders, "transition", book.transition)
        app = FastAPI()
        app.include_router(orders.router)
        app.include_router(partner.router)
        app.dependency_overrides[get_db] = session
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, db, book
    await engine.dispose()


def bearer(who):
    return {"Authorization": "Bearer " + who}


ROUTES = [("GET", "/api/v1/orders/a/admin", None),
          ("GET", "/api/v1/orders/a/label-data", None),
          ("POST", "/api/v1/orders/a/ship", {"courier_code": "11", "courier_service_code": "REG"}),
          ("GET", "/api/v1/partner/admin/orders", None)]


@pytest.mark.parametrize("method,path,body", ROUTES)
@pytest.mark.parametrize("headers", [{}, {"X-Admin-Token": "test-admin-token"},
                                     {"Authorization": "Basic forged"}, bearer("forged")])
async def test_identity_required(api, method, path, body, headers):
    client, db, book = api
    response = await client.request(method, path, json=body, headers=headers)
    assert response.status_code == 401
    book.assert_not_awaited()
    assert (await db.get(Order, "a")).state == OrderState.ESCROW_HELD


@pytest.mark.parametrize("method,path,body", ROUTES[:3])
@pytest.mark.parametrize("who", ["outsider", "pending", "staff", "owner"])
async def test_cross_tenant_denied_before_state_checks_or_side_effects(api, method, path, body, who):
    client, db, book = api
    # Staff/owner belong only to a. Supplied slug, body and headers cannot override persisted b.
    path = path.replace("/a/", "/b/") + "?brand_slug=a"
    response = await client.request(method, path, json=body, headers={**bearer(who), "X-Store-Slug": "a"})
    assert response.status_code == 403
    book.assert_not_awaited()
    book.transition.assert_not_awaited()
    order = await db.get(Order, "b")
    assert order.state == OrderState.ESCROW_HELD
    assert order.fulfillment_awb is None
    assert not (await db.execute(select(OrderEvent))).scalars().all()


@pytest.mark.parametrize("who", ["staff", "owner", "admin"])
async def test_authorized_read_label_and_booking(api, who):
    client, db, book = api
    slug = "b" if who == "admin" else "a"
    path = f"/api/v1/orders/{slug}"
    response = await client.get(path + "/admin?brand_slug=unrelated", headers=bearer(who))
    assert response.status_code == 200
    assert response.json()["brand_id"] == slug
    response = await client.post(path + "/ship", headers=bearer(who), json={"courier_code": "11", "courier_service_code": "REG"})
    assert response.status_code == 200
    assert response.json()["state"] == "FULFILLING"
    assert response.json()["fulfillment_awb"] == "awb"
    book.assert_awaited_once()
    assert book.call_args.kwargs["order"].brand_id == slug
    # Dependency deliberately rolls back: route/helper must not commit caller's transaction.
    order = await db.get(Order, slug)
    assert order.state == OrderState.ESCROW_HELD
    order.fulfillment_awb = "awb"
    order.carrier = "jubelio"
    await db.commit()
    response = await client.get(path + "/label-data", headers=bearer(who))
    assert response.status_code == 200
    assert response.json()["sender"]["store_name"] == slug


@pytest.mark.parametrize("who,expected", [("staff", ["a"]), ("owner", ["a"]),
                                         ("admin", ["a", "b"]), ("outsider", []), ("pending", [])])
async def test_collection_persisted_scope_without_slug(api, who, expected):
    client, _, _ = api
    response = await client.get("/api/v1/partner/admin/orders", headers=bearer(who))
    assert response.status_code == 200
    assert sorted(row["id"] for row in response.json()) == expected


@pytest.mark.parametrize("query,expected", [("brand_slug=b", []), ("brand_slug=a", ["a"]),
    ("status=pending", []), ("limit=1&offset=1", []), ("brand_slug=missing", [])])
async def test_collection_filters_never_grant_access(api, query, expected):
    client, _, _ = api
    response = await client.get("/api/v1/partner/admin/orders?" + query, headers=bearer("staff"))
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == expected


@pytest.mark.parametrize("suffix,method,body", [("admin", "GET", None), ("label-data", "GET", None),
                                               ("ship", "POST", {"courier_code": "11", "courier_service_code": "REG"})])
async def test_missing_order(api, suffix, method, body):
    client, _, book = api
    response = await client.request(method, "/api/v1/orders/missing/" + suffix,
                                    json=body, headers=bearer("staff"))
    assert response.status_code == 404
    book.assert_not_awaited()


@pytest.mark.parametrize("suffix,body", [("ship/cancel", {"reason": "test"}), ("tracking/refresh", None)])
async def test_internal_routes_do_not_accept_merchant_bearer(api, suffix, body):
    client, _, _ = api
    response = await client.post("/api/v1/orders/a/" + suffix, json=body, headers=bearer("admin"))
    assert response.status_code == 403
