"""Bot/Bella carts: tell the seller-bpp when the cart is actually paid.

The jd-sell MCP checkout sends Beckn /confirm to the seller BEFORE the
shopper pays (``cart.order_id`` = pseudo order id). The seller only awards
OITO loyalty points once it receives
``PATCH /api/internal/escrow-orders/{order_id} {"escrow_status": "held"}``.

These tests pin that every cart → paid transition (Xendit, OY, Sento, Dipay
webhooks + the dev Dipay mock) schedules that PATCH exactly once, never on a
replay, never without an order_id, and never breaks the webhook if the seller
bridge blows up. ``seller_bridge.patch_escrow_status`` is monkeypatched — no
network.
"""

from __future__ import annotations

import asyncio
import os
import sys

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from config import settings  # noqa: E402
from routers import internal_mock  # noqa: E402
from routers import webhooks_dipay, webhooks_oy, webhooks_sento, webhooks_xendit  # noqa: E402
from services import seller_bridge  # noqa: E402
from tests._oy_fakes import FakeSession, StubCart  # noqa: E402


@pytest.fixture
def patch_calls(monkeypatch):
    """Record every seller PATCH instead of hitting the network."""
    calls: list[tuple[str, str]] = []

    async def _fake_patch(*, order_id: str, escrow_status: str) -> bool:
        calls.append((order_id, escrow_status))
        return True

    monkeypatch.setattr(seller_bridge, "patch_escrow_status", _fake_patch)
    return calls


@pytest.fixture
def failing_patch(monkeypatch):
    calls: list[str] = []

    async def _boom(*, order_id: str, escrow_status: str) -> bool:
        calls.append(order_id)
        raise RuntimeError("seller-bpp exploded")

    monkeypatch.setattr(seller_bridge, "patch_escrow_status", _boom)
    return calls


async def _drain() -> None:
    """Let fire-and-forget tasks run to completion. Not using
    return_exceptions: a background task that leaks an exception fails the
    test."""
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


# ---- the helper itself -------------------------------------------------------


class TestNotifyCartPaidHelper:
    def test_schedules_one_held_patch_for_cart_order_id(self, patch_calls):
        cart = StubCart(order_id="pseudo-1")

        async def go():
            assert seller_bridge.notify_cart_paid(cart) is None
            assert patch_calls == []  # fire-and-forget: not awaited inline

        _run(go)
        assert patch_calls == [("pseudo-1", "held")]

    def test_cart_without_order_id_is_noop(self, patch_calls):
        async def go():
            seller_bridge.notify_cart_paid(StubCart(order_id=None))
            seller_bridge.notify_cart_paid(StubCart(order_id=""))

        _run(go)
        assert patch_calls == []

    def test_failing_bridge_is_swallowed(self, failing_patch):
        async def go():
            seller_bridge.notify_cart_paid(StubCart(order_id="pseudo-2"))

        _run(go)  # _drain would raise if the task leaked the exception
        assert failing_patch == ["pseudo-2"]


# ---- every cart → paid transition ------------------------------------------


def _xendit(db, cart):
    return webhooks_xendit._handle_paid(db, "xnd-inv-1", f"cart-{cart.id}")


def _oy(db, cart):
    return webhooks_oy._handle_paid(db, "OY-TRX-1", {"external_id": f"cart-{cart.id}"})


def _sento(db, cart):
    return webhooks_sento._handle_paid(db, f"cart-{cart.id}", {})


def _dipay(db, cart):
    # Dipay resolves the cart before _handle_paid; only the ledger SELECT
    # runs inside, so drop the cart from the FakeSession queue.
    db._results.pop(0)
    return webhooks_dipay._handle_paid(db, "dipay-ref-1", cart, None)


PROVIDERS = {"xendit": _xendit, "oy": _oy, "sento": _sento, "dipay": _dipay}


def _session_for(cart):
    # Queue: cart SELECT, then escrow-ledger dedupe SELECT (None → insert).
    return FakeSession([cart, None] if cart.order_id else [cart])


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
class TestWebhookCartPaid:
    def test_pending_to_paid_notifies_seller_once(self, provider, patch_calls):
        cart = StubCart(payment_state="pending", order_id="pseudo-abc")
        db = _session_for(cart)

        _run(lambda: PROVIDERS[provider](db, cart))

        assert cart.payment_state == "paid"
        assert patch_calls == [("pseudo-abc", "held")]

    def test_replay_on_already_paid_cart_does_not_renotify(self, provider, patch_calls):
        cart = StubCart(payment_state="paid", order_id="pseudo-abc")
        db = _session_for(cart)

        _run(lambda: PROVIDERS[provider](db, cart))

        assert patch_calls == []

    def test_cart_without_order_id_does_not_notify(self, provider, patch_calls):
        cart = StubCart(payment_state="pending", order_id=None)
        db = _session_for(cart)

        _run(lambda: PROVIDERS[provider](db, cart))

        assert cart.payment_state == "paid"
        assert patch_calls == []

    def test_failing_bridge_does_not_break_handler(self, provider, failing_patch):
        cart = StubCart(payment_state="pending", order_id="pseudo-abc")
        db = _session_for(cart)

        out = _run(lambda: PROVIDERS[provider](db, cart))

        assert out["payment_state"] == "paid"
        assert failing_patch == ["pseudo-abc"]


# ---- dev mock-paid path -----------------------------------------------------


class TestInternalMockPaid:
    def _call(self, monkeypatch, cart):
        monkeypatch.setattr(settings, "environment", "test")
        db = FakeSession([cart])
        body = internal_mock.DipayMockPaidIn(invoice_id="dipay-dev-1")
        return _run(lambda: internal_mock.mock_dipay_qris_paid(body, db))

    def test_mock_paid_notifies_seller_once(self, monkeypatch, patch_calls):
        cart = StubCart(payment_state="pending", order_id="pseudo-mock")
        self._call(monkeypatch, cart)
        assert cart.payment_state == "paid"
        assert patch_calls == [("pseudo-mock", "held")]

    def test_mock_paid_replay_does_not_renotify(self, monkeypatch, patch_calls):
        cart = StubCart(payment_state="paid", order_id="pseudo-mock")
        self._call(monkeypatch, cart)
        assert patch_calls == []


# ---- full HTTP round-trip: bridge failure never leaks into the ACK ----------


def test_xendit_http_ack_survives_failing_bridge(monkeypatch, failing_patch):
    from database import get_db

    monkeypatch.setattr(settings, "xendit_webhook_token", "tok")
    cart = StubCart(payment_state="pending", order_id="pseudo-http")
    session = FakeSession([cart, None])

    async def _stub_get_db():
        yield session

    app = FastAPI()
    app.include_router(webhooks_xendit.router)
    app.dependency_overrides[get_db] = _stub_get_db

    with TestClient(app) as client:
        resp = client.post(
            "/webhooks/xendit/invoice",
            json={"id": "xnd-inv-http", "external_id": f"cart-{cart.id}", "status": "PAID"},
            headers={"x-callback-token": "tok"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["payment_state"] == "paid"
    assert cart.payment_state == "paid"
