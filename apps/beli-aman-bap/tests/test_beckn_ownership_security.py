"""Signed router-level regression tests with generated keys and offline DB fakes."""
import copy
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from jaringan_dagang_beckn import BecknSigner, generate_keypair

from config import settings
from models.bot_rest import Cart, CartStatus
from models.dispute import Dispute, DisputeStatus
from models.order import Order, OrderState
from models.order_rating import OrderRating
from models.mirror import MirrorStore
from models.beckn_logs import BecknInboundLog
from routers import beckn, beckn_handlers

ALICE = "alice.example.test"
BOB = "bob.example.test"


class Result:
    def __init__(self, value):
        self.value = value
    def scalar_one_or_none(self):
        return self.value
    def scalars(self):
        return self
    def first(self):
        return self.value


class DB:
    """Evaluate security-relevant query predicates, not just queued results."""
    def __init__(self):
        self.order = NS(id="local-order", bpp_id=ALICE, seller_order_ref="seller-order",
                        state=OrderState.FULFILLING, fulfillment_status="pending")
        self.cart = NS(id="cart", bpp_id=ALICE, transaction_id="cart-transaction", order_id="cart-order",
                       status=CartStatus.CONFIRMED, invoice_provider="xendit", qr_image_url=None,
                       payment_state="pending", updated_at=None)
        self.dispute = NS(id="dispute", order_id="local-order", bpp_issue_id="issue",
                          status=DisputeStatus.OPEN, resolution=None, bpp_refund_request_id=None,
                          note=None, bpp_resolution_note=None)
        self.rating = NS(order_id="local-order", acknowledged=False, acknowledged_at=None)
        self.store = None
        self.logs = []
        self.queries = []
        self.added = []
        self.commit = AsyncMock()
        self.rollback = AsyncMock()
        self.flush = AsyncMock()

    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def get(self, model, key):
        self.queries.append((model.__name__, key))
        return self.order if model is Order and self.order is not None and self.order.id == key else None
    async def execute(self, stmt):
        text = str(stmt)
        self.queries.append(text)
        params = stmt.compile().params
        if "beckn_inbound_logs" in text:
            for log in self.logs:
                if all(getattr(log, k.rsplit("_", 1)[0], None) == v for k, v in params.items()):
                    return Result(log)
            return Result(None)
        if "orders" in text:
            return Result(self.order)
        if "disputes" in text:
            return Result(self.dispute)
        if "carts" in text:
            return Result(self.cart if self.cart else None)
        if "order_ratings" in text:
            return Result(self.rating)
        if "mirror_stores" in text:
            return Result(self.store)
        return Result(None)
    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, BecknInboundLog):
            self.logs.append(obj)
    def add_all(self, objects):
        self.added.extend(objects)


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(beckn, "REQUIRE_SIGNATURE", True)
    identities = {sid: generate_keypair() for sid in (ALICE, BOB)}
    registry = NS(lookup=AsyncMock(side_effect=lambda sid: NS(signing_public_key_b64=identities[sid].public_key_base64)))
    monkeypatch.setattr(beckn, "_get_registry", lambda: registry)
    db = DB()
    monkeypatch.setattr(beckn, "async_session", lambda: db)
    app = FastAPI()
    app.include_router(beckn.router)
    return app, db, identities, registry


async def send(setup, action, message=None, *, signer=ALICE, claimed=None, transaction="local-order", message_id="message", context_extra=None, unsigned=False):
    app, db, identities, registry = setup
    context = {"bpp_id": claimed or signer, "bap_id": settings.subscriber_id, "action": action,
               "transaction_id": transaction, "message_id": message_id}
    context.update(context_extra or {})
    body = json.dumps({"context": context, "message": message or {}}).encode()
    auth = BecknSigner(identities[signer].signing_key, signer, "test-key").sign(body)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(f"/api/v1/beckn/{action}", content=body, headers={} if unsigned else {"Authorization": auth})


def message_for(action):
    if action in {"on_issue", "on_issue_status"}:
        return {"issue": {"id": "issue", "status": "REJECTED"}}
    if action == "on_rating":
        return {"feedback_ack": True}
    if action == "on_settle":
        return {"settlement": {"order_id": "seller-order", "settlement_status": "PAID"}}
    if action == "on_search":
        return {"catalog": {"bpp/providers": [{"id": ALICE, "descriptor": {"name": "Alice"}, "items": []}]}}
    return {"order": {"id": "seller-order", "fulfillments": [{"state": {"descriptor": {"code": "delivered"}}}],
                      "tags": [{"code": "refund_denied", "list": []}]}}


@pytest.mark.parametrize("action", beckn._BECKN_ACTIONS)
async def test_context_impersonation_denied_before_db(setup, action):
    response = await send(setup, action, message_for(action), signer=BOB, claimed=ALICE)
    assert response.status_code == 403
    assert setup[1].queries == []
    setup[1].commit.assert_not_called()


@pytest.mark.parametrize("mode", ["production", "staging", "", "unknown", "development", "test"])
async def test_unsigned_never_reaches_state(setup, monkeypatch, mode):
    monkeypatch.setattr(settings, "environment", mode)
    monkeypatch.setattr(beckn, "REQUIRE_SIGNATURE", False)
    response = await send(setup, "on_status", message_for("on_status"), unsigned=True)
    assert response.status_code == 401
    assert setup[1].queries == []


@pytest.mark.parametrize("action", ["on_status", "on_confirm", "on_update", "on_settle", "on_rating", "on_issue", "on_issue_status"])
async def test_other_signer_cannot_mutate_owned_resources(setup, action):
    db = setup[1]
    before = copy.deepcopy((vars(db.order), vars(db.cart), vars(db.dispute), vars(db.rating)))
    response = await send(setup, action, message_for(action), signer=BOB)
    assert response.status_code == 403
    assert (vars(db.order), vars(db.cart), vars(db.dispute), vars(db.rating)) == before
    assert not db.added
    db.rollback.assert_awaited()


@pytest.mark.parametrize("action", ["on_status", "on_confirm", "on_update", "on_settle", "on_rating", "on_issue", "on_issue_status"])
async def test_valid_owner_callback_updates(setup, action):
    response = await send(setup, action, message_for(action))
    assert response.status_code == 200, response.text
    setup[1].commit.assert_awaited_once()
    assert len(setup[1].logs) == 1
    if action == "on_status":
        assert setup[1].order.fulfillment_status == "delivered"
    elif action in {"on_issue", "on_issue_status"}:
        assert setup[1].dispute.status == DisputeStatus.RESOLVED
        assert setup[1].dispute.resolution == "denied"
    elif action == "on_rating":
        assert setup[1].rating.acknowledged is True
    elif action == "on_settle":
        assert setup[1].order.settlement_status == "PAID"


@pytest.mark.parametrize("action", ["on_status", "on_confirm", "on_update", "on_settle", "on_issue", "on_issue_status", "on_rating"])
async def test_wrong_transaction_rejected(setup, action):
    response = await send(setup, action, message_for(action), transaction="another-transaction")
    assert response.status_code in {403, 404}
    assert not setup[1].logs


@pytest.mark.parametrize("action", ["on_status", "on_update", "on_settle", "on_confirm"])
async def test_wrong_resource_rejected(setup, action):
    message = message_for(action)
    (message["settlement"] if action == "on_settle" else message["order"])["order_id" if action == "on_settle" else "id"] = "unrelated-order"
    response = await send(setup, action, message)
    assert response.status_code == 403
    assert not setup[1].logs


async def test_replay_is_scoped_to_signer_action_and_transaction(setup, monkeypatch):
    handler = AsyncMock(return_value=None)
    monkeypatch.setattr(beckn_handlers, "handle_on_search", handler)
    assert (await send(setup, "on_search")).status_code == 200
    assert (await send(setup, "on_search")).status_code == 200
    assert handler.await_count == 1
    assert (await send(setup, "on_search", signer=BOB)).status_code == 200
    assert handler.await_count == 2
    assert (await send(setup, "on_search", transaction="another")).status_code == 200
    assert handler.await_count == 3
    assert (await send(setup, "on_support")).status_code == 200
    assert len(setup[1].logs) == 4


async def test_replay_does_not_bypass_ownership(setup):
    assert (await send(setup, "on_status", message_for("on_status"))).status_code == 200
    setup[1].order.bpp_id = BOB
    assert (await send(setup, "on_status", message_for("on_status"))).status_code == 403


@pytest.mark.parametrize("context_extra", [{"action": "on_update"}, {"bap_id": "other-bap"}, {"transaction_id": ""}, {"message_id": ""}])
async def test_invalid_envelope_denied_before_db(setup, context_extra):
    response = await send(setup, "on_status", message_for("on_status"), context_extra=context_extra)
    assert response.status_code in {400, 403}
    assert setup[1].queries == []


async def test_payload_cannot_assert_verified_signer(setup):
    response = await send(setup, "on_status", message_for("on_status"), signer=BOB,
                          context_extra={"_verified_signer": ALICE, "signer": ALICE})
    assert response.status_code == 403


async def test_registry_outage_fails_even_when_signature_flag_disabled(setup, monkeypatch):
    monkeypatch.setattr(beckn, "REQUIRE_SIGNATURE", False)
    setup[3].lookup.side_effect = RuntimeError("offline registry")
    response = await send(setup, "on_status", message_for("on_status"))
    assert response.status_code == 503
    assert not setup[1].queries


@pytest.mark.parametrize("action", ["on_status", "on_search"])
async def test_handler_failure_not_acknowledged_or_cached(setup, monkeypatch, action):
    monkeypatch.setattr(beckn_handlers, f"handle_{action}", AsyncMock(side_effect=RuntimeError("fail")))
    response = await send(setup, action, message_for(action) if action != "on_search" else {})
    assert response.status_code == 500
    setup[1].rollback.assert_awaited()
    assert not setup[1].logs
    setup[1].commit.assert_not_called()


@pytest.mark.parametrize("alias", ["bpp/providers", "providers"])
async def test_catalog_provider_takeover_rejected(setup, alias):
    response = await send(setup, "on_search", {"catalog": {alias: [{"id": ALICE}]}}, signer=BOB)
    assert response.status_code == 403
    assert not setup[1].added


async def test_catalog_slug_collision_cannot_rotate_owner(setup):
    setup[1].store = NS(id="store", bpp_id=ALICE, slug="alice")
    response = await send(setup, "on_search", {"catalog": {"providers": [{"id": BOB, "descriptor": {"name": "Alice"}}]}}, signer=BOB)
    assert response.status_code == 403
    assert setup[1].store.bpp_id == ALICE
    assert not setup[1].added


@pytest.mark.parametrize("provider", ["xendit", "oy", "sento", "dipay", None])
@pytest.mark.parametrize("payment_state", ["pending", "cancelled", "expired", "paid"])
async def test_cart_qr_never_proves_payment(setup, provider, payment_state):
    db = setup[1]
    db.order = None
    db.cart.invoice_provider = provider
    db.cart.payment_state = payment_state
    response = await send(setup, "on_confirm", {"order": {"id": "cart-order", "payments": [{"params": {"qr_image_url": "https://example.test/qr"}}]}}, transaction="cart-transaction")
    assert response.status_code == 200, response.text
    assert db.cart.payment_state == payment_state


@pytest.mark.parametrize("signer,reference", [(BOB, "cart-order"), (ALICE, "other-order")])
async def test_cart_other_owner_or_reference_rejected(setup, signer, reference):
    db = setup[1]
    db.order = None
    before = copy.deepcopy(vars(db.cart))
    response = await send(setup, "on_confirm", {"order": {"id": reference}}, signer=signer, transaction="cart-transaction")
    assert response.status_code == 403
    assert vars(db.cart) == before


async def post_raw(setup, raw, *, signed_body=None):
    app, _, identities, _ = setup
    auth = BecknSigner(identities[ALICE].signing_key, ALICE, "test-key").sign(
        raw if signed_body is None else signed_body
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/api/v1/beckn/on_status", content=raw, headers={"Authorization": auth})


async def test_tampered_signed_body_denied_before_db(setup):
    response = await post_raw(setup, b'{"tampered":true}', signed_body=b'{"tampered":false}')
    assert response.status_code == 401
    assert not setup[1].queries


@pytest.mark.parametrize("body", [b'[]', b'null', b'not-json', b'{"context":[],"message":{}}'])
async def test_signed_malformed_envelope_not_acknowledged(setup, body):
    response = await post_raw(setup, body)
    assert response.status_code == 400
    assert not setup[1].queries


async def test_commit_failure_returns_error_and_rolls_back(setup):
    setup[1].commit.side_effect = RuntimeError("commit failed")
    response = await send(setup, "on_status", message_for("on_status"))
    assert response.status_code == 500
    setup[1].rollback.assert_awaited_once()
    # The fake captures add() attempts, not durable transactional persistence.
    setup[1].commit.assert_awaited_once()


async def test_valid_catalog_owner_creates_own_store(setup):
    response = await send(setup, "on_search", message_for("on_search"))
    assert response.status_code == 200, response.text
    stores = [obj for obj in setup[1].added if isinstance(obj, MirrorStore)]
    assert len(stores) == 1
    assert stores[0].bpp_id == ALICE


async def test_handler_rejects_plain_json_identity(setup):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await beckn_handlers.handle_on_status(
            {"bpp_id": ALICE, "transaction_id": "local-order"},
            message_for("on_status"), setup[1],
        )
    assert exc.value.status_code == 403
    assert not setup[1].queries
