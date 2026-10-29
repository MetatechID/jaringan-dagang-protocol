"""JD-031: browser returns are per-partner, revalidated and script-free."""
import pytest
from fastapi import HTTPException
from config import settings
from services.partner_return_url import validate_return_url
from services.partner_orders import create_partner_order
from test_partner_integration import (
    FakeSession, _build_pay_app, _build_partner_app,
    stub_partner_order, stub_partner_brand, _VALID_BODY,
)


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "partner_return_origins", {
        "brand-id": ["https://merchant.example"],
        "other-brand": ["https://other.example"],
    })


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "data:text/html,test", "//merchant.example/ok",
    "http://merchant.example", "https://merchant.example.evil.test",
    "https://merchant.example@evil.test", "https://user@merchant.example",
    "https://merchant.example:444/", "https://merchant.example./",
    "https://merchant.example\\@evil.test", "https://merchant.example/%0aevil",
    "https://merchant.example/%250d", "https://merchant.example/</script><script>alert(1)</script>",
    "https://merchant.example/%3c/script%3e", " https://merchant.example/",
    "https://merchant.example/\tbad", "https://other.example/", "",
])
def test_reject_hostile_or_other_partner(url):
    with pytest.raises(ValueError):
        validate_return_url(url, "brand-id")


def test_exact_origin_query_and_none():
    url = "https://merchant.example:443/return?order=123&paid=true#receipt"
    assert validate_return_url(url, "brand-id") == url
    assert validate_return_url(None, "brand-id") is None
    with pytest.raises(ValueError):
        validate_return_url(url, "unknown")


@pytest.mark.parametrize("mode,allowed", [("development", True), ("test", True),
                                         ("staging", False), ("production", False)])
def test_local_requires_explicit_policy_and_local_mode(monkeypatch, mode, allowed):
    monkeypatch.setattr(settings, "environment", mode)
    monkeypatch.setattr(settings, "partner_return_origins", {"brand-id": ["http://localhost:3000"]})
    if allowed:
        assert validate_return_url("http://localhost:3000/ok", "brand-id")
    else:
        with pytest.raises(ValueError):
            validate_return_url("http://localhost:3000/ok", "brand-id")


@pytest.mark.asyncio
async def test_invalid_input_before_database_or_provider_work():
    db = FakeSession([])
    with pytest.raises(HTTPException) as error:
        await create_partner_order(db, brand=stub_partner_brand(), external_order_id="id",
            amount_idr=100, description="test", buyer={}, items=[],
            success_url="https://evil.example/")
    assert error.value.status_code == 422
    assert not db.added and not db.committed


@pytest.mark.parametrize("status", ["pending", "expired", "paid"])
@pytest.mark.parametrize("url", ["javascript:alert(1)",
    "https://merchant.example/</script><script>alert(1)</script>", "https://evil.example/"])
def test_legacy_unsafe_url_not_exposed_or_executed(status, url):
    order = stub_partner_order(status=status, success_url=url)
    client = _build_pay_app(FakeSession([order, order, order]))
    response = client.get("/pay/po-123/done", follow_redirects=False)
    assert response.status_code == 200
    assert "location" not in response.headers
    assert url not in response.text
    assert "sessionStorage" not in response.text
    assert client.get("/pay/po-123/status").json()["success_url"] is None
    public = _build_partner_app(FakeSession([order]), None)
    assert public.get("/api/v1/partner/public/orders/po-123").json()["success_url"] is None


@pytest.mark.parametrize("status,code", [("pending", 200), ("expired", 200), ("paid", 303)])
def test_server_redirect_only_after_paid(status, code):
    url = "https://merchant.example/return?paid=123"
    order = stub_partner_order(status=status, success_url=url)
    client = _build_pay_app(FakeSession([order, order]))
    response = client.get("/pay/po-123/done", follow_redirects=False)
    assert response.status_code == code
    if code == 303:
        assert response.headers["location"] == url
        assert "<script>" not in response.text
    else:
        assert "location" not in response.headers


def test_registration_denies_other_origin_before_invoice():
    db = FakeSession([])
    response = _build_partner_app(db, stub_partner_brand()).post(
        "/api/v1/partner/orders", json={**_VALID_BODY, "success_url": "https://other.example/"})
    assert response.status_code == 422
    assert not db.added and not db.committed
