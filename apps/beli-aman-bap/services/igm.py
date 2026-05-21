"""ONDC IGM (Issue & Grievance Management) — buyer-side service.

Task A5 scope (YAGNI / narrow): refund-request Issue path only. Buyer
opens an Issue against a BPP order, the BPP responds via /on_issue,
both sides track the dispute through PROCESSING -> RESOLVED | REJECTED.

This service is the single entry point used by the BAP REST endpoint
(``POST /api/v1/orders/{order_id}/issue``) and any internal callers
(e.g. ops tooling) to ensure every /issue we emit:

  1. Resolves the local Order + validates that the order is in an
     IGM-eligible state (ESCROW_HELD <= state < ESCROW_RELEASED).
  2. Persists a ``Dispute`` row with the assigned ``bpp_issue_id`` so
     /on_issue can correlate.
  3. Builds + signs + POSTs the /issue envelope to the BPP via the
     existing ``beckn.outbound.send_beckn_request`` plumbing.

Multi-party IGM (GRIEVANCE / DISPUTE / ODR), other Issue categories,
auto-refund timeouts and ESCALATE flows are deferred to A6/future.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

# Make beckn-protocol importable. apps/beli-aman-bap/services -> .. -> ..
# (apps) -> .. (buyer repo root) -> packages/beckn-protocol/.
_proto_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "packages", "beckn-protocol")
)
if _proto_path not in sys.path:
    sys.path.insert(0, _proto_path)

from beckn_protocol import (  # noqa: E402
    ISSUE_CATEGORIES,
    ISSUE_SUB_CATEGORIES_ITEM,
    build_issue_envelope,
)

from config import settings  # noqa: E402
from models.dispute import Dispute, DisputeReason, DisputeStatus  # noqa: E402
from models.order import Order, OrderState  # noqa: E402

logger = logging.getLogger(__name__)


class IgmError(Exception):
    """Application-level IGM errors (state-eligibility, validation)."""


class OrderNotFoundError(IgmError):
    """Issue raised against an order the BAP doesn't know about."""


class OrderNotEligibleError(IgmError):
    """Order is in a state that doesn't permit raising an Issue.

    ONDC IGM v1 requires the order to have entered the post-purchase
    lifecycle (ESCROW_HELD or later) and to not be a terminal happy-path
    (ESCROW_RELEASED). Refund + dispute states ARE eligible — buyers can
    raise an Issue on top of an already-disputed order.
    """


_ELIGIBLE_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.ESCROW_HELD,
        OrderState.FULFILLING,
        OrderState.RECEIVED,
        OrderState.DISPUTED,
        # REFUNDED is excluded: nothing left to dispute on a refunded order.
        # ESCROW_RELEASED is excluded: the buyer accepted; no IGM after release.
    }
)


# Map IGM ITEM sub-categories to our internal DisputeReason enum so the
# Dispute row carries a consistent reason regardless of whether it was
# opened via /api/v1/disputes (legacy) or /api/v1/orders/{id}/issue (IGM).
_SUB_TO_REASON: dict[str, DisputeReason] = {
    "ITM01": DisputeReason.NOT_RECEIVED,   # Missing items
    "ITM02": DisputeReason.NOT_RECEIVED,   # Item not received
    "ITM03": DisputeReason.OTHER,          # Quantity issue
    "ITM04": DisputeReason.WRONG_ITEM,     # Item mismatch
    "ITM05": DisputeReason.DAMAGED,        # Quality issue
}


def _map_sub_category_to_reason(sub_category: str) -> DisputeReason:
    return _SUB_TO_REASON.get(sub_category, DisputeReason.OTHER)


async def open_issue(
    db: AsyncSession,
    *,
    order_id: str,
    profile_id: str,
    category: str,
    sub_category: str,
    description: str,
    refund_amount: int | None = None,
    complainant_name: str | None = None,
    complainant_email: str | None = None,
    complainant_phone: str | None = None,
    send: Any | None = None,
) -> Dispute:
    """Open an ONDC IGM Issue against a local Order.

    Args:
        db: async session; caller commits.
        order_id: local Order id (UUID string).
        profile_id: the buyer's BAP profile id (becomes ``opened_by``).
        category: IGM category, must be in :data:`ISSUE_CATEGORIES`.
        sub_category: IGM sub-category. For category=ITEM must be in
            :data:`ISSUE_SUB_CATEGORIES_ITEM`.
        description: free-text Issue body.
        refund_amount: optional refund expectation in minor units (IDR
            cents).
        complainant_name / email / phone: optional contact metadata
            included in the envelope.
        send: dependency-injection hook for the outbound caller. Defaults
            to ``beckn.outbound.send_beckn_request``. Tests substitute a
            local async callable so we don't try to hit the wire.

    Returns:
        The persisted ``Dispute`` row (with ``bpp_issue_id`` populated).

    Raises:
        OrderNotFoundError: order_id unknown.
        OrderNotEligibleError: order is in a non-eligible state.
        ValueError: invalid IGM category / sub_category.
    """
    if category not in ISSUE_CATEGORIES:
        raise ValueError(
            f"unknown IGM category {category!r}; "
            f"allowed: {sorted(ISSUE_CATEGORIES)}"
        )
    if category == "ITEM" and sub_category not in ISSUE_SUB_CATEGORIES_ITEM:
        raise ValueError(
            f"unknown IGM sub_category {sub_category!r} for category=ITEM; "
            f"allowed: {sorted(ISSUE_SUB_CATEGORIES_ITEM)}"
        )

    order = await db.get(Order, order_id)
    if order is None:
        raise OrderNotFoundError(f"order {order_id} not found")

    if order.state not in _ELIGIBLE_STATES:
        raise OrderNotEligibleError(
            f"order {order_id} in state {order.state.value} is not "
            f"IGM-eligible (allowed: "
            f"{sorted(s.value for s in _ELIGIBLE_STATES)})"
        )

    # Build the envelope first so we have a stable issue id BEFORE we
    # persist the Dispute row (ensures correlation works even if the
    # outbound POST fails mid-flight and we have to retry from the
    # Dispute row).
    bpp_id = order.bpp_id or os.environ.get(
        "DEFAULT_BPP_ID", "bpp.jaringan-dagang.id"
    )
    bpp_uri = os.environ.get(
        "DEFAULT_BPP_URL", "http://localhost:8001/beckn"
    )
    envelope = build_issue_envelope(
        bap_id=settings.subscriber_id,
        bap_uri=settings.subscriber_url,
        bpp_id=bpp_id,
        bpp_uri=bpp_uri,
        transaction_id=str(order.id),
        complainant_id=profile_id,
        complainant_name=complainant_name,
        complainant_email=complainant_email,
        complainant_phone=complainant_phone,
        category=category,
        sub_category=sub_category,
        description=description,
        order_id=order.seller_order_ref or str(order.id),
        refund_amount=refund_amount,
        country_code=settings.country_code,
        city_code=settings.city_code,
        core_version=settings.core_version,
    )
    issue_id = envelope["message"]["issue"]["id"]

    dispute = Dispute(
        order_id=order.id,
        opened_by=f"buyer:{profile_id}",
        reason=_map_sub_category_to_reason(sub_category),
        note=description,
        status=DisputeStatus.OPEN,
        bpp_issue_id=issue_id,
    )
    db.add(dispute)
    await db.flush()

    # Best-effort outbound send. Failure here MUST NOT roll back the
    # Dispute row — ops can retry the send out-of-band, and the buyer's
    # UX shouldn't 500 because the BPP is briefly unreachable.
    if send is None:
        try:
            from beckn.outbound import send_beckn_request as _default_send
        except Exception:
            logger.exception("beckn.outbound import failed; skipping /issue send")
            return dispute
        send = _default_send

    try:
        await send(
            bpp_id=bpp_id, action="issue", body=envelope,
            target_url=f"{bpp_uri.rstrip('/')}/issue",
        )
    except Exception:
        logger.exception(
            "beckn /issue send failed for dispute %s (issue_id=%s); will "
            "remain locally as bpp_issue_id-marked Dispute for retry",
            dispute.id,
            issue_id,
        )

    return dispute
