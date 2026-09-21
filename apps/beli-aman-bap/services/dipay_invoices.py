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

import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.bot_rest import Cart
from models.brand import Brand
from models.order import Order
from services import dipay_client
from services.dipay_client import snap_ref

_LOG = logging.getLogger("beli_aman_bap.dipay_invoices")


def _normalize_response(response: dict, partner_ref: str) -> dict:
    """Flatten QRIS MPM generate's response into the vendor-neutral shape
    the rest of the BAP expects: ``id`` + ``invoice_url`` +
    ``qris_image_url`` + ``qris_content``.
    """
    qr_content = response.get("qrContent") or response.get("qrString")
    png_url = (
        f"{settings.qr_public_base.rstrip('/')}/api/v1/qris/{partner_ref}.png"
    )
    return {
        "id": partner_ref,
        "invoice_url": png_url,
        "qris_image_url": png_url,
        "qris_content": qr_content,
        "expires_at": None,
    }


async def _resolve_brand_for_cart(db: AsyncSession, cart: Cart) -> Brand | None:
    result = await db.execute(select(Brand).where(Brand.bpp_id == cart.bpp_id))
    return result.scalars().first()


def _cart_amount_idr(cart: Cart) -> int:
    quote = cart.quote_json or {}
    return int(quote.get("total_idr") or 0)


def _mock_mode(brand: Brand | None) -> bool:
    """Real Dipay needs (a) brand row, (b) provider=='dipay', (c)
    dipay_client_key (env or per-Brand). Until all three, fall back to the
    mock-checkout page.
    """
    if brand is None:
        return True
    if brand.payment_provider != "dipay":
        return True
    has_env_key = bool(getattr(settings, "dipay_client_key", ""))
    has_brand_key = bool(getattr(brand, "dipay_client_key", None))
    return not (has_env_key or has_brand_key)


def _qris_png_url(partner_ref: str) -> str:
    return f"{settings.qr_public_base.rstrip('/')}/api/v1/qris/{partner_ref}.png"


async def create_invoice_for_cart(db: AsyncSession, cart: Cart) -> dict:
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
    mock = _mock_mode(brand)

    if not mock and amount_idr <= 0:
        raise HTTPException(409, "Cart total is 0 — cannot create Dipay invoice")

    if mock:
        mock_base = (
            getattr(settings, "mock_checkout_public_base", None)
            or "https://jaringan-dagang-seller-api.metatech.id"
        ).rstrip("/")
        mock_invoice_id = f"dipay-dev-{cart.order_id or cart.id}"
        cart.invoice_id = mock_invoice_id
        cart.invoice_provider = "dipay"
        cart.qr_image_url = f"{mock_base}/api/mock-checkout/{mock_invoice_id}"
        _LOG.warning(
            "create_invoice_for_cart(dipay): mock fallback for cart=%s bpp_id=%s "
            "(brand=%s provider=%s dipay_env_key=%s dipay_brand_key=%s)",
            cart.id, cart.bpp_id,
            getattr(brand, "slug", None),
            getattr(brand, "payment_provider", None),
            bool(getattr(settings, "dipay_client_key", "")),
            bool(getattr(brand, "dipay_client_key", None)) if brand else False,
        )
        return {"id": mock_invoice_id, "invoice_url": cart.qr_image_url, "mock": True}

    partner_ref = snap_ref("q", str(cart.id))
    # Persist the ref BEFORE the API call (race window): a concurrent
    # double-confirm hits the same partnerReferenceNo instead of minting a
    # second payable QR.
    cart.invoice_id = partner_ref
    cart.invoice_provider = "dipay"

    response = await dipay_client.create_qris(
        partner_reference_no=partner_ref,
        amount_idr=amount_idr,
        merchant_id=getattr(brand, "dipay_merchant_id", None) or None,
    )

    norm = _normalize_response(response, partner_ref)
    cart.qr_image_url = norm["invoice_url"]
    cart.qris_image_url = norm["qris_image_url"]
    cart.qris_content = norm.get("qris_content")
    return norm


async def create_invoice_for_order(
    db: AsyncSession, order: Order, *, buyer_email: str | None = None,
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

    # ponytail: same shape as the Sento order path — drop the strict
    # provider / brand-missing guards so the dispatcher isn't a footgun
    # during provider flips. Mock-mode covers brand-missing and
    # Dipay-unconfigured uniformly.
    if _mock_mode(brand):
        mock_base = (
            getattr(settings, "mock_checkout_public_base", None)
            or "https://jaringan-dagang-seller-api.metatech.id"
        ).rstrip("/")
        mock_invoice_id = f"dipay-dev-{order.id}"
        snap = dict(order.payment_method_snapshot or {})
        snap.update({
            "type": "dipay_qris",
            "payment_provider": "dipay",
            "invoice_id": mock_invoice_id,
            "invoice_url": f"{mock_base}/api/mock-checkout/{mock_invoice_id}",
        })
        order.payment_method_snapshot = snap
        _LOG.warning(
            "create_invoice_for_order(dipay): mock fallback for order=%s brand=%s",
            order.id, getattr(brand, "slug", None),
        )
        return {
            "id": mock_invoice_id,
            "invoice_url": snap["invoice_url"],
            "mock": True,
        }

    partner_ref = snap_ref("q", str(order.id))
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
    })
    order.payment_method_snapshot = snap

    response = await dipay_client.create_qris(
        partner_reference_no=partner_ref,
        amount_idr=order.total_idr,
        merchant_id=getattr(brand, "dipay_merchant_id", None) or None,
    )

    qr_content = response.get("qrContent") or response.get("qrString")
    if qr_content:
        snap["qris_content"] = qr_content
    snap["qris_image_url"] = png_url
    order.payment_method_snapshot = snap

    return {
        "id": partner_ref,
        "invoice_url": png_url,
        "qris_image_url": png_url,
        "qris_content": qr_content,
        "expires_at": None,
    }
