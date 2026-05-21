"""Task A5 — buyer inbound /on_issue handler.

The handler receives a BPP-emitted /on_issue, looks up the local
Dispute by ``bpp_issue_id`` (assigned at /issue time), and applies the
resolution action to it.

Coverage:

* action=PROCESSING -> Dispute.status = BRAND_RESPONDING.
* action=RESOLVED   -> Dispute.status = RESOLVED, resolution = "resolved",
                        Order.state -> REFUNDED when refund_amount is set.
* action=REJECTED   -> Dispute.status = RESOLVED, resolution = "denied".
* Idempotent: re-applying the same /on_issue is a no-op (the
  BecknInboundLog dedupe also covers retries upstream).
* Unknown issue_id -> ignored (logged, no error).
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

from models.dispute import DisputeReason, DisputeStatus  # noqa: E402
from models.order import OrderState  # noqa: E402
from routers.beckn_handlers import handle_on_issue  # noqa: E402


def _dispute(*, status=DisputeStatus.OPEN, issue_id="issue-1"):
    return types.SimpleNamespace(
        id=str(uuid.uuid4()),
        order_id="order-1",
        opened_by="buyer:p1",
        reason=DisputeReason.NOT_RECEIVED,
        note="paket belum tiba",
        status=status,
        resolution=None,
        resolved_at=None,
        bpp_issue_id=issue_id,
        bpp_refund_request_id=None,
        bpp_resolution_note=None,
    )


def _order(state=OrderState.FULFILLING):
    return types.SimpleNamespace(id="order-1", state=state)


class _DisputeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _FakeDB:
    """Returns a Dispute on .execute() and an Order on .get()."""

    def __init__(self, dispute, order=None):
        self.dispute = dispute
        self.order = order

    async def execute(self, _stmt):
        return _DisputeResult(self.dispute)

    async def get(self, _model, _oid):
        return self.order


_CTX = {"action": "on_issue", "transaction_id": "t1"}


def _on_issue_msg(*, action: str, issue_id="issue-1", refund_amount=None,
                  short="ok", long="long-desc"):
    issue = {
        "id": issue_id,
        "status": action,
        "issue_type": "ISSUE",
        "issue_actions": {
            "complainant_actions": [],
            "respondent_actions": [
                {
                    "respondent_action": action,
                    "short_desc": short,
                    "long_desc": long,
                    "updated_at": "2026-05-20T10:00:00Z",
                }
            ],
        },
    }
    if action in {"RESOLVED", "REJECTED"}:
        res: dict = {
            "short_desc": short,
            "long_desc": long,
            "action_triggered": action,
        }
        if refund_amount is not None:
            res["refund_amount"] = {
                "value": str(refund_amount),
                "currency": "IDR",
            }
        issue["resolution"] = res
    return {"issue": issue}


class TestActions:
    def test_processing_flips_to_brand_responding(self):
        d = _dispute()
        db = _FakeDB(d, _order())
        asyncio.run(
            handle_on_issue(_CTX, _on_issue_msg(action="PROCESSING"), db)
        )
        assert d.status is DisputeStatus.BRAND_RESPONDING

    def test_resolved_marks_dispute_resolved(self):
        d = _dispute()
        order = _order()
        db = _FakeDB(d, order)
        asyncio.run(
            handle_on_issue(
                _CTX,
                _on_issue_msg(action="RESOLVED", short="refund issued"),
                db,
            )
        )
        assert d.status is DisputeStatus.RESOLVED
        assert d.resolution == "resolved"
        assert d.resolved_at  # ISO timestamp set
        # No refund_amount in this case → order state untouched.
        assert order.state is OrderState.FULFILLING

    def test_resolved_with_refund_amount_flips_order_to_refunded(self):
        d = _dispute()
        order = _order(state=OrderState.FULFILLING)
        db = _FakeDB(d, order)
        asyncio.run(
            handle_on_issue(
                _CTX,
                _on_issue_msg(action="RESOLVED", refund_amount=35000),
                db,
            )
        )
        assert d.status is DisputeStatus.RESOLVED
        assert order.state is OrderState.REFUNDED

    def test_rejected_marks_dispute_denied(self):
        d = _dispute()
        db = _FakeDB(d, _order())
        asyncio.run(
            handle_on_issue(
                _CTX,
                _on_issue_msg(action="REJECTED", short="claim invalid"),
                db,
            )
        )
        assert d.status is DisputeStatus.RESOLVED
        assert d.resolution == "denied"
        assert d.bpp_resolution_note is not None

    def test_resolved_persists_resolution_note(self):
        d = _dispute()
        db = _FakeDB(d, _order())
        asyncio.run(
            handle_on_issue(
                _CTX,
                _on_issue_msg(
                    action="RESOLVED",
                    short="refund issued",
                    long="Your refund has been processed via Xendit.",
                ),
                db,
            )
        )
        assert (
            d.bpp_resolution_note
            == "Your refund has been processed via Xendit."
        )


class TestIdempotencyAndUnknownIssue:
    def test_idempotent_resolved_then_resolved_is_terminal(self):
        d = _dispute()
        order = _order()
        db = _FakeDB(d, order)
        # First /on_issue: RESOLVED.
        asyncio.run(
            handle_on_issue(
                _CTX,
                _on_issue_msg(action="RESOLVED", refund_amount=10000),
                db,
            )
        )
        first_resolved_at = d.resolved_at
        assert d.status is DisputeStatus.RESOLVED
        # Re-applying the same /on_issue (BPP retry) must not throw and
        # the dispute must remain in RESOLVED with the same resolution.
        asyncio.run(
            handle_on_issue(
                _CTX,
                _on_issue_msg(action="RESOLVED", refund_amount=10000),
                db,
            )
        )
        assert d.status is DisputeStatus.RESOLVED
        assert d.resolution == "resolved"
        assert d.resolved_at is not None

    def test_unknown_issue_id_is_ignored(self):
        db = _FakeDB(None)  # no Dispute matches issue_id
        # Must not raise.
        asyncio.run(
            handle_on_issue(
                _CTX,
                _on_issue_msg(
                    action="RESOLVED", issue_id="never-seen"
                ),
                db,
            )
        )
