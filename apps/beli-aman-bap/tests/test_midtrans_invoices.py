"""Invoice reservation ordering and uncertain-outcome recovery (fake DB)."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from services import midtrans_invoices as invoices
from services.midtrans_client import MidtransError
from models.order import OrderState


@pytest.fixture(autouse=True)
def global_midtrans_settings(monkeypatch):
    monkeypatch.setattr(invoices, "settings", SimpleNamespace(midtrans_server_key="secret",
        midtrans_environment="sandbox", midtrans_merchant_id=None))


class Result:
    def __init__(self, value): self.value = value
    def scalar_one(self): return self.value
    def scalar_one_or_none(self): return self.value


class Session:
    def __init__(self, results): self.results = iter(results); self.commits = 0; self.added = []
    @property
    def no_autoflush(self): return nullcontext()
    async def execute(self, query): return Result(next(self.results))
    async def commit(self): self.commits += 1
    async def flush(self): pass
    async def refresh(self, owner, **kwargs): pass
    def add(self, value): self.added.append(value)


def setup(existing=None):
    brand = SimpleNamespace(id="brand", payment_provider="midtrans", midtrans_server_key="secret",
                            midtrans_environment="sandbox", midtrans_merchant_id=None)
    owner = SimpleNamespace(id="order", brand_id="brand", total_idr=10000,
                            state=OrderState.CART_REVIEWED, payment_method_snapshot={})
    db = Session([brand])
    reservation = Session([brand, existing, owner, existing])
    return owner, db, reservation


@pytest.mark.asyncio
async def test_requires_independent_reservation():
    with pytest.raises(RuntimeError):
        await invoices.create_invoice_for_order(None, None)


@pytest.mark.asyncio
async def test_existing_uncertain_link_recovers_without_new_post(monkeypatch):
    link = SimpleNamespace(id="link", brand_id="brand", amount_idr=10000, environment="sandbox",
        base_reference="mt-stable", invoice_url=None, paid_attempt_id=None, status="creating",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30))
    owner, db, session = setup(link)
    async def get(config, reference):
        assert session.commits == 1
        assert reference == "mt-stable"
        return {"order_id": reference, "gross_amount": 10000,
                "payment_link_url": "https://app.sandbox.midtrans.com/payment-links/opaque"}
    async def forbidden(*args, **kwargs): raise AssertionError("Must not create duplicate link")
    monkeypatch.setattr(invoices.client, "get_link", get)
    monkeypatch.setattr(invoices.client, "create_link", forbidden)
    result = await invoices.create_invoice_for_order(db, owner, reservation_db=session)
    assert result["id"] == "mt-stable"
    assert result["invoice_url"].endswith("/opaque")
    assert session.commits == 2
    assert owner.payment_method_snapshot["payment_provider"] == "midtrans"


@pytest.mark.asyncio
async def test_unavailable_recovery_is_retryable(monkeypatch):
    link = SimpleNamespace(id="link", brand_id="brand", amount_idr=10000, environment="sandbox",
        base_reference="mt-stable", invoice_url=None, paid_attempt_id=None, status="creating",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30))
    owner, db, session = setup(link)
    async def unavailable(*args): raise MidtransError(503)
    monkeypatch.setattr(invoices.client, "get_link", unavailable)
    with pytest.raises(HTTPException) as exc:
        await invoices.create_invoice_for_order(db, owner, reservation_db=session)
    assert exc.value.status_code == 503
    assert session.commits == 1


@pytest.mark.asyncio
async def test_total_change_rejected():
    link = SimpleNamespace(amount_idr=9999, environment="sandbox", brand_id="brand")
    owner, db, session = setup(link)
    with pytest.raises(HTTPException) as exc:
        await invoices.create_invoice_for_order(db, owner, reservation_db=session)
    assert exc.value.status_code == 409
    assert session.commits == 0


@pytest.mark.asyncio
async def test_cart_reservation_avoids_outer_autoflush_and_keeps_paid_state(monkeypatch):
    from models.bot_rest import CartStatus
    brand = SimpleNamespace(id="brand", bpp_id="brand.bpp", payment_provider="midtrans",
        midtrans_server_key="secret", midtrans_environment="sandbox", midtrans_merchant_id=None)
    owner = SimpleNamespace(id="cart", bpp_id="brand.bpp", quote_json={"total_idr": 10000},
        order_id="caller-order", payment_state="pending", status=CartStatus.CONFIRMED)
    locked = SimpleNamespace(id="cart", bpp_id="brand.bpp", quote_json={"total_idr": 10000},
        order_id="durable-order", invoice_id="mt-stable", status=CartStatus.CONFIRMED)
    link = SimpleNamespace(id="link", brand_id="brand", amount_idr=10000, environment="sandbox",
        base_reference="mt-stable", invoice_url=None, paid_attempt_id=None, status="creating",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30))
    # No outer-session SELECT: dirty cart state must not autoflush before reservation.
    db = Session([])
    session = Session([brand, link, locked, link])
    async def get(*args):
        assert session.commits == 1
        link.paid_attempt_id = "mt-stable-1234567890"
        link.status = "paid"
        return {"order_id": "mt-stable", "payment_link_url":
            "https://app.sandbox.midtrans.com/payment-links/opaque"}
    async def refresh(cart, **kwargs):
        cart.payment_state = "paid"
        cart.order_id = "durable-order"
    monkeypatch.setattr(invoices.client, "get_link", get)
    monkeypatch.setattr(db, "refresh", refresh)
    result = await invoices.create_invoice_for_cart(db, owner, reservation_db=session)
    assert owner.payment_state == "paid"
    assert owner.order_id == "durable-order"
    assert link.status == "paid"
    assert owner.qr_image_url == result["invoice_url"]
    assert owner.qris_image_url is None
    assert owner.qris_content is None
