"""Jubelio webhook router — unit tests.

Covers the signature-verification branch of ``routers/webhooks_jubelio.py``
against the contract v1.8 §7 webhook scheme:

    signature = HMAC_SHA256(secret, body_bytes + secret_bytes).hexdigest()

Sent in the ``x-jubelio-signature`` header. Public status-dispatch
behavior (DELIVERED → RECEIVED, RETURNED/CANCELED/SHIPMENT_ISSUE →
DISPUTED, etc.) is exercised indirectly through the same handler — we
stub out DB lookups and the state-machine ``transition`` so the focus
stays on HMAC + body parsing.

DB fakes live in ``tests/_jubelio_fakes``.
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.webhooks_jubelio import router as jubelio_router  # noqa: E402
from tests._jubelio_fakes import (  # noqa: E402
    FakeSession,
    StubOrder,
    sign as _sign,
)

# Configurable webhook secret — must mirror what the production
# JUBELIO_WEBHOOK_TOKEN env var holds. Tests override the settings
# value via monkeypatch.setattr inside each test.
_WEBHOOK_SECRET = "jubelio-shared-secret"


@pytest.fixture
def client():
    """Build a TestClient with the Jubelio webhook router mounted.

    Tests install their FakeSession via ``app.dependency_overrides[get_db]``
    and set ``settings.jubelio_webhook_token`` via monkeypatch.
    """
    app = FastAPI()
    app.include_router(jubelio_router)
    return TestClient(app), app


@pytest.fixture(autouse=True)
def _set_webhook_token(monkeypatch):
    """Default the configured webhook token to a known value.

    Individual tests can override by calling ``monkeypatch.setattr`` again
    (monkeypatch always restores the original at teardown, so this stays
    isolated per test).
    """
    from config import settings

    monkeypatch.setattr(settings, "jubelio_webhook_token", _WEBHOOK_SECRET)


def _patch_db(app, *results):
    """Install a stubbed get_db via FastAPI's dependency_overrides dict."""
    from database import get_db as real_get_db

    session = FakeSession(list(results))

    async def _stub_get_db():
        yield session

    app.dependency_overrides[real_get_db] = _stub_get_db
    return session


def _stub_transition(monkeypatch, target_state):
    """Replace ``services.state_machine.transition`` with a no-op that
    just flips ``order.state`` so downstream assertions can read it."""
    from services import state_machine

    async def _fake_transition(db, order, new_state, **_):
        order.state = new_state
        return None

    monkeypatch.setattr(state_machine, "transition", _fake_transition)
    return target_state


def _stub_lock(db_value):
    """Build a fake ``lock_order_for_update`` that yields ``db_value``."""

    async def _fake_lock(_db, order_id):
        return db_value

    return _fake_lock


# ---- Signature verification (contract v1.8 §7) -----------------------------


class TestSignature:
    SECRET = "jubelio-shared-secret"

    def _body_with_sig(self, **overrides) -> tuple[bytes, str]:
        """Build a (body_bytes, signature) pair honoring the contract."""
        body = {
            "event": "awb",
            "ref_no": "order-abc",
            "awb": "TESTAWB123",
            "shipment_id": "1281",
            "latest_status": "DELIVERED",
            "tracking": {
                "date": "2025-01-01T08:56:32.375000Z",
                "status": "D09",
                "status_detail": "DELIVERED",
            },
        }
        body.update(overrides)
        raw = json.dumps(body).encode()
        return raw, _sign(raw, self.SECRET)

    def test_valid_hmac_signature_accepted(self, monkeypatch, client):
        client, app = client
        body_bytes, sig = self._body_with_sig()
        # shipment_id lookup returns the order; ref_no lookup is skipped
        # because shipment_id won the race.
        order = StubOrder(jubelio_shipment_id="1281", state="FULFILLING")
        _patch_db(app, order)
        monkeypatch.setattr(
            "routers.webhooks_jubelio.lock_order_for_update",
            _stub_lock(order),
        )
        from services.state_machine import OrderState

        async def _fake_transition(db, o, new_state, **_):
            o.state = new_state
            return None

        monkeypatch.setattr(
            "routers.webhooks_jubelio.transition", _fake_transition
        )

        resp = client.post(
            "/webhooks/jubelio",
            content=body_bytes,
            headers={
                "x-jubelio-signature": sig,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True
        # Must not have logged a 401 on a legit signature.
        assert resp.json().get("state") != "REJECTED"

    def test_bad_signature_rejected(self, monkeypatch, client):
        client, app = client
        body_bytes, _sig = self._body_with_sig()
        _patch_db(app)

        resp = client.post(
            "/webhooks/jubelio",
            content=body_bytes,
            headers={
                "x-jubelio-signature": "deadbeef" * 8,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 401, resp.text

    def test_returns_401_when_signature_header_missing(self, monkeypatch, client):
        client, app = client
        body_bytes, _sig = self._body_with_sig()
        _patch_db(app)

        resp = client.post(
            "/webhooks/jubelio",
            content=body_bytes,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 401, resp.text
        assert "signature" in resp.json()["detail"].lower()

    def test_returns_503_when_webhook_token_unset(self, monkeypatch, client):
        """No configured secret → 503 (defensive: don't accept unsigned
        callbacks just because config is missing)."""
        client, app = client
        from config import settings

        monkeypatch.setattr(settings, "jubelio_webhook_token", "")
        body_bytes, sig = self._body_with_sig()
        _patch_db(app)

        resp = client.post(
            "/webhooks/jubelio",
            content=body_bytes,
            headers={
                "x-jubelio-signature": sig,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 503, resp.text
        assert "JUBELIO_WEBHOOK_TOKEN" in resp.json()["detail"]

    def test_signature_uses_compare_digest_not_equality(self, monkeypatch, client):
        """Guard against regression to ``==`` (timing-leak safe comparison).

        We sign ``body + b' '`` and submit the original body — sigs are
        equal-length hex but mismatched. With ``hmac.compare_digest`` the
        handler returns a clean 401; with ``==`` it would too, but the
        deeper intent here is to ensure no incidental bytewise match
        short-circuits verification.
        """
        client, app = client
        body_bytes, _sig = self._body_with_sig()
        _patch_db(app)
        wrong_sig = _sign(body_bytes + b" ", self.SECRET)

        resp = client.post(
            "/webhooks/jubelio",
            content=body_bytes,
            headers={
                "x-jubelio-signature": wrong_sig,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 401, resp.text

    def test_verify_helper_directly_rejects_bad_signature(self):
        """The verification helper is exported: test it in isolation so a
        regression in the routing layer doesn't mask a helper bug."""
        from routers.webhooks_jubelio import _verify_signature
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            _verify_signature(b"hello", "deadbeef" * 8, _WEBHOOK_SECRET)
        assert ei.value.status_code == 401

    def test_verify_helper_rejects_when_secret_unset(self):
        """Empty configured secret must 503, not silently accept."""
        from routers.webhooks_jubelio import _verify_signature
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            _verify_signature(b"hello", _sign(b"hello", "ignored"), "")
        assert ei.value.status_code == 503


# ---- Status dispatch (smoke-level coverage) -------------------------------


class TestStatusDispatch:
    SECRET = "jubelio-shared-secret"

    def _post(self, test_client: TestClient, app, status: str) -> tuple[int, dict]:
        body = {
            "event": "awb",
            "ref_no": "order-abc",
            "awb": "AWB-1",
            "shipment_id": "100",
            "latest_status": status,
            "tracking": {
                "date": "2025-01-01T00:00:00Z",
                "status": "X",
                "status_detail": "x",
            },
        }
        raw = json.dumps(body).encode()
        sig = _sign(raw, self.SECRET)
        order = StubOrder(jubelio_shipment_id="100")
        _patch_db(app, order)
        resp = test_client.post(
            "/webhooks/jubelio",
            content=raw,
            headers={
                "x-jubelio-signature": sig,
                "Content-Type": "application/json",
            },
        )
        return resp.status_code, resp.json()

    def test_delivered_locks_in_transition(self, monkeypatch, client):
        client, app = client
        from services.state_machine import OrderState

        captured: dict = {}

        async def _fake_transition(db, order, new_state, **kw):
            captured["state"] = new_state
            captured["actor"] = kw.get("actor")
            order.state = new_state
            return None

        monkeypatch.setattr(
            "routers.webhooks_jubelio.transition", _fake_transition
        )
        status, body = self._post(client, app, "DELIVERED")
        assert status == 200, body
        assert captured["state"] == OrderState.RECEIVED
        assert captured["actor"] == "system:jubelio_webhook"

    def test_canceled_triggers_dispute(self, monkeypatch, client):
        client, app = client
        from services.state_machine import OrderState

        captured: dict = {}

        async def _fake_transition(db, order, new_state, **kw):
            captured["state"] = new_state
            order.state = new_state
            return None

        monkeypatch.setattr(
            "routers.webhooks_jubelio.transition", _fake_transition
        )
        status, body = self._post(client, app, "CANCELED")
        assert status == 200, body
        assert captured["state"] == OrderState.DISPUTED

    def test_in_transit_persists_status_no_transition(self, monkeypatch, client):
        client, app = client
        called = {"n": 0}

        async def _fake_transition(db, order, new_state, **_):
            called["n"] += 1
            return None

        monkeypatch.setattr(
            "routers.webhooks_jubelio.transition", _fake_transition
        )
        status, body = self._post(client, app, "ON_DELIVERY")
        assert status == 200, body
        # No state-machine call for in-transit events.
        assert called["n"] == 0
