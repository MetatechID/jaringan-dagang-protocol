"""Buyer-side ONDC /rating service (Task A6).

Storefront calls ``POST /api/v1/orders/{order_id}/rating`` (auth: same
Firebase profile that owns the order); we validate the rating set, build
+ sign + POST the /rating envelope to the BPP via the existing
``beckn.outbound.send_beckn_request`` plumbing, and persist a local
``OrderRating`` row so the storefront can echo back later (idempotent
on order_id — submitting a second time updates the existing row).

v1 doesn't model the BPP's /on_rating ack beyond logging it (the
``handle_on_rating`` handler updates the OrderRating timestamp). Buyer
storefront UI for capture is deferred to v2.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Make beckn-protocol importable.
_proto_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "packages", "beckn-protocol")
)
if _proto_path not in sys.path:
    sys.path.insert(0, _proto_path)

from beckn_protocol import (  # noqa: E402
    RATING_CATEGORIES,
    build_rating_envelope,
)

from config import settings  # noqa: E402
from models.order import Order, OrderState  # noqa: E402
from models.order_rating import OrderRating  # noqa: E402

logger = logging.getLogger(__name__)


class RatingError(Exception):
    """Application-level rating errors (state-eligibility, validation)."""


class OrderNotFoundError(RatingError):
    """Rating against an order the BAP doesn't know."""


class OrderNotEligibleError(RatingError):
    """Order is in a state that doesn't permit rating.

    ONDC Rating v1 requires the order to have reached at least
    RECEIVED. Earlier states (PRE_AUTH..ESCROW_HELD..FULFILLING) are
    pre-delivery and can't be rated yet.
    """


_ELIGIBLE_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.RECEIVED,
        OrderState.ESCROW_RELEASED,
        OrderState.REFUNDED,
        OrderState.DISPUTED,
    }
)


async def submit_rating(
    db: AsyncSession,
    *,
    order_id: str,
    ratings: list[dict[str, Any]],
    send: Any | None = None,
) -> OrderRating:
    """Submit a buyer-side /rating to the BPP.

    Args:
        db: async session; caller commits.
        order_id: local Order id (UUID string).
        ratings: list of ``{category, value, id?, comments?}`` dicts.
            * ``category`` must be in :data:`RATING_CATEGORIES`.
            * ``value`` must be a numeric string/number in [1.0, 5.0].
            * ``id`` is the id of the rated entity (item_id, provider_id);
              omitted for Order-level ratings.
        send: outbound dep-injection hook for tests.

    Returns:
        The persisted OrderRating row.

    Raises:
        OrderNotFoundError, OrderNotEligibleError, ValueError.
    """
    if not ratings:
        raise ValueError("ratings list is empty")
    for r in ratings:
        cat = r.get("category") or r.get("rating_category")
        val = r.get("value")
        if cat not in RATING_CATEGORIES:
            raise ValueError(
                f"unknown rating category {cat!r}; "
                f"allowed: {sorted(RATING_CATEGORIES)}"
            )
        if val is None:
            raise ValueError("rating value is required")
        try:
            v = float(val)
        except (TypeError, ValueError):
            raise ValueError(f"rating value {val!r} not parseable")
        if not (1.0 <= v <= 5.0):
            raise ValueError(
                f"rating value {v} outside [1.0, 5.0]"
            )

    order = await db.get(Order, order_id)
    if order is None:
        raise OrderNotFoundError(f"order {order_id} not found")
    if order.state not in _ELIGIBLE_STATES:
        raise OrderNotEligibleError(
            f"order {order_id} in state {order.state.value} is not "
            f"rating-eligible (allowed: "
            f"{sorted(s.value for s in _ELIGIBLE_STATES)})"
        )

    # Build the envelope first so we have the wire shape locked before
    # any DB mutation.
    bpp_id = order.bpp_id or os.environ.get(
        "DEFAULT_BPP_ID", "bpp.jaringan-dagang.id"
    )
    bpp_uri = os.environ.get(
        "DEFAULT_BPP_URL", "http://localhost:8001/beckn"
    )
    # Normalize the rating dicts into the envelope-builder shape.
    norm = []
    for r in ratings:
        norm.append({
            "rating_category": r.get("category") or r.get("rating_category"),
            "value": str(r.get("value")),
            "id": r.get("id"),
            "comments": r.get("comments"),
        })
    envelope = build_rating_envelope(
        bap_id=settings.subscriber_id,
        bap_uri=settings.subscriber_url,
        bpp_id=bpp_id,
        bpp_uri=bpp_uri,
        transaction_id=str(order.id),
        order_id=order.seller_order_ref or str(order.id),
        ratings=norm,
        country_code=settings.country_code,
        city_code=settings.city_code,
        core_version=settings.core_version,
    )

    # Idempotent upsert per (order_id) — multi-rating in one call replaces
    # the prior row's JSON payload.
    existing = (await db.execute(
        select(OrderRating).where(OrderRating.order_id == order.id)
    )).scalar_one_or_none()
    if existing is None:
        rating_row = OrderRating(
            order_id=order.id,
            ratings=ratings,
            acknowledged=False,
        )
        db.add(rating_row)
    else:
        existing.ratings = ratings
        existing.acknowledged = False
        rating_row = existing
    await db.flush()

    if send is None:
        try:
            from beckn.outbound import send_beckn_request as _default_send
        except Exception:
            logger.exception("beckn.outbound import failed; skipping /rating send")
            return rating_row
        send = _default_send

    try:
        await send(
            bpp_id=bpp_id, action="rating", body=envelope,
            target_url=f"{bpp_uri.rstrip('/')}/rating",
        )
    except Exception:
        logger.exception(
            "beckn /rating send failed for order %s; OrderRating row "
            "persists locally for /on_rating reconcile",
            order.id,
        )

    return rating_row
