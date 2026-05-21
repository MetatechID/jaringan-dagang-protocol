"""Buyer-side Beckn order orchestration (Task A4 part 2).

Implements the spec § 6.1 ``select → init → confirm`` round-trip on top of
the existing ``beckn.outbound.send_beckn_request`` helper and the already-wired
``handle_on_select/on_init/on_confirm`` inbound handlers in
``routers/beckn_handlers.py``.

This module is intentionally a thin orchestrator: it does not write Order
rows itself. Order persistence is still done by ``routers/orders.py`` and
``routers/payments.py``; this module just provides the Beckn calls those
endpoints can make at each transition.

Feature gating
--------------
The ``BECKN_ORDER_FLOW`` env var controls how/whether these calls fire:

  - ``off`` (default): legacy ``services.seller_bridge`` only. Helpers in
    this module are still callable (so contract tests can exercise them)
    but they're NOT used by the deployed payment flow.
  - ``shadow``: at ``/confirm-payment`` time we send both this module's
    ``confirm_order_v2`` AND the legacy ``seller_bridge.post_order``. Bridge
    result is authoritative; Beckn result is logged for diff.
  - ``on``: only Beckn. ``seller_bridge`` is bypassed and the deprecated
    seller-side endpoint at ``app/api/escrow_orders.py`` should respond 410.

Quote-token expectations
------------------------
Per spec § 6.1 the seller's ``/on_init`` returns a 10-min ``quote_token``
that the buyer echoes on ``/confirm``. This module exposes a helper to read
the token off an /on_init response payload; the seller side issues the
token (see seller's ``app/beckn/handlers.py::handle_init``).
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from beckn.outbound import build_ondc_context, send_beckn_request

logger = logging.getLogger(__name__)


# ---------- Flag resolution ----------

_VALID_ORDER_FLOWS = {"off", "shadow", "on"}


def beckn_order_flow_mode() -> str:
    """Return the active ``BECKN_ORDER_FLOW`` value (off|shadow|on).

    Read at every call so a env flip takes effect without restart (matches
    the ``CATALOG_SOURCE`` pattern in ``services/catalog.py``).
    """
    raw = (os.environ.get("BECKN_ORDER_FLOW") or "off").strip().lower()
    if raw not in _VALID_ORDER_FLOWS:
        logger.warning(
            "BECKN_ORDER_FLOW=%r is not one of %s — defaulting to 'off'",
            raw, sorted(_VALID_ORDER_FLOWS),
        )
        return "off"
    return raw


# ---------- Envelope builders ----------


def _bpp_target(bpp_id: str, bpp_uri: str | None) -> tuple[str, str]:
    """Resolve (bpp_id, bpp_uri) — fall back to DEFAULT_BPP_URL if needed."""
    uri = bpp_uri or os.environ.get("DEFAULT_BPP_URL", "http://localhost:8001/beckn")
    return bpp_id, uri


def build_select_envelope(
    *,
    cart_items: list[dict[str, Any]],
    bpp_id: str,
    bpp_uri: str,
    transaction_id: str | None = None,
) -> dict[str, Any]:
    """Build a signed-ready ``/select`` envelope.

    ``cart_items`` is a list of ``{sku_id: str, qty: int}`` dicts (the same
    shape ``routers/orders.py`` already stores on ``Order.items``). We
    translate to Beckn's ``{id, quantity.selected.count}`` shape.
    """
    return {
        "context": build_ondc_context(
            action="select",
            bpp_id=bpp_id,
            bpp_uri=bpp_uri,
            transaction_id=transaction_id or str(uuid.uuid4()),
        ),
        "message": {
            "order": {
                "provider": {"id": bpp_id},
                "items": [
                    {
                        "id": ci.get("sku_id") or ci.get("id"),
                        "quantity": {"selected": {"count": int(ci.get("qty") or 1)}},
                    }
                    for ci in cart_items
                ],
            }
        },
    }


def build_init_envelope(
    *,
    cart_items: list[dict[str, Any]],
    bpp_id: str,
    bpp_uri: str,
    transaction_id: str,
    billing: dict[str, Any],
    shipping_address: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a signed-ready ``/init`` envelope.

    The seller side returns ``/on_init`` with a quote + a ``quote_token``
    (10-min validity) that the buyer must echo at ``/confirm``. Token
    extraction is handled by :func:`extract_quote_token`.
    """
    fulfillments: list[dict[str, Any]] = [
        {
            "type": "Delivery",
            "end": {"location": {"address": shipping_address}} if shipping_address else {},
        }
    ]
    return {
        "context": build_ondc_context(
            action="init",
            bpp_id=bpp_id,
            bpp_uri=bpp_uri,
            transaction_id=transaction_id,
        ),
        "message": {
            "order": {
                "provider": {"id": bpp_id},
                "items": [
                    {
                        "id": ci.get("sku_id") or ci.get("id"),
                        "quantity": {"selected": {"count": int(ci.get("qty") or 1)}},
                    }
                    for ci in cart_items
                ],
                "billing": billing,
                "fulfillments": fulfillments,
            }
        },
    }


def build_confirm_envelope(
    *,
    order_dict: dict[str, Any],
    bpp_id: str,
    bpp_uri: str,
    transaction_id: str,
    quote_token: str | None = None,
) -> dict[str, Any]:
    """Build a signed-ready ``/confirm`` envelope.

    Echoes ``quote_token`` in ``order.tags`` so the seller can verify the
    buyer is paying against the 10-min quote it issued at /init.
    """
    tags: list[dict[str, Any]] = [
        {
            "code": "escrow_status",
            "list": [
                {"code": "value", "value": order_dict.get("escrow_status") or "held"}
            ],
        }
    ]
    if quote_token:
        tags.append(
            {
                "code": "quote_token",
                "list": [{"code": "value", "value": quote_token}],
            }
        )
    return {
        "context": build_ondc_context(
            action="confirm",
            bpp_id=bpp_id,
            bpp_uri=bpp_uri,
            transaction_id=transaction_id,
        ),
        "message": {
            "order": {
                "id": order_dict.get("order_id"),
                "items": order_dict.get("items") or [],
                "billing": (order_dict.get("buyer") or {}),
                "fulfillments": [
                    {
                        "type": "Delivery",
                        "end": {
                            "location": {"address": order_dict.get("shipping_address")}
                        },
                    }
                ],
                "quote": {
                    "price": {
                        "value": str(order_dict.get("total_idr") or 0),
                        "currency": "IDR",
                    },
                },
                "payments": [
                    {
                        "type": "PRE-FULFILLMENT",
                        "status": "PAID",
                        "params": {
                            "amount": str(order_dict.get("total_idr") or 0),
                            "currency": "IDR",
                        },
                    }
                ],
                "tags": tags,
            }
        },
    }


# ---------- Response helpers ----------


def extract_quote_token(on_init_payload: dict[str, Any]) -> str | None:
    """Pull the ``quote_token`` value out of an /on_init response payload.

    The seller emits it inside ``message.order.tags`` as a single-entry list
    with ``code="quote_token"``. Returns ``None`` if absent (older seller
    versions or a non-init payload).
    """
    order = (on_init_payload or {}).get("message", {}).get("order") or {}
    # Sometimes the BAP receives the payload already pre-stripped to
    # ``{"order": {...}}``; tolerate both.
    if not order:
        order = (on_init_payload or {}).get("order") or {}
    for tag in order.get("tags") or []:
        if tag.get("code") == "quote_token":
            for kv in tag.get("list") or []:
                if (kv.get("code") or "").lower() == "value":
                    return kv.get("value")
    return None


def extract_qr_image_url(on_confirm_payload: dict[str, Any]) -> str | None:
    """Pull the QRIS QR image URL out of an /on_confirm response payload.

    Spec § 6.1: the seller attaches the Xendit QRIS QR image URL inside
    ``message.order.payments[].params.qr_image_url`` so the BAP can surface
    it to the buyer. Some payment providers expose ``invoice_url`` instead;
    we accept that shape too. Returns ``None`` if neither is present.
    """
    order = (on_confirm_payload or {}).get("message", {}).get("order") or {}
    if not order:
        order = (on_confirm_payload or {}).get("order") or {}
    for pay in order.get("payments") or []:
        params = pay.get("params") or {}
        url = (
            params.get("qr_image_url")
            or params.get("qr_url")
            or params.get("invoice_url")
        )
        if url:
            return url
    return None


# ---------- Outbound orchestrators ----------


def build_search_envelope(
    *,
    bpp_id: str,
    bpp_uri: str,
    query: str,
    category: str | None = None,
    city: str | None = None,
    transaction_id: str | None = None,
) -> dict[str, Any]:
    """Build a signed-ready ``/search`` envelope.

    The Beckn /search ACK is synchronous; the catalog itself arrives via
    one or more /on_search callbacks. Filtering is hint-only — the BPP
    decides what catalog snapshot to emit.
    """
    intent: dict[str, Any] = {
        "item": {"descriptor": {"name": query}},
    }
    if category:
        intent["category"] = {"descriptor": {"name": category}}
    ctx = build_ondc_context(
        action="search",
        bpp_id=bpp_id,
        bpp_uri=bpp_uri,
        transaction_id=transaction_id or str(uuid.uuid4()),
    )
    if city:
        # build_ondc_context already injects settings.city_code; allow a
        # caller override (the bot may scope to a specific city).
        ctx["city"] = city
    return {
        "context": ctx,
        "message": {"intent": intent},
    }


async def send_search(
    *,
    query: str,
    category: str | None = None,
    city: str | None = None,
    bpp_id: str | None = None,
    bpp_uri: str | None = None,
    transaction_id: str | None = None,
) -> tuple[str, bool]:
    """Fire a signed ``/search`` to a BPP (or the default if unspecified).

    Returns ``(transaction_id, transport_success)``. The actual catalog
    arrives later via /on_search and is handled by
    ``routers.beckn_handlers.handle_on_search`` which upserts mirror_*
    rows. Callers store the transaction_id so they can correlate.
    """
    bpp_id_resolved = bpp_id or os.environ.get(
        "DEFAULT_BPP_ID", "bpp.jaringan-dagang.id"
    )
    bpp_id_resolved, bpp_uri_resolved = _bpp_target(bpp_id_resolved, bpp_uri)
    transaction_id = transaction_id or str(uuid.uuid4())
    env = build_search_envelope(
        bpp_id=bpp_id_resolved,
        bpp_uri=bpp_uri_resolved,
        query=query,
        category=category,
        city=city,
        transaction_id=transaction_id,
    )
    target = f"{bpp_uri_resolved.rstrip('/')}/search"
    try:
        ok = await send_beckn_request(
            bpp_id=bpp_id_resolved, action="search", body=env, target_url=target,
        )
    except Exception:
        logger.exception("beckn /search to %s failed", target)
        ok = False
    return transaction_id, ok


async def select_cart(
    *,
    cart_items: list[dict[str, Any]],
    bpp_id: str,
    bpp_uri: str | None = None,
    transaction_id: str | None = None,
) -> bool:
    """Fire a signed ``/select`` to the seller.

    The seller responds via ``/on_select`` (handled by
    ``routers/beckn_handlers.py::handle_on_select``). This call returns the
    boolean transport success; the async response shows up in the mirror
    + the inbound log.
    """
    bpp_id, bpp_uri = _bpp_target(bpp_id, bpp_uri)
    env = build_select_envelope(
        cart_items=cart_items,
        bpp_id=bpp_id,
        bpp_uri=bpp_uri,
        transaction_id=transaction_id,
    )
    target = f"{bpp_uri.rstrip('/')}/select"
    try:
        return await send_beckn_request(
            bpp_id=bpp_id, action="select", body=env, target_url=target,
        )
    except Exception:
        logger.exception("beckn /select to %s failed", target)
        return False


async def init_order(
    *,
    cart_items: list[dict[str, Any]],
    bpp_id: str,
    bpp_uri: str | None = None,
    transaction_id: str,
    billing: dict[str, Any],
    shipping_address: dict[str, Any] | None = None,
) -> bool:
    """Fire a signed ``/init`` to the seller.

    The seller's ``/on_init`` callback carries the 10-min ``quote_token``;
    the BAP's existing inbound flow records it via the audit log + (in a
    follow-up that ties it to the Order row) will surface it back here.
    For now this function just sends and reports transport success.
    """
    bpp_id, bpp_uri = _bpp_target(bpp_id, bpp_uri)
    env = build_init_envelope(
        cart_items=cart_items,
        bpp_id=bpp_id,
        bpp_uri=bpp_uri,
        transaction_id=transaction_id,
        billing=billing,
        shipping_address=shipping_address,
    )
    target = f"{bpp_uri.rstrip('/')}/init"
    try:
        return await send_beckn_request(
            bpp_id=bpp_id, action="init", body=env, target_url=target,
        )
    except Exception:
        logger.exception("beckn /init to %s failed", target)
        return False


async def confirm_order_v2(
    *,
    order_dict: dict[str, Any],
    quote_token: str | None = None,
) -> bool:
    """Fire a signed ``/confirm`` to the seller, echoing the quote_token.

    Replaces ``services.beckn_orders.confirm_order`` once the feature flag is
    ``on``. While ``BECKN_ORDER_FLOW=shadow`` we call both. Returns transport
    success only; the seller's response is handled async via the inbound
    ``handle_on_confirm`` and the inbound audit log.
    """
    bpp_id = order_dict.get("bpp_id") or os.environ.get(
        "DEFAULT_BPP_ID", "bpp.jaringan-dagang.id"
    )
    bpp_uri = os.environ.get("DEFAULT_BPP_URL", "http://localhost:8001/beckn")
    transaction_id = str(order_dict.get("transaction_id") or order_dict.get("order_id") or uuid.uuid4())

    env = build_confirm_envelope(
        order_dict=order_dict,
        bpp_id=bpp_id,
        bpp_uri=bpp_uri,
        transaction_id=transaction_id,
        quote_token=quote_token,
    )
    target = f"{bpp_uri.rstrip('/')}/confirm"
    try:
        return await send_beckn_request(
            bpp_id=bpp_id, action="confirm", body=env, target_url=target,
        )
    except Exception:
        logger.exception("beckn /confirm to %s failed", target)
        return False
