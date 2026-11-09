"""Paid partner orders → OITO on the seller-bpp.

Partner orders (``/api/v1/partner``, e.g. consumerland.id tickets) live only
in the BAP DB. When one is paid the BAP POSTs a snapshot to the seller's
``POST /api/oito/internal/partner-checkouts`` (X-Internal-Token = the seller
bridge token) so it is recorded as an OITO transaction.

These tests pin that every partner paid path (Xendit + Dipay webhooks)
schedules that POST, that replays re-send (the seller is idempotent on the
partner order id, so a replay doubles as a retry), that non-paid orders are
skipped, and that a failing seller never breaks the webhook ACK. No network:
``seller_bridge.load_partner_checkout`` / ``post_partner_checkout`` are
monkeypatched, and the HTTP poster is exercised against ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

import httpx  # noqa: E402
import pytest  # noqa: E402

from config import settings  # noqa: E402
from routers import webhooks_dipay, webhooks_xendit  # noqa: E402
from services import partner_webhook, seller_bridge  # noqa: E402
from tests._dipay_fakes import StubPartnerOrder  # noqa: E402
from tests._oy_fakes import FakeSession  # noqa: E402


async def _drain() -> None:
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current]
    if pending:
        await asyncio.gather(*pending)


def _run(coro_fn):
    async def _main():
        result = await coro_fn()
        await _drain()
        return result
    return asyncio.run(_main())


@pytest.fixture(autouse=True)
def _no_partner_callback(monkeypatch):
    """The partner's own signed callback is out of scope here."""
    async def _noop(_order_id):
        return None
    monkeypatch.setattr(partner_webhook, "notify_partner_paid_bg", _noop)


@pytest.fixture
def posts(monkeypatch):
    """Loader returns a minimal payload; record every POST."""
    sent: list[dict] = []

    async def _load(partner_order_id: str):
        return {"partner_order_id": partner_order_id}

    async def _post(payload: dict) -> bool:
        sent.append(payload)
        return True

    monkeypatch.setattr(seller_bridge, "load_partner_checkout", _load)
    monkeypatch.setattr(seller_bridge, "post_partner_checkout", _post)
    return sent


@pytest.fixture
def failing_post(monkeypatch):
    calls: list[str] = []

    async def _load(partner_order_id: str):
        return {"partner_order_id": partner_order_id}

    async def _boom(payload: dict) -> bool:
        calls.append(payload["partner_order_id"])
        raise RuntimeError("seller-bpp exploded")

    monkeypatch.setattr(seller_bridge, "load_partner_checkout", _load)
    monkeypatch.setattr(seller_bridge, "post_partner_checkout", _boom)
    return calls


def _paid_order(**kw):
    return StubPartnerOrder(
        id="po-1", status="paid", paid_at=datetime(2026, 11, 7, 9, 30, tzinfo=timezone.utc),
        buyer={"name": "Siti", "email": "siti@example.com", "phone": "081234567890"},
        items=[{"name": "Tiket VIP", "qty": 2, "unit_price_idr": 50_000}],
        **kw,
    )


# ---- payload + HTTP poster --------------------------------------------------


def test_payload_shape():
    p = seller_bridge.partner_checkout_payload(_paid_order(), "consumerland")
    assert p == {
        "partner_order_id": "po-1",
        "brand_slug": "consumerland",
        "external_order_id": "CL26-ABC123",
        "amount_idr": 100_000,
        "paid_at": "2026-11-07T09:30:00+00:00",
        "buyer": {"name": "Siti", "email": "siti@example.com", "phone": "081234567890"},
        "items": [{"name": "Tiket VIP", "qty": 2, "unit_price_idr": 50_000}],
        "invoice_provider": "dipay",
    }


def test_loader_reads_order_and_brand_slug_and_skips_unpaid(monkeypatch):
    import database

    def _session_with(results):
        session = FakeSession(results)

        class Ctx:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *a):
                return None
        return Ctx

    monkeypatch.setattr(database, "async_session", _session_with([_paid_order(), "consumerland"]))
    out = asyncio.run(seller_bridge.load_partner_checkout("po-1"))
    assert out["brand_slug"] == "consumerland" and out["partner_order_id"] == "po-1"

    monkeypatch.setattr(database, "async_session", _session_with([StubPartnerOrder(status="expired")]))
    assert asyncio.run(seller_bridge.load_partner_checkout("po-1")) is None
    monkeypatch.setattr(database, "async_session", _session_with([None]))
    assert asyncio.run(seller_bridge.load_partner_checkout("po-1")) is None


def test_post_hits_oito_internal_endpoint_with_bridge_token(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"recorded": True})

    real = httpx.AsyncClient
    monkeypatch.setattr(seller_bridge.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(settings, "seller_bridge_enabled", True)
    monkeypatch.setattr(settings, "seller_bridge_url", "http://seller.test/")
    monkeypatch.setattr(settings, "seller_bridge_token", "tok")

    assert asyncio.run(seller_bridge.post_partner_checkout({"partner_order_id": "po-1"})) is True
    [req] = seen
    assert req.method == "POST"
    assert str(req.url) == "http://seller.test/api/oito/internal/partner-checkouts"
    assert req.headers["X-Internal-Token"] == "tok"
    assert json.loads(req.content) == {"partner_order_id": "po-1"}


def test_post_non_2xx_and_disabled_return_false(monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(seller_bridge.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(404)), **kw))
    monkeypatch.setattr(settings, "seller_bridge_enabled", True)
    assert asyncio.run(seller_bridge.post_partner_checkout({"partner_order_id": "po-1"})) is False
    monkeypatch.setattr(settings, "seller_bridge_enabled", False)
    assert asyncio.run(seller_bridge.post_partner_checkout({"partner_order_id": "po-1"})) is False


# ---- the scheduling helper -----------------------------------------------------


class TestNotifyPartnerOrderPaid:
    def test_schedules_one_post_for_a_paid_order(self, posts):
        async def go():
            assert seller_bridge.notify_partner_order_paid(_paid_order()) is None
            assert posts == []  # fire-and-forget

        _run(go)
        assert posts == [{"partner_order_id": "po-1"}]

    def test_unpaid_order_is_noop(self, posts):
        async def go():
            seller_bridge.notify_partner_order_paid(StubPartnerOrder(status="pending"))
            seller_bridge.notify_partner_order_paid(StubPartnerOrder(status="expired"))

        _run(go)
        assert posts == []

    def test_failing_seller_is_swallowed(self, failing_post):
        async def go():
            seller_bridge.notify_partner_order_paid(_paid_order())

        _run(go)  # _drain would raise if the task leaked
        assert failing_post == ["po-1"]


# ---- every partner paid path -------------------------------------------------


def _xendit(order):
    db = FakeSession([order])
    return webhooks_xendit._handle_paid(db, "xnd-inv-1", f"partner-{order.id}")


def _dipay(order):
    return webhooks_dipay._handle_paid(FakeSession([]), "q-ref-1", None, None, order)


PROVIDERS = {"xendit": _xendit, "dipay": _dipay}


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
class TestWebhookPartnerPaid:
    def test_pending_to_paid_notifies_seller(self, provider, posts):
        order = StubPartnerOrder(id="po-9", status="pending", invoice_id=None, invoice_provider=None)
        out = _run(lambda: PROVIDERS[provider](order))
        assert out["status"] == "paid"
        assert posts == [{"partner_order_id": "po-9"}]

    def test_replay_resends_for_seller_side_idempotency(self, provider, posts):
        order = _paid_order()
        _run(lambda: PROVIDERS[provider](order))
        assert posts == [{"partner_order_id": "po-1"}]

    def test_expired_order_is_not_sent(self, provider, posts):
        order = StubPartnerOrder(status="expired")
        out = _run(lambda: PROVIDERS[provider](order))
        assert out["status"] == "expired"
        assert posts == []

    def test_failing_seller_does_not_break_handler(self, provider, failing_post):
        order = StubPartnerOrder(id="po-9", status="pending")
        out = _run(lambda: PROVIDERS[provider](order))
        assert out["status"] == "paid"
        assert failing_post == ["po-9"]
