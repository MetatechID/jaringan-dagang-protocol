"""Best-effort POST to the seller's BPP when an order moves to ESCROW_HELD.

Failure here is non-fatal: the BAP demo still works without the seller seeing
the order. The seller dashboard piece of the demo just won't show new rows.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from config import settings

logger = logging.getLogger(__name__)


async def post_order(*, order_dict: dict[str, Any]) -> bool:
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
            logger.info("seller-bridge POST ok %s -> %s", url, resp.status_code)
            return True
        logger.warning(
            "seller-bridge POST non-2xx %s -> %s %s",
            url, resp.status_code, resp.text[:200],
        )
        return False
    except Exception as e:
        logger.warning("seller-bridge POST exception %s: %s", url, e)
        return False
