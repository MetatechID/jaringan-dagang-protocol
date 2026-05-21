"""Task A5 — ONDC IGM /issue outbound: services.igm.open_issue.

The service is the single entry point for raising an IGM Issue against
a BPP order. We exercise it via a fake DB + send hook so the test
doesn't touch Postgres or the network.

Coverage:

* Happy-path open creates a Dispute row + emits /issue envelope with the
  ONDC IGM shape (context.domain = ONDC:RET11, category, sub_category).
* Unknown order_id raises OrderNotFoundError.
* Order in non-eligible state raises OrderNotEligibleError.
* Invalid IGM category / sub_category raises ValueError.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
import uuid

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

import pytest  # noqa: E402

from models.dispute import DisputeReason, DisputeStatus  # noqa: E402
from models.order import OrderState  # noqa: E402
from services import igm as igm_service  # noqa: E402


def _order(*, state=OrderState.ESCROW_HELD, bpp_id="safiyafood.jaringan-dagang.id"):
    return types.SimpleNamespace(
        id=str(uuid.uuid4()),
        bpp_id=bpp_id,
        seller_order_ref="JD-ORD-1",
        state=state,
        profile_id="profile-1",
    )


class _FakeDB:
    """Minimal AsyncSession stand-in for services.igm.open_issue.

    get(Order, id) -> returns the prepared order (or None).
    add(obj)       -> stash so we can inspect the created Dispute.
    flush()        -> no-op.
    """

    def __init__(self, order):
        self._order = order
        self.added: list = []

    async def get(self, _model, _oid):
        return self._order

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def refresh(self, _obj):
        return None


class TestHappyPath:
    def test_creates_dispute_and_emits_envelope(self):
        order = _order()
        db = _FakeDB(order)
        sent: dict = {}

        async def _capture(**kwargs):
            sent.update(kwargs)
            return True

        dispute = asyncio.run(
            igm_service.open_issue(
                db,
                order_id=order.id,
                profile_id="profile-1",
                category="ITEM",
                sub_category="ITM02",
                description="Paket belum tiba meski sudah ditandai terkirim",
                refund_amount=35000,
                complainant_name="Budi",
                complainant_email="budi@example.id",
                send=_capture,
            )
        )

        assert dispute is not None
        assert dispute.status is DisputeStatus.OPEN
        assert dispute.bpp_issue_id, "bpp_issue_id should be assigned"
        # ITM02 maps to NOT_RECEIVED in the dispute reason taxonomy.
        assert dispute.reason is DisputeReason.NOT_RECEIVED

        body = sent["body"]
        ctx = body["context"]
        assert ctx["action"] == "issue"
        assert ctx["bap_id"] == "beli-aman.bap.jaringan-dagang.id"
        assert ctx["bpp_id"] == "safiyafood.jaringan-dagang.id"
        # Safiya resolves to ONDC:RET11 per A2b.
        assert ctx["domain"] == "ONDC:RET11"
        assert ctx["transaction_id"] == order.id

        issue = body["message"]["issue"]
        assert issue["category"] == "ITEM"
        assert issue["sub_category"] == "ITM02"
        assert issue["complainant_info"]["id"] == "profile-1"
        assert issue["complainant_info"]["name"] == "Budi"
        assert issue["order_details"]["id"] == "JD-ORD-1"
        # Refund amount surfaces in additional_desc for the BPP.
        refund = issue["description"]["additional_desc"]["refund"]
        assert refund == {"amount": "35000", "currency": "IDR"}
        # First complainant action records OPEN.
        opens = issue["issue_actions"]["complainant_actions"]
        assert opens and opens[0]["complainant_action"] == "OPEN"

    def test_envelope_uses_seller_order_ref_when_set(self):
        order = _order()
        order.seller_order_ref = "JD-CUSTOM"
        db = _FakeDB(order)
        sent: dict = {}

        async def _capture(**kwargs):
            sent.update(kwargs)
            return True

        asyncio.run(
            igm_service.open_issue(
                db,
                order_id=order.id,
                profile_id="p1",
                category="ITEM",
                sub_category="ITM05",
                description="Produk rusak saat sampai",
                send=_capture,
            )
        )
        issue = sent["body"]["message"]["issue"]
        assert issue["order_details"]["id"] == "JD-CUSTOM"


class TestErrors:
    def test_unknown_order_raises_order_not_found(self):
        class _MissDB(_FakeDB):
            async def get(self, _m, _i):
                return None

        async def _capture(**_):
            return True

        with pytest.raises(igm_service.OrderNotFoundError):
            asyncio.run(
                igm_service.open_issue(
                    _MissDB(None),
                    order_id="no-such-order",
                    profile_id="p1",
                    category="ITEM",
                    sub_category="ITM02",
                    description="x",
                    send=_capture,
                )
            )

    def test_order_in_pre_auth_state_is_not_eligible(self):
        order = _order(state=OrderState.PRE_AUTH)
        db = _FakeDB(order)

        async def _capture(**_):
            return True

        with pytest.raises(igm_service.OrderNotEligibleError):
            asyncio.run(
                igm_service.open_issue(
                    db,
                    order_id=order.id,
                    profile_id="p1",
                    category="ITEM",
                    sub_category="ITM02",
                    description="x",
                    send=_capture,
                )
            )

    def test_order_after_escrow_released_is_not_eligible(self):
        order = _order(state=OrderState.ESCROW_RELEASED)
        db = _FakeDB(order)

        async def _capture(**_):
            return True

        with pytest.raises(igm_service.OrderNotEligibleError):
            asyncio.run(
                igm_service.open_issue(
                    db,
                    order_id=order.id,
                    profile_id="p1",
                    category="ITEM",
                    sub_category="ITM02",
                    description="x",
                    send=_capture,
                )
            )

    def test_invalid_category_raises_value_error(self):
        order = _order()
        db = _FakeDB(order)

        async def _capture(**_):
            return True

        with pytest.raises(ValueError):
            asyncio.run(
                igm_service.open_issue(
                    db,
                    order_id=order.id,
                    profile_id="p1",
                    category="FOOBAR",
                    sub_category="ITM02",
                    description="x",
                    send=_capture,
                )
            )

    def test_invalid_item_subcategory_raises_value_error(self):
        order = _order()
        db = _FakeDB(order)

        async def _capture(**_):
            return True

        with pytest.raises(ValueError):
            asyncio.run(
                igm_service.open_issue(
                    db,
                    order_id=order.id,
                    profile_id="p1",
                    category="ITEM",
                    sub_category="NOT-A-CODE",
                    description="x",
                    send=_capture,
                )
            )


class TestEligibilityStates:
    """Each eligible state must accept an Issue."""

    @pytest.mark.parametrize(
        "state",
        [
            OrderState.ESCROW_HELD,
            OrderState.FULFILLING,
            OrderState.RECEIVED,
            OrderState.DISPUTED,
        ],
    )
    def test_state_accepts_issue(self, state):
        order = _order(state=state)
        db = _FakeDB(order)
        sent: dict = {}

        async def _capture(**kwargs):
            sent.update(kwargs)
            return True

        dispute = asyncio.run(
            igm_service.open_issue(
                db,
                order_id=order.id,
                profile_id="p1",
                category="ITEM",
                sub_category="ITM02",
                description="x",
                send=_capture,
            )
        )
        assert dispute.status is DisputeStatus.OPEN
        assert sent["body"]["message"]["issue"]["category"] == "ITEM"
