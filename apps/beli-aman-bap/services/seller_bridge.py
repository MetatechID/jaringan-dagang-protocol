"""Best-effort POST to the seller's BPP when an order moves to ESCROW_HELD.

Failure here is non-fatal: the BAP demo still works without the seller seeing
the order. The seller dashboard piece of the demo just won't show new rows.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from config import settings

logger = logging.getLogger(__name__)

# Strong refs to in-flight fire-and-forget tasks so the event loop's weak
# reference doesn't let them be garbage-collected mid-flight.
_BG_TASKS: set[asyncio.Task] = set()


async def post_order(*, order_dict: dict[str, Any], order_id: str = "") -> bool:
    """POST the order snapshot to the seller's internal escrow-orders endpoint."""
    if not settings.seller_bridge_enabled:
        logger.debug("Seller bridge disabled — skipping POST")
        return False

    url = f"{settings.seller_bridge_url.rstrip('/')}/api/internal/escrow-orders"
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Token": settings.seller_bridge_token,
    }

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(url, json=order_dict, headers=headers)
        if 200 <= resp.status_code < 300:
            logger.info(
                "seller-bridge POST ok %s -> %s (order_id=%s)",
                url, resp.status_code, order_id,
            )
            return True
        logger.warning(
            "seller-bridge POST non-2xx %s -> %s %s (order_id=%s)",
            url, resp.status_code, resp.text[:200], order_id,
        )
        return False
    except Exception as e:
        logger.warning(
            "seller-bridge POST exception %s (order_id=%s): %s",
            url, order_id, e,
        )
        return False


async def patch_escrow_status(*, order_id: str, escrow_status: str) -> bool:
    """PATCH the seller's escrow-status for one order (non-fatal).

    Used to sync post-creation state transitions (currently ESCROW_RELEASED)
    from the BAP into the seller-bpp's ``orders.escrow_status`` column. The
    seller dashboard reads this column directly; without the PATCH the badge
    stays stuck at ``held`` after the buyer releases.

    The seller-bpp mounts the internal router under ``/api`` (see
    ``app/main.py``: ``include_router(escrow_orders_router, prefix="/api")``),
    so the full path is ``/api/internal/escrow-orders/{id}`` — same prefix as
    :func:`post_order`. Omitting ``/api`` here 404s and the release sync is
    silently lost (the failure is non-fatal by design).
    """
    if not settings.seller_bridge_enabled:
        logger.debug("Seller bridge disabled — skipping PATCH")
        return False

    url = f"{settings.seller_bridge_url.rstrip('/')}/api/internal/escrow-orders/{order_id}"
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Token": settings.seller_bridge_token,
    }

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.patch(
                url,
                json={"escrow_status": escrow_status},
                headers=headers,
            )
        if 200 <= resp.status_code < 300:
            logger.info(
                "seller-bridge PATCH ok %s -> %s (order_id=%s, status=%s)",
                url, resp.status_code, order_id, escrow_status,
            )
            return True
        logger.warning(
            "seller-bridge PATCH non-2xx %s -> %s %s (order_id=%s, status=%s)",
            url, resp.status_code, resp.text[:200], order_id, escrow_status,
        )
        return False
    except Exception as e:
        logger.warning(
            "seller-bridge PATCH exception %s (order_id=%s, status=%s): %s",
            url, order_id, escrow_status, e,
        )
        return False


def notify_cart_paid(cart: Any) -> None:
    """Fire-and-forget: tell the seller-bpp a bot/Bella cart was actually paid.

    Bot carts (jd-sell MCP checkout) send Beckn /confirm to the seller BEFORE
    the shopper pays, under ``cart.order_id`` (a pseudo order id). The seller
    awards OITO loyalty points for that order only once it sees
    ``escrow_status="held"``. Call this exactly on the cart's transition to
    ``payment_state="paid"`` (not on webhook replays).

    Scheduled as a background task so a slow/unreachable seller can't delay
    the provider ACK; never raises into the caller.
    """
    order_id = getattr(cart, "order_id", None)
    if not order_id:
        return
    try:
        task = asyncio.create_task(_notify_cart_paid_bg(order_id))
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
    except Exception as e:  # noqa: BLE001 — must never break the webhook
        logger.warning("seller-bridge cart-paid schedule failed (order_id=%s): %s", order_id, e)


async def _notify_cart_paid_bg(order_id: str) -> None:
    try:
        # Looked up on the module at call time so tests can monkeypatch it.
        await patch_escrow_status(order_id=order_id, escrow_status="held")
    except Exception as e:  # noqa: BLE001 — background, non-fatal
        logger.warning("seller-bridge cart-paid PATCH failed (order_id=%s): %s", order_id, e)
