"""Buyer-side Beckn order helpers — send /confirm to seller's BPP.

Replaces services/seller_bridge.py which posted to a private REST endpoint.
This routes the same intent through the Beckn protocol (signed + idempotent).
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

# Make the beckn-protocol package importable
_proto_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "packages", "beckn-protocol")
)
if _proto_path not in sys.path:
    sys.path.insert(0, _proto_path)

from beckn.outbound import build_ondc_context, send_beckn_request  # noqa: E402

logger = logging.getLogger(__name__)


async def confirm_order(*, order_dict: dict[str, Any]) -> bool:
    """Send a Beckn /confirm to the seller's BPP.

    `order_dict` matches what seller_bridge.post_order used to send. The
    payload is embedded in the message.order portion so the seller's
    handle_confirm can upsert.
    """
    bpp_id = order_dict.get("bpp_id") or os.environ.get("DEFAULT_BPP_ID", "bpp.jaringan-dagang.id")
    bpp_uri = os.environ.get("DEFAULT_BPP_URL", "http://localhost:8001/beckn")
    target = f"{bpp_uri.rstrip('/')}/confirm"

    env = {
        "context": build_ondc_context(
            action="confirm",
            bpp_id=bpp_id,
            bpp_uri=bpp_uri,
            transaction_id=order_dict.get("transaction_id"),
        ),
        "message": {
            "order": {
                "id": order_dict.get("order_id"),
                "items": order_dict.get("items") or [],
                "billing": (order_dict.get("buyer") or {}),
                "fulfillments": [
                    {
                        "type": "Delivery",
                        "end": {"location": {"address": order_dict.get("shipping_address")}},
                    }
                ],
                "quote": {
                    "price": {"value": str(order_dict.get("total_idr") or 0), "currency": "IDR"},
                },
                "payments": [
                    {
                        "type": "PRE-FULFILLMENT",
                        "status": "PAID",
                        "params": {"amount": str(order_dict.get("total_idr") or 0), "currency": "IDR"},
                    }
                ],
                "tags": [{"code": "escrow_status", "list": [{"code": "value", "value": order_dict.get("escrow_status") or "held"}]}],
            }
        },
    }
    try:
        return await send_beckn_request(
            bpp_id=bpp_id, action="confirm", body=env, target_url=target,
        )
    except Exception:
        logger.exception("beckn /confirm to %s failed", target)
        return False
