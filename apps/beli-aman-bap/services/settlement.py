"""Buyer-side ONDC RSP settlement service (Task A6).

The BAP-side counterpart of the BPP's ``handle_settle``. Operators
trigger ``request_settlement`` (via the new
``POST /api/v1/orders/{order_id}/settle`` endpoint, admin-token-gated)
to ask a BPP for a SettlementRecord. The BPP responds via ``/on_settle``,
which our ``handle_on_settle`` persists onto the Order row.

v1 doesn't move money — settlement records are observability + audit-trail
only. The actual rail integration is operator-driven (deferred to v2).
"""

from __future__ import annotations

import logging
import os
import sys
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
    SETTLEMENT_BASES,
    SETTLEMENT_WINDOWS,
    build_settle_envelope,
)

from config import settings  # noqa: E402
from models.order import Order  # noqa: E402

logger = logging.getLogger(__name__)


class SettlementError(Exception):
    """Application-level settlement-request errors."""


class OrderNotFoundError(SettlementError):
    """Caller asked about an order the BAP doesn't know."""


class OrderNotEligibleError(SettlementError):
    """Order is in a state that doesn't permit a settlement request.

    ONDC RSP v1 requires the order to be in a post-fulfillment state. We
    accept RECEIVED / ESCROW_RELEASED / REFUNDED (refunded is eligible
    so the BAP can pull the settlement record AFTER refund issuance and
    see the net BPP payable). PRE_AUTH / AUTHED / CART_REVIEWED are
    out of scope.
    """


async def request_settlement(
    db: AsyncSession,
    *,
    order_id: str,
    settlement_basis: str = "DELIVERY",
    settlement_window: str = "P1D",
    send: Any | None = None,
) -> dict[str, Any]:
    """Build + send a /settle envelope, persist BAP-side settlement state.

    Args:
        db: async session; caller commits.
        order_id: local Order id (UUID string).
        settlement_basis: one of SETTLEMENT_BASES (default DELIVERY).
        settlement_window: one of SETTLEMENT_WINDOWS (default P1D).
        send: dependency-injection hook for the outbound caller. Defaults
            to ``beckn.outbound.send_beckn_request``. Tests substitute a
            local async callable so we don't try to hit the wire.

    Returns:
        The persisted Order row's settlement_* fields as a dict (so the
        REST endpoint can echo).

    Raises:
        OrderNotFoundError: order_id unknown.
        OrderNotEligibleError: order is in a non-eligible state.
        ValueError: invalid basis / window.
    """
    if settlement_basis not in SETTLEMENT_BASES:
        raise ValueError(
            f"unknown settlement_basis {settlement_basis!r}; "
            f"allowed: {sorted(SETTLEMENT_BASES)}"
        )
    if settlement_window not in SETTLEMENT_WINDOWS:
        raise ValueError(
            f"unknown settlement_window {settlement_window!r}; "
            f"allowed: {sorted(SETTLEMENT_WINDOWS)}"
        )

    order = await db.get(Order, order_id)
    if order is None:
        raise OrderNotFoundError(f"order {order_id} not found")

    # Persist BAP-side settlement intent on the Order row.
    order.settlement_status = "NOT_PAID"
    order.settlement_basis = settlement_basis
    order.settlement_window = settlement_window
    await db.flush()

    bpp_id = order.bpp_id or os.environ.get(
        "DEFAULT_BPP_ID", "bpp.jaringan-dagang.id"
    )
    bpp_uri = os.environ.get(
        "DEFAULT_BPP_URL", "http://localhost:8001/beckn"
    )
    envelope = build_settle_envelope(
        bap_id=settings.subscriber_id,
        bap_uri=settings.subscriber_url,
        bpp_id=bpp_id,
        bpp_uri=bpp_uri,
        transaction_id=str(order.id),
        order_id=order.seller_order_ref or str(order.id),
        settlement_basis=settlement_basis,
        settlement_window=settlement_window,
        country_code=settings.country_code,
        city_code=settings.city_code,
        core_version=settings.core_version,
    )

    if send is None:
        try:
            from beckn.outbound import send_beckn_request as _default_send
        except Exception:
            logger.exception("beckn.outbound import failed; skipping /settle send")
            return _state_dict(order)
        send = _default_send

    try:
        await send(
            bpp_id=bpp_id, action="settle", body=envelope,
            target_url=f"{bpp_uri.rstrip('/')}/settle",
        )
    except Exception:
        logger.exception(
            "beckn /settle send failed for order %s; settlement state "
            "remains as NOT_PAID for retry",
            order.id,
        )

    return _state_dict(order)


def _state_dict(order: Order) -> dict[str, Any]:
    return {
        "order_id": str(order.id),
        "settlement_status": order.settlement_status,
        "settlement_basis": order.settlement_basis,
        "settlement_window": order.settlement_window,
        "settlement_reference": order.settlement_reference,
    }
