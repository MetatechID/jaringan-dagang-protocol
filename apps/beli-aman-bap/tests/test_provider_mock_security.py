"""Offline regression coverage for explicit local-only payment mocks."""
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from config import settings
from services import xendit_invoices, oy_invoices, sento_invoices
from routers import checkout, orders, partner, pay_page, profiles
from models.bot_rest import CartStatus
from models.order import OrderState
from models.payment_method import PaymentMethodType

MODULES = {"xendit": xendit_invoices, "oy": oy_invoices, "sento": sento_invoices}


class Result:
    def __init__(self, value):
        self.value = value
    def scalar_one_or_none(self):
        return self.value
    def scalars(self):
        return self
    def first(self):
        return self.value
    def all(self):
        return self.value


def session(*values):
    return NS(execute=AsyncMock(side_effect=[Result(v) for v in values]),
              add=Mock(), flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())


def brand(provider):
    return NS(id="brand", slug="brand", name="Store", bpp_id="bpp", payment_provider=provider,
              xendit_sub_account_id="sub-test", oy_api_key="key-test", oy_username="user-test",
              sento_api_key="key-test", sento_username="user-test")


def target():
    return NS(id="order", brand_id="brand", profile_id="buyer", bpp_id="bpp", order_id=None,
              quote_json={"total_idr": 1000}, billing_json={}, items_json=[],
              invoice_id=None, invoice_provider=None, qr_image_url=None, qris_image_url=None,
              payment_method_snapshot=None, total_idr=1000, amount_idr=1000,
              shipping_address={}, items=[], description="Test", buyer={}, success_url=None,
              state=OrderState.CART_REVIEWED, status=CartStatus.QUOTED, payment_state=None,
              quote_token=None, transaction_id="transaction", shipping_json={})


@pytest.fixture(autouse=True)
def synthetic_settings(monkeypatch):
    for name in ("xendit_secret_key", "oy_api_key", "oy_default_username", "sento_api_key", "sento_default_username"):
        monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(settings, "mock_checkout_public_base", "http://localhost:9999")


def configure(monkeypatch, provider, b, state):
    if provider == "xendit":
        monkeypatch.setattr(settings, "xendit_secret_key", "key-test")
        if state == "missing":
            monkeypatch.setattr(settings, "xendit_secret_key", "")
        elif state == "blank":
            monkeypatch.setattr(settings, "xendit_secret_key", "  \t")
        elif state == "partial":
            b.xendit_sub_account_id = " \t"
    else:
        if state == "missing":
            setattr(b, f"{provider}_api_key", None)
            setattr(b, f"{provider}_username", None)
        elif state == "blank":
            setattr(b, f"{provider}_api_key", " \t")
        elif state == "partial":
            setattr(b, f"{provider}_username", " \t")
        elif state == "global":
            setattr(b, f"{provider}_api_key", None)
            setattr(b, f"{provider}_username", None)
            monkeypatch.setattr(settings, f"{provider}_api_key", "global-key")
            monkeypatch.setattr(settings, f"{provider}_default_username", "global-user")
    if state == "wrong-provider":
        b.payment_provider = "other"
    return None if state == "no-brand" else b


@pytest.mark.parametrize("provider", MODULES)
@pytest.mark.parametrize("flow", ["cart", "order"])
@pytest.mark.parametrize("mode", ["production", "staging", "development", "test", "", "unknown", " Production "])
@pytest.mark.parametrize("credentials", ["missing", "blank", "partial", "no-brand", "wrong-provider", "valid", "global"])
async def test_invoice_matrix(monkeypatch, provider, flow, mode, credentials):
    monkeypatch.setattr(settings, "environment", mode)
    b = configure(monkeypatch, provider, brand(provider), credentials)
    obj = target()
    before = deepcopy(vars(obj))
    mod = MODULES[provider]
    # Stub only HTTP transport: exercise the client's real auth resolution.
    client_mod = getattr(mod, f"{provider}_client")
    import httpx
    response = NS(status_code=200, json=lambda: {"id": "real-test", "invoice_url": "https://provider.example/pay", "payment_link_id": "real-test", "url": "https://provider.example/pay"})
    request = AsyncMock(return_value=response)
    http = NS(__aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False))
    http.__aenter__.return_value = NS(request=request)
    from unittest.mock import MagicMock
    context = MagicMock()
    context.__aenter__ = http.__aenter__
    context.__aexit__ = http.__aexit__
    monkeypatch.setattr(httpx, "AsyncClient", Mock(return_value=context))
    valid = credentials in {"valid", "global"}
    local = mode.strip().lower() in {"development", "test"}
    call = getattr(mod, f"create_invoice_for_{flow}")
    if not valid and not local:
        with pytest.raises(HTTPException) as exc:
            await call(session(b), obj)
        assert exc.value.status_code == 503
        assert vars(obj) == before
        request.assert_not_called()
    else:
        result = await call(session(b), obj)
        assert bool(result.get("mock")) is (not valid)
        assert request.await_count == int(valid)
        if valid:
            assert result.get("invoice_url") == "https://provider.example/pay"
            headers = request.call_args.kwargs["headers"]
            if provider != "xendit":
                assert headers["x-api-key"] == ("global-key" if credentials == "global" else "key-test")
                assert headers["x-oy-username" if provider == "oy" else "x-username"] == ("global-user" if credentials == "global" else "user-test")


@pytest.mark.parametrize("mode", ["production", "staging", "test", "development"])
async def test_partner_denial_before_insert(monkeypatch, mode):
    monkeypatch.setattr(settings, "environment", mode)
    b = brand("xendit")
    create = AsyncMock(return_value=target())
    monkeypatch.setattr(partner, "create_partner_order", create)
    monkeypatch.setattr(partner, "_create_invoice", AsyncMock())
    body = partner.PartnerOrderIn(external_order_id="external", amount_idr=1000,
                                 description="Test", buyer={"name": "Buyer", "email": "buyer@example.test", "phone": "+628123456789"})
    if mode in {"production", "staging"}:
        with pytest.raises(HTTPException):
            await partner.register_order(body, b, session())
        create.assert_not_called()
    else:
        # Test the local invoice helper directly (registration serialization is unrelated).
        assert partner._is_mock_mode(b)


@pytest.mark.parametrize("mode", ["production", "staging", "development", "test"])
async def test_existing_partner_demo_target(monkeypatch, mode):
    monkeypatch.setattr(settings, "environment", mode)
    obj = target()
    obj.invoice_id = "dev-partner-old"
    monkeypatch.setattr(pay_page, "_load_order", AsyncMock(return_value=obj))
    if mode in {"production", "staging"}:
        with pytest.raises(HTTPException) as exc:
            await pay_page.pay_target("order", session())
        assert exc.value.status_code == 503
    else:
        assert "mock-checkout" in (await pay_page.pay_target("order", session()))["pay_url"]


@pytest.mark.parametrize("mode", ["production", "staging", "development", "test"])
@pytest.mark.parametrize("existing", [False, True])
async def test_profile_demo_methods(monkeypatch, mode, existing):
    monkeypatch.setattr(settings, "environment", mode)
    demo = NS(id="demo", display_label="BCA Virtual Account — Demo", type=PaymentMethodType.VIRTUAL_ACCOUNT, is_default=True, xendit_token=None)
    db = session([demo] if existing else [])
    result = await profiles.list_payment_methods(NS(id="buyer"), db)
    if mode in {"production", "staging"}:
        assert result == []
        db.add.assert_not_called()
        db.flush.assert_not_called()
    else:
        assert len(result) == 1


@pytest.mark.parametrize("mode", ["production", "staging", "development", "test"])
@pytest.mark.parametrize("selection", [None, "demo", "saved", "missing"])
async def test_order_auth_payment_selection(monkeypatch, selection, mode):
    monkeypatch.setattr(settings, "environment", mode)
    obj = target()
    obj.state = OrderState.PRE_AUTH
    # Satisfy the independently enforced shipping contract without provider I/O.
    from services import shipping_quotes
    b = brand("xendit")
    courier = {"server_free_shipping": True}
    obj.shipping_idr = 0
    obj.shipping_address = {"shipping_binding": shipping_quotes.binding(b, "12345", obj.items, courier)}
    monkeypatch.setattr(shipping_quotes, "select_shipping", AsyncMock(return_value=(obj.items, courier, 0)))
    before = deepcopy(vars(obj))
    monkeypatch.setattr(orders, "lock_order_for_update", AsyncMock(return_value=obj))
    monkeypatch.setattr(orders, "_serialize_order", lambda o: {})
    transition = AsyncMock()
    monkeypatch.setattr(orders, "transition", transition)
    demo = NS(id="demo", display_label="BCA Virtual Account — Demo", type=PaymentMethodType.VIRTUAL_ACCOUNT, xendit_token=None)
    saved = NS(id="saved", display_label="Saved wallet", type=PaymentMethodType.EWALLET)
    db = session(demo if selection == "demo" else saved if selection == "saved" else None)
    db.get = AsyncMock(return_value=b)
    body = orders.AdvanceWithAddressIn(address_inline={"line1": "Test", "postal_code": "12345"}, payment_method_id=selection)
    local = mode in {"development", "test"}
    if selection == "missing" or (selection == "demo" and not local):
        with pytest.raises(HTTPException):
            await orders.advance_to_authed("order", body, NS(id="buyer"), db)
        assert vars(obj) == before
        transition.assert_not_called()
    else:
        monkeypatch.setattr(orders, "_serialize_order", lambda o: {})
        await orders.advance_to_authed("order", body, NS(id="buyer"), db)
        if selection == "saved":
            assert obj.payment_method_snapshot == {"type": "ewallet", "display_label": "Saved wallet"}
        elif local:
            assert obj.payment_method_snapshot["display_label"] == demo.display_label
        else:
            assert obj.payment_method_snapshot is None


async def test_checkout_denial_before_beckn_and_mutation(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    obj = target()
    before = deepcopy(vars(obj))
    beckn = AsyncMock()
    monkeypatch.setattr(checkout.order_flow, "confirm_order_v2", beckn)
    with pytest.raises(HTTPException):
        await checkout.confirm_cart("order", checkout.ConfirmIn(), session(obj, brand("xendit")))
    assert vars(obj) == before
    beckn.assert_not_called()


@pytest.mark.parametrize("prefix", ["dev-", "oy-dev-", "sento-dev-"])
async def test_existing_sdk_demo_not_returned(monkeypatch, prefix):
    monkeypatch.setattr(settings, "environment", "production")
    obj = target()
    obj.payment_method_snapshot = {"invoice_id": prefix + "old", "invoice_url": "https://example.test/api/mock-checkout/old", "type": "xendit_invoice"}
    with pytest.raises(HTTPException):
        await orders.create_invoice("order", NS(id="buyer"), session(obj))
