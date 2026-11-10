"""Authoritative Midtrans status rules; network-free."""
import pytest
from routers.webhooks_midtrans import verified_status
from services.midtrans_client import MidtransConfig


@pytest.fixture(autouse=True)
def global_midtrans_settings(monkeypatch):
    from types import SimpleNamespace
    from routers import webhooks_midtrans
    monkeypatch.setattr(webhooks_midtrans, "settings", SimpleNamespace(
        midtrans_server_key="secret", midtrans_environment="sandbox", midtrans_merchant_id="merchant"))


def status(**changes):
    result = dict(order_id="mt-test-1234567890123", transaction_id="tx-1",
                  currency="IDR", gross_amount="10000.00", status_code="200",
                  transaction_status="settlement", merchant_id="merchant")
    result.update(changes)
    return result


def check(data):
    return verified_status(data, attempt_id="mt-test-1234567890123", amount=10000,
                           config=MidtransConfig("secret", merchant_id="merchant"))


def test_paid_statuses():
    assert check(status())
    assert check(status(transaction_status="capture", fraud_status="accept"))
    assert not check(status(transaction_status="capture"))
    assert not check(status(transaction_status="capture", fraud_status="challenge"))
    for state in ["pending", "expire", "deny", "cancel", "authorize", "refund", "partial_refund", "unknown"]:
        assert not check(status(transaction_status=state))
    assert not check(status(status_code="201"))


@pytest.mark.parametrize("changes", [dict(order_id="another-1"), dict(currency="USD"),
    dict(gross_amount="9999.00"), dict(merchant_id="another"), dict(transaction_id="")])
def test_mismatch_rejected(changes):
    with pytest.raises(ValueError):
        check(status(**changes))


class Result:
    def __init__(self, value): self.value = value
    def scalar_one(self): return self.value
    def scalar_one_or_none(self): return self.value
    def scalars(self): return self
    def first(self): return self.value


class Session:
    def __init__(self, results):
        self.results = iter(results)
        self.commits = 0
        self.added = []
    async def execute(self, query): return Result(next(self.results))
    async def commit(self): self.commits += 1
    def add(self, value): self.added.append(value)


def notification(**changes):
    import hashlib
    payload = dict(order_id="mt-test-1234567890123", gross_amount="10000.00", status_code="200",
                   transaction_status="settlement")
    payload.update(changes)
    payload["signature_key"] = hashlib.sha512((payload["order_id"] + payload["status_code"] +
                                                payload["gross_amount"] + "secret").encode()).hexdigest()
    return payload


def request(payload):
    import json
    from starlette.requests import Request
    async def receive():
        return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}
    return Request({"type": "http", "method": "POST", "path": "/webhooks/midtrans", "headers": []}, receive)


def fixtures():
    from types import SimpleNamespace
    link = SimpleNamespace(id="link", brand_id="brand", base_reference="mt-test", amount_idr=10000,
                           environment="sandbox", cart_id="cart", order_id=None,
                           paid_attempt_id=None, status="ready")
    brand = SimpleNamespace(midtrans_server_key="secret", midtrans_environment="sandbox",
                            midtrans_merchant_id="merchant")
    cart = SimpleNamespace(id="cart", order_id="cart-order", quote_json={"total_idr": 10000},
                           payment_state="pending", invoice_provider="midtrans")
    return link, brand, cart


@pytest.mark.asyncio
async def test_cart_paid_and_replay_create_one_hold(monkeypatch):
    from routers import webhooks_midtrans as webhook
    link, brand, cart = fixtures()
    async def authoritative(*args): return status()
    monkeypatch.setattr(webhook.client, "get_status", authoritative)
    db = Session([link, brand, link, None, cart, None])
    assert await webhook.midtrans_notification(request(notification()), db) == {"ok": True}
    assert db.commits == 1
    assert cart.payment_state == "paid"
    assert link.paid_attempt_id == "mt-test-1234567890123"
    from models.escrow_ledger import EscrowLedger
    assert len([row for row in db.added if isinstance(row, EscrowLedger)]) == 1
    attempt = next(row for row in db.added if not isinstance(row, EscrowLedger))
    replay = Session([link, brand, link, attempt])
    await webhook.midtrans_notification(request(notification()), replay)
    assert replay.commits == 1
    assert replay.added == []


@pytest.mark.asyncio
async def test_unsigned_status_and_custom_field_cannot_mark_paid(monkeypatch):
    from routers import webhooks_midtrans as webhook
    link, brand, cart = fixtures()
    async def authoritative(*args): return status(transaction_status="pending", status_code="201")
    monkeypatch.setattr(webhook.client, "get_status", authoritative)
    db = Session([link, brand, link, None])
    await webhook.midtrans_notification(request(notification(custom_field1="other-tenant")), db)
    assert link.status == "ready"
    assert link.paid_attempt_id is None
    assert cart.payment_state == "pending"
    assert len(db.added) == 1
    assert db.added[0].status == "pending"


@pytest.mark.asyncio
async def test_second_successful_attempt_flags_reconciliation(monkeypatch):
    from routers import webhooks_midtrans as webhook
    link, brand, _ = fixtures()
    link.paid_attempt_id = "mt-test-1111111111111"
    link.status = "paid"
    async def authoritative(*args): return status()
    monkeypatch.setattr(webhook.client, "get_status", authoritative)
    db = Session([link, brand, link, None])
    await webhook.midtrans_notification(request(notification()), db)
    assert db.added[0].status == "duplicate_paid"
    assert link.paid_attempt_id == "mt-test-1111111111111"
    assert db.commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"gross_amount": "9000"}, {"currency": "USD"},
                                     {"merchant_id": "other"}, {"order_id": "other-123"}])
async def test_endpoint_authoritative_mismatch_never_commits(monkeypatch, changes):
    from fastapi import HTTPException
    from routers import webhooks_midtrans as webhook
    link, brand, _ = fixtures()
    async def authoritative(*args): return status(**changes)
    monkeypatch.setattr(webhook.client, "get_status", authoritative)
    db = Session([link, brand])
    with pytest.raises(HTTPException) as exc:
        await webhook.midtrans_notification(request(notification()), db)
    assert exc.value.status_code == 403
    assert db.commits == 0
    assert db.added == []


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_fails", [False, True])
async def test_sdk_effects_only_after_successful_commit(monkeypatch, commit_fails):
    from types import SimpleNamespace
    from models.order import OrderState
    from routers import webhooks_midtrans as webhook
    link, brand, _ = fixtures()
    link.order_id = "order"
    link.cart_id = None
    order = SimpleNamespace(id="order", brand_id="brand", total_idr=10000, state=OrderState.CART_REVIEWED)
    db = Session([link, brand, link, None, order])
    async def authoritative(*args): return status()
    async def mark_paid(*args, **kwargs):
        assert kwargs["schedule_effects"] is False
        order.state = OrderState.ESCROW_HELD
        return order
    async def failed_commit(): raise RuntimeError("synthetic commit failure")
    scheduled = []
    def schedule(coroutine):
        assert db.commits == 1
        scheduled.append(coroutine)
        coroutine.close()
    monkeypatch.setattr(webhook.client, "get_status", authoritative)
    monkeypatch.setattr(webhook.order_paid, "mark_order_paid", mark_paid)
    monkeypatch.setattr(webhook.asyncio, "create_task", schedule)
    if commit_fails:
        monkeypatch.setattr(db, "commit", failed_commit)
        with pytest.raises(RuntimeError, match="synthetic commit failure"):
            await webhook.midtrans_notification(request(notification()), db)
        assert scheduled == []
    else:
        await webhook.midtrans_notification(request(notification()), db)
        assert len(scheduled) == 2


@pytest.mark.asyncio
async def test_invalid_signature_never_queries_provider(monkeypatch):
    from fastapi import HTTPException
    from routers import webhooks_midtrans as webhook
    link, brand, _ = fixtures()
    async def forbidden(*args): raise AssertionError("Unauthenticated provider query")
    monkeypatch.setattr(webhook.client, "get_status", forbidden)
    payload = notification()
    payload["gross_amount"] = "9000.00"
    db = Session([link, brand])
    with pytest.raises(HTTPException) as exc:
        await webhook.midtrans_notification(request(payload), db)
    assert exc.value.status_code == 403
    assert db.commits == 0
