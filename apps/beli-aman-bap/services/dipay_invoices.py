"""Build + persist Dipay QRIS invoices for carts / orders.

Mirrors ``services/sento_invoices.py`` — same call shapes, same DB writes
on the renamed cart columns. Brand.payment_provider is the dispatch key;
this module fires only when that column is ``"dipay"``.

See ``services/dipay_client.py`` for the raw SNAP v2.1 HTTP wrapper.

Dipay QRIS MPM contract used here:
- ``partnerReferenceNo`` → our SNAP ref (``q-{cart|order id}``, ≤32
  chars via ``snap_ref``); echoed back in the payment callback.
- Response carries ``qrContent`` (the EMVCo payload encoded in the QR).
  We render the buyer-facing PNG ourselves at
  ``{qr_public_base}/api/v1/qris/{partnerReferenceNo}.png``
  (see ``routers/qris.py``) instead of depending on a hosted page.
- We normalize at the boundary so downstream consumers still see
  ``id`` + ``invoice_url`` (matching Xendit / OY / Sento counterparts).

Docs: https://api-docs.dipay.id/ (QRIS MPM Generate).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.bot_rest import Cart
from models.brand import Brand
from models.order import Order
from services import dipay_client
from services.dipay_client import DipayConfig, DipayError, snap_ref
from services.release_clock import JAKARTA

_LOG = logging.getLogger("beli_aman_bap.dipay_invoices")
_QRIS_SUCCESS_PREFIX = "20047"


def _mock_allowed() -> bool:
    env = str(getattr(settings, "environment", "test") or "test").lower()
    return env in {"development", "test"}


def _mock_invoice_url(invoice_id: str) -> str:
    if not _mock_allowed():
        raise DipayError(
            0,
            "Dipay mock checkout requires a non-production environment",
        )
    mock_base = (
        getattr(settings, "mock_checkout_public_base", None)
        or "https://jaringan-dagang-seller-api.metatech.id"
    ).rstrip("/")
    token = getattr(settings, "seller_bridge_token", "") or ""
    if token:
        message = f"mock-checkout:{invoice_id}".encode()
        tok = hmac.new(
            token.encode(), message, hashlib.sha256,
        ).hexdigest()
        return f"{mock_base}/api/mock-checkout/{quote(invoice_id)}?token={tok}"
    return f"{mock_base}/api/mock-checkout/{quote(invoice_id)}"


def _validity_period() -> str:
    seconds = getattr(settings, "dipay_qris_duration_seconds", None) or 1800
    exp = datetime.now(JAKARTA) + timedelta(seconds=int(seconds))
    return exp.isoformat(timespec="seconds")


def _validate_qris_response(response: dict) -> str | None:
    code = str(response.get("responseCode") or "")
    message = str(response.get("responseMessage") or "")
    if not code.startswith(_QRIS_SUCCESS_PREFIX):
        raise DipayError(
            0, f"Dipay QRIS generation rejected: {code} {message}".strip(),
        )
    qr_content = response.get("qrContent") or response.get("qrString")
    if not isinstance(qr_content, str) or not qr_content.strip():
        return None
    return qr_content.strip()


def _normalize_response(
    response: dict, partner_ref: str, validity_period: str | None = None,
) -> dict:
    """Flatten and validate QRIS generate into the vendor-neutral shape."""
    qr_content = _validate_qris_response(response)
    png_url = (
        f"{settings.qr_public_base.rstrip('/')}/api/v1/qris/{partner_ref}.png"
    )
    return {
        "id": partner_ref,
        "invoice_url": png_url,
        "qris_image_url": png_url,
        "qris_content": qr_content,
        "expires_at": validity_period,
    }


async def _resolve_brand_for_cart(db: AsyncSession, cart: Cart) -> Brand | None:
    result = await db.execute(select(Brand).where(Brand.bpp_id == cart.bpp_id))
    return result.scalars().first()


def _cart_amount_idr(cart: Cart) -> int:
    quote = cart.quote_json or {}
    return int(quote.get("total_idr") or 0)


def _resolved_config(brand: Brand | None) -> DipayConfig | None:
    """Return real credentials, or mock only when both bundles are absent."""
    if brand is None or getattr(brand, "payment_provider", None) != "dipay":
        if _mock_allowed():
            return None
        raise DipayError(0, "Dipay invoice requires a Dipay-configured brand")

    brand_key = str(getattr(brand, "dipay_client_key", "") or "").strip()
    env_key = str(getattr(settings, "dipay_client_key", "") or "").strip()
    if not brand_key and not env_key:
        if _mock_allowed():
            return None
        raise DipayError(0, "Dipay credentials not configured")

    if not brand_key and env_key:
        return dipay_client.resolve_config(None)

    return dipay_client.resolve_config(brand)


def _mock_mode(brand: Brand | None) -> bool:
    return _resolved_config(brand) is None


def _qris_png_url(partner_ref: str) -> str:
    return f"{settings.qr_public_base.rstrip('/')}/api/v1/qris/{partner_ref}.png"


async def create_invoice_for_cart(
    db: AsyncSession,
    cart: Cart,
    *,
    reservation_db: AsyncSession | None = None,
) -> dict:
    """Mint a Dipay QRIS invoice for a cart in /confirm state.

    Persists ``cart.invoice_id`` (the SNAP ``partnerReferenceNo``, echoed
    back in Dipay's payment callback), ``cart.invoice_provider``,
    ``cart.qr_image_url`` / ``cart.qris_image_url`` (our PNG renderer URL)
    and ``cart.qris_content`` (the raw EMVCo payload). Returns the
    normalized response.

    The ref is written to the cart BEFORE the API call so a racing
    duplicate /confirm can't mint two QRIS invoices for one cart — the
    second call would reuse the same ``partnerReferenceNo`` and Dipay
    rejects/returns the original.
    """
    brand = await _resolve_brand_for_cart(db, cart)
    amount_idr = _cart_amount_idr(cart)
    config = _resolved_config(brand)

    if config is not None and amount_idr <= 0:
        raise HTTPException(409, "Cart total is 0 — cannot create Dipay invoice")

    if config is None:
        mock_invoice_id = f"dipay-dev-{cart.order_id or cart.id}"
        cart.invoice_id = mock_invoice_id
        cart.invoice_provider = "dipay"
        cart.qr_image_url = _mock_invoice_url(mock_invoice_id)
        _LOG.warning(
            "create_invoice_for_cart(dipay): authorized mock for cart=%s bpp_id=%s",
            cart.id, cart.bpp_id,
        )
        return {"id": mock_invoice_id, "invoice_url": cart.qr_image_url, "mock": True}

    partner_ref = snap_ref("q", str(cart.id))
    cart.invoice_id = partner_ref
    cart.invoice_provider = "dipay"
    # A flush in ``db`` is not visible to Dipay's callback connection. The
    # router supplies a short independent session so only this deterministic
    # reservation commits before network I/O.
    if reservation_db is not None:
        reserved = (
            await reservation_db.execute(
                select(Cart).where(Cart.id == str(cart.id)).with_for_update()
            )
        ).scalar_one_or_none()
        if reserved is None:
            raise HTTPException(404, "Cart not found while reserving Dipay invoice")
        if reserved.invoice_id and reserved.invoice_id != partner_ref:
            raise HTTPException(409, "Cart already has a different invoice")
        reserved.invoice_id = partner_ref
        reserved.invoice_provider = "dipay"
        await reservation_db.commit()
    else:
        # Unit/service callers can inject an already-independent session;
        # request routers always provide ``reservation_db``.
        flush = getattr(db, "flush", None)
        if flush is not None:
            await flush()

    validity_period = _validity_period()
    response = await dipay_client.create_qris(
        config=config,
        partner_reference_no=partner_ref,
        amount_idr=amount_idr,
        validity_period=validity_period,
    )

    norm = _normalize_response(response, partner_ref, validity_period=validity_period)
    cart.qr_image_url = norm["invoice_url"]
    cart.qris_image_url = norm["qris_image_url"]
    cart.qris_content = norm.get("qris_content")
    cart.expires_at = datetime.fromisoformat(validity_period).astimezone(timezone.utc)
    return norm


async def create_invoice_for_order(
    db: AsyncSession,
    order: Order,
    *,
    buyer_email: str | None = None,
    reservation_db: AsyncSession | None = None,
) -> dict:
    """Mint a Dipay QRIS invoice for a CART_REVIEWED Order (SDK flow).

    Stashes the result on the order's ``payment_method_snapshot`` so the
    Dipay webhook can recover it. Returns the normalized response.

    ``buyer_email`` is the authenticated profile's email (from
    ``get_current_profile`` in the router). It takes precedence over any
    ``email`` stashed on ``order.shipping_address`` — same precedence rule
    as the Sento path.
    """
    brand_q = await db.execute(select(Brand).where(Brand.id == order.brand_id))
    brand = brand_q.scalar_one_or_none()

    config = _resolved_config(brand)
    if config is None:
        mock_invoice_id = f"dipay-dev-{order.id}"
        mock_url = _mock_invoice_url(mock_invoice_id)
        snap = dict(order.payment_method_snapshot or {})
        snap.update({
            "type": "dipay_qris",
            "payment_provider": "dipay",
            "invoice_id": mock_invoice_id,
            "invoice_url": mock_url,
        })
        order.payment_method_snapshot = snap
        _LOG.warning(
            "create_invoice_for_order(dipay): authorized mock for order=%s",
            order.id,
        )
        return {"id": mock_invoice_id, "invoice_url": mock_url, "mock": True}

    if order.total_idr <= 0:
        raise HTTPException(409, "Order total is 0 — cannot create Dipay invoice")

    partner_ref = snap_ref("q", str(order.id))
    validity_period = _validity_period()
    png_url = _qris_png_url(partner_ref)
    items = [
        {
            "name": (i.get("name") or i.get("sku") or "item")[:255],
            "quantity": int(i.get("qty") or 1),
            "price": int(i.get("unit_price_idr") or 0),
        }
        for i in (order.items or [])
    ]
    # ponytail: QRIS MPM generate carries no buyer-visible line items or
    # description field in our wrapper. Fold them into the snapshot for
    # webhook / ops parity (mirrors how the Sento path folds items into a
    # description). Re-model if Dipay surfaces a description field.
    description = (
        f"{brand.name} order {order.id} — {order.total_idr:,} IDR"
        + (" — " + ", ".join(f"{i['name']} x{i['quantity']}" for i in items)
            if items else "")
    )
    email = buyer_email or (order.shipping_address or {}).get("email")
    sender_name = (order.shipping_address or {}).get("recipient_name") or "Buyer"

    snap = dict(order.payment_method_snapshot or {})
    snap.update({
        "type": "dipay_qris",
        "payment_provider": "dipay",
        # partner_ref is the lookup key Dipay echoes back in the
        # callback body — it MUST be stored (and the snapshot persisted)
        # BEFORE the API call so the receiver can resolve the order even
        # if we crash mid-create.
        "partner_ref": partner_ref,
        "invoice_id": partner_ref,
        "invoice_url": png_url,
        "description": description,
        "sender_name": sender_name,
        "email": email,
        "expires_at": validity_period,
    })
    order.payment_method_snapshot = snap
    if reservation_db is not None:
        reserved = (
            await reservation_db.execute(
                select(Order).where(Order.id == str(order.id)).with_for_update()
            )
        ).scalar_one_or_none()
        if reserved is None:
            raise HTTPException(404, "Order not found while reserving Dipay invoice")
        current = dict(reserved.payment_method_snapshot or {})
        existing = current.get("invoice_id")
        if existing and existing != partner_ref:
            raise HTTPException(409, "Order already has a different invoice")
        current.update(snap)
        reserved.payment_method_snapshot = current
        await reservation_db.commit()
    else:
        flush = getattr(db, "flush", None)
        if flush is not None:
            await flush()

    response = await dipay_client.create_qris(
        config=config,
        partner_reference_no=partner_ref,
        amount_idr=order.total_idr,
        validity_period=validity_period,
    )

    qr_content = _validate_qris_response(response)
    if qr_content:
        snap["qris_content"] = qr_content
    snap["qris_image_url"] = png_url
    snap["expires_at"] = validity_period
    order.payment_method_snapshot = snap

    return {
        "id": partner_ref,
        "invoice_url": png_url,
        "qris_image_url": png_url,
        "qris_content": qr_content,
        "expires_at": validity_period,
    }
