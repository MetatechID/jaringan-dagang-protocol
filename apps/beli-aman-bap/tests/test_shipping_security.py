"""Authoritative checkout shipping with synthetic catalog and carrier controls."""
import copy
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from config import settings
from database import get_db
from deps import get_current_profile
from models.brand import Brand
from models.order import Order, OrderState
from routers import orders
from services import carriers, shipping, jubelio


PRODUCTS = [{"sku": "sku", "name": "Product", "price_idr": 50000, "weight_grams": 600,
             "variants": [{"sku": "variant", "label": "Large", "price_idr": 70000, "weight_grams": 900}]}]
RATE = {"courier_code": "jne", "courier_service_code": "REG", "courier_service_name": "Regular", "price": 12000, "duration": "2 days"}


class Result:
    def __init__(self, value):
        self.value = value
    def scalar_one_or_none(self):
        return self.value


class DB:
    def __init__(self, brand):
        self.brand = brand
        self.added = []
        self.flush = AsyncMock(side_effect=self.defaults)
    async def execute(self, stmt):
        return Result(self.brand)
    async def get(self, model, key):
        return self.brand if model is Brand and key == self.brand.id else None
    def add(self, obj):
        self.added.append(obj)
    async def defaults(self):
        for obj in self.added:
            if isinstance(obj, Order):
                obj.id = "order"
                obj.state = OrderState.PRE_AUTH
                obj.created_at = obj.updated_at = datetime.now(timezone.utc)


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    # Supports running these regressions against the pre-fix Settings model.
    object.__setattr__(settings, "shipping_free_brand_slugs", "")
    monkeypatch.setattr(settings, "default_carrier", "biteship")
    brand = NS(id="brand", slug="store", bpp_id="store.example.test", fee_pct_bp=0,
               jubelio_enabled=False, biteship_origin_address={"postal_code": "12345"})
    db = DB(brand)
    profile = NS(id="buyer")
    monkeypatch.setattr(orders.catalog_service, "list_products", AsyncMock(return_value=copy.deepcopy(PRODUCTS)))
    rates = AsyncMock(return_value=[copy.deepcopy(RATE)])
    monkeypatch.setattr(carriers, "get_rates", rates)
    app = FastAPI()
    app.include_router(orders.router)
    async def session():
        yield db
    async def identity():
        return profile
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_profile] = identity
    return NS(app=app, db=db, brand=brand, profile=profile, rates=rates)


def payload():
    return {"brand_slug": "store", "destination_postal_code": "54321", "items": [{"sku": "sku", "qty": 2}],
            "shipping": {"courier_code": "jne", "courier_service_code": "REG", "price_idr": 12000}}


async def request(setup, body, method="POST", path="/api/v1/orders"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup.app), base_url="http://test") as client:
        return await client.request(method, path, json=body)


async def create(setup):
    response = await request(setup, payload())
    assert response.status_code == 200, response.text
    return next(obj for obj in setup.db.added if isinstance(obj, Order))


async def test_create_uses_catalog_cart_and_server_rate(setup):
    order = await create(setup)
    assert order.shipping_idr == 12000
    assert order.total_idr == 112000
    args = setup.rates.call_args.kwargs
    assert args["brand"] is setup.brand
    assert args["destination_postal_code"] == "54321"
    assert args["items"] == [{"name": "Product", "quantity": 2, "value": 50000, "weight": 600}]
    assert order.shipping_address["shipping_binding"]["brand_id"] == "brand"


@pytest.mark.parametrize("change", ["underquote", "overquote", "service", "courier", "omitted", "destination", "empty-cart", "sku"])
async def test_invalid_create_has_no_mutation(setup, change):
    body = payload()
    if change in {"underquote", "overquote"}:
        body["shipping"]["price_idr"] = 1 if change == "underquote" else 99999
    elif change in {"service", "courier"}:
        body["shipping"]["courier_service_code" if change == "service" else "courier_code"] = "other"
    elif change == "omitted":
        del body["shipping"]
    elif change == "destination":
        del body["destination_postal_code"]
    elif change == "empty-cart":
        body["items"] = []
    else:
        body["items"][0]["sku"] = "another-store-sku"
    response = await request(setup, body)
    assert response.status_code in {400, 409, 422}, response.text
    assert not setup.db.added
    setup.db.flush.assert_not_awaited()


async def test_changed_cart_reprices_not_client_quote(setup):
    setup.rates.side_effect = lambda **kw: [{**RATE, "price": 12000 * kw["items"][0]["quantity"]}]
    response = await request(setup, payload())
    assert response.status_code == 409
    assert not setup.db.added


async def test_changed_destination_reprices_not_client_quote(setup):
    setup.rates.side_effect = lambda **kw: [{**RATE, "price": 18000 if kw["destination_postal_code"] == "99999" else 12000}]
    body = payload()
    body["destination_postal_code"] = "99999"
    assert (await request(setup, body)).status_code == 409
    assert not setup.db.added


async def test_variant_weight_and_price_are_server_resolved(setup):
    body = payload()
    body["items"] = [{"sku": "variant", "qty": 3, "weight": 1, "value": 0}]
    assert (await request(setup, body)).status_code == 200
    assert setup.rates.call_args.kwargs["items"] == [{"name": "Product - Large", "quantity": 3, "value": 70000, "weight": 900}]


@pytest.mark.parametrize("failure", [[], carriers.ShippingError("outage"), shipping.ShippingError("outage"), jubelio.ShippingError("outage")])
async def test_unavailable_rate_cannot_create_order(setup, failure):
    if isinstance(failure, Exception):
        setup.rates.side_effect = failure
    else:
        setup.rates.return_value = failure
    assert (await request(setup, payload())).status_code == 503
    assert not setup.db.added


async def test_explicit_server_free_shipping_only(setup, monkeypatch):
    monkeypatch.setattr(settings, "shipping_free_brand_slugs", "store", raising=False)
    body = payload()
    del body["shipping"]
    response = await request(setup, body)
    assert response.status_code == 200
    assert response.json()["shipping_idr"] == 0
    setup.rates.assert_not_awaited()


@pytest.mark.parametrize("change", [None, "destination", "cart", "tenant", "owner", "catalog", "rate", "service", "carrier", "origin", "missing-binding", "inline-forgery"])
async def test_address_transition_revalidates_all_bindings(setup, monkeypatch, change):
    order = await create(setup)
    before = copy.deepcopy(order.shipping_address)
    monkeypatch.setattr(orders, "lock_order_for_update", AsyncMock(return_value=order))
    advance = AsyncMock()
    monkeypatch.setattr(orders, "transition", advance)
    address = {"postal_code": "54321", "line1": "Test"}
    if change == "destination":
        address["postal_code"] = "99999"
    elif change == "cart":
        order.items = [{**order.items[0], "qty": 999}]
    elif change == "tenant":
        setup.brand.id = "another-brand"
    elif change == "owner":
        setup.brand.bpp_id = "another-owner"
    elif change == "catalog":
        orders.catalog_service.list_products.return_value = [{**PRODUCTS[0], "weight_grams": 999}]
    elif change == "rate":
        setup.rates.return_value = [{**RATE, "price": 18000}]
    elif change == "service":
        setup.rates.return_value = [{**RATE, "courier_service_code": "YES"}]
    elif change == "carrier":
        setup.brand.jubelio_enabled = True
    elif change == "origin":
        setup.brand.biteship_origin_address = {"postal_code": "99999"}
    elif change == "missing-binding":
        order.shipping_address = {"courier": before["courier"]}
    elif change == "inline-forgery":
        address.update(courier={"price_idr": 0}, shipping_binding={"brand_id": "other"})
    response = await request(setup, {"address_inline": address}, "PATCH", "/api/v1/orders/order/auth")
    if change in {None, "inline-forgery"}:
        assert response.status_code == 200, response.text
        assert order.shipping_address["courier"]["price_idr"] == 12000
        assert order.shipping_address["shipping_binding"] == before["shipping_binding"]
        advance.assert_awaited_once()
    else:
        assert response.status_code == 409, response.text
        advance.assert_not_awaited()


@pytest.mark.parametrize("price", [0, -1, "NaN", "Infinity", "1.5", None])
async def test_invalid_provider_prices_never_authorize_shipping(setup, price):
    setup.rates.return_value = [{**RATE, "price": price}]
    assert (await request(setup, payload())).status_code == 503
    assert not setup.db.added


async def test_catalog_is_scoped_to_persisted_tenant(setup):
    async def tenant_catalog(slug):
        return PRODUCTS if slug == "another-store" else []
    orders.catalog_service.list_products.side_effect = tenant_catalog
    assert (await request(setup, payload())).status_code == 400
    setup.rates.assert_not_awaited()
    assert not setup.db.added


async def test_free_policy_revocation_prevents_auth(setup, monkeypatch):
    monkeypatch.setattr(settings, "shipping_free_brand_slugs", "store")
    body = payload()
    del body["shipping"]
    assert (await request(setup, body)).status_code == 200
    order = next(obj for obj in setup.db.added if isinstance(obj, Order))
    monkeypatch.setattr(settings, "shipping_free_brand_slugs", "")
    monkeypatch.setattr(orders, "lock_order_for_update", AsyncMock(return_value=order))
    advance = AsyncMock()
    monkeypatch.setattr(orders, "transition", advance)
    response = await request(setup, {"address_inline": {"postal_code": "54321"}}, "PATCH", "/api/v1/orders/order/auth")
    assert response.status_code in {409, 422}
    advance.assert_not_awaited()


@pytest.mark.parametrize("destination", ["", "1234", "123456", "abcde", "１２３４５"])
async def test_invalid_destination_is_rejected_before_provider(setup, destination):
    body = payload()
    body["destination_postal_code"] = destination
    assert (await request(setup, body)).status_code == 422
    setup.rates.assert_not_awaited()


async def test_saved_address_uses_same_quote_binding(setup, monkeypatch):
    order = await create(setup)
    monkeypatch.setattr(orders, "lock_order_for_update", AsyncMock(return_value=order))
    address = NS(recipient_name="Test", phone_e164="+620000", line1="Test", line2=None,
                 kelurahan=None, kecamatan=None, kota=None, provinsi=None, postal_code="54321")
    setup.db.execute = AsyncMock(return_value=Result(address))
    monkeypatch.setattr(orders, "transition", AsyncMock())
    response = await request(setup, {"address_id": "owned-address"}, "PATCH", "/api/v1/orders/order/auth")
    assert response.status_code == 200, response.text
    assert order.shipping_address["courier"]["price_idr"] == 12000


@pytest.mark.parametrize("provider", ["biteship", "jubelio"])
@pytest.mark.parametrize("credentials", ["", " "])
async def test_nonlocal_missing_credentials_fail_before_http(monkeypatch, provider, credentials):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "biteship_api_key", credentials)
    monkeypatch.setattr(settings, "jubelio_client_id", "synthetic")
    monkeypatch.setattr(settings, "jubelio_client_secret", credentials)
    module = shipping if provider == "biteship" else jubelio
    kwargs = {"destination_postal_code": "54321"} if provider == "biteship" else {"origin_zipcode": "12345", "destination_zipcode": "54321"}
    with pytest.raises(module.ShippingError):
        await module.get_rates(items=[{"weight": 500, "quantity": 1}], **kwargs)


@pytest.mark.parametrize("missing", [False, True])
async def test_biteship_uses_only_persisted_tenant_origin(monkeypatch, missing):
    monkeypatch.setattr(settings, "default_carrier", "biteship")
    provider = AsyncMock(return_value=[copy.deepcopy(RATE)])
    monkeypatch.setattr(shipping, "get_rates", provider)
    brand = NS(jubelio_enabled=False, biteship_origin_address={} if missing else {"postal_code": "12345"})
    if missing:
        with pytest.raises(carriers.ShippingError):
            await carriers.get_rates(brand=brand, destination_postal_code="54321", items=[])
        provider.assert_not_awaited()
    else:
        await carriers.get_rates(brand=brand, destination_postal_code="54321", items=[])
        assert provider.call_args.kwargs["origin_postal_code"] == "12345"


async def test_jubelio_fallback_origin_change_invalidates_binding(setup, monkeypatch):
    setup.brand.jubelio_enabled = True
    setup.brand.jubelio_origin_address = {"area_id": 1}
    order = await create(setup)
    setup.brand.biteship_origin_address = {"postal_code": "99999"}
    monkeypatch.setattr(orders, "lock_order_for_update", AsyncMock(return_value=order))
    advance = AsyncMock()
    monkeypatch.setattr(orders, "transition", advance)
    response = await request(setup, {"address_inline": {"postal_code": "54321"}}, "PATCH", "/api/v1/orders/order/auth")
    assert response.status_code == 409
    advance.assert_not_awaited()


@pytest.mark.parametrize("field,value", [("price_idr", "1.5"), ("weight_grams", "NaN"), ("weight_grams", -1)])
async def test_invalid_catalog_numeric_data_fails_closed(setup, field, value):
    orders.catalog_service.list_products.return_value = [{**PRODUCTS[0], field: value}]
    assert (await request(setup, payload())).status_code == 503
    setup.rates.assert_not_awaited()
    assert not setup.db.added


async def test_auth_provider_failure_does_not_mutate_address(setup, monkeypatch):
    order = await create(setup)
    before = copy.deepcopy(order.shipping_address)
    monkeypatch.setattr(orders, "lock_order_for_update", AsyncMock(return_value=order))
    advance = AsyncMock()
    monkeypatch.setattr(orders, "transition", advance)
    setup.rates.side_effect = carriers.ShippingError("offline")
    response = await request(setup, {"address_inline": {"postal_code": "54321"}}, "PATCH", "/api/v1/orders/order/auth")
    assert response.status_code == 503
    assert order.shipping_address == before
    advance.assert_not_awaited()


@pytest.mark.parametrize("code", [None, "", "None"])
async def test_missing_provider_service_cannot_be_selected(setup, code):
    setup.rates.return_value = [{**RATE, "courier_service_code": code}]
    body = payload()
    body["shipping"]["courier_service_code"] = code
    assert (await request(setup, body)).status_code == 409
    assert not setup.db.added


async def test_already_authed_order_cannot_replace_address(setup, monkeypatch):
    order = await create(setup)
    order.state = OrderState.AUTHED
    before = copy.deepcopy(order.shipping_address)
    monkeypatch.setattr(orders, "lock_order_for_update", AsyncMock(return_value=order))
    response = await request(setup, {"address_inline": {"postal_code": "54321"}}, "PATCH", "/api/v1/orders/order/auth")
    assert response.status_code == 409
    assert order.shipping_address == before


class Client:
    response = None
    failure = None
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def post(self, *args, **kwargs):
        if self.failure:
            raise self.failure
        return self.response


@pytest.mark.parametrize("provider", ["biteship", "jubelio"])
@pytest.mark.parametrize("mode", ["production", "staging", "", "unknown", "test", "development"])
@pytest.mark.parametrize("outcome", ["timeout", "500", "empty", "malformed", "valid"])
async def test_provider_failure_mock_policy_and_positive_control(monkeypatch, provider, mode, outcome):
    monkeypatch.setattr(settings, "environment", mode)
    monkeypatch.setattr(settings, "biteship_api_key", "synthetic")
    monkeypatch.setattr(settings, "jubelio_client_id", "synthetic")
    monkeypatch.setattr(settings, "jubelio_client_secret", "synthetic")
    monkeypatch.setattr(jubelio, "_auth_headers", AsyncMock(return_value={"Authorization": "synthetic"}))
    client = Client()
    client.failure = httpx.ConnectTimeout("offline") if outcome == "timeout" else None
    data = {"pricing": [RATE]} if provider == "biteship" else [{"courier_id": 1, "courier_service_id": 2, "rates": 12000}]
    if outcome == "empty":
        data = {"pricing": []} if provider == "biteship" else []
    if outcome == "malformed":
        data = {"pricing": [None]} if provider == "biteship" else [None]
    client.response = httpx.Response(500 if outcome == "500" else 200, json=data, request=httpx.Request("POST", "https://carrier.example.test"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    module = shipping if provider == "biteship" else jubelio
    kwargs = {"destination_postal_code": "54321"} if provider == "biteship" else {"origin_zipcode": "12345", "destination_zipcode": "54321"}
    if outcome == "malformed" or (outcome != "valid" and mode not in {"development", "test"}):
        with pytest.raises(module.ShippingError):
            await module.get_rates(items=[{"weight": 500, "quantity": 1}], **kwargs)
    else:
        rates = await module.get_rates(items=[{"weight": 500, "quantity": 1}], **kwargs)
        assert rates
        if outcome == "valid":
            assert len(rates) == 1 and rates[0]["price"] == 12000
