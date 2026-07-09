"""Build + persist Xendit hosted invoices for carts / orders.

The Xendit invoice is the buyer's payment surface (QR + VA + e-wallet
+ retail on a single hosted page). Funds settle into the brand's
XenPlatform sub-account balance — Xendit retains custody as the
licensed PJP.

See `services/xendit_client.py` for the raw HTTP wrapper.
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
from services import xendit_client

_LOG = logging.getLogger("beli_aman_bap.xendit_invoices")


async def _resolve_brand_for_cart(db: AsyncSession, cart: Cart) -> Brand | None:
    """Look up the cart's brand via its ``bpp_id``. Returns None if absent — caller
    handles the mock-checkout fallback so we can demo end-to-end while Xendit
    onboarding is pending."""
    result = await db.execute(select(Brand).where(Brand.bpp_id == cart.bpp_id))
    return result.scalars().first()


def _cart_amount_idr(cart: Cart) -> int:
    quote = cart.quote_json or {}
    return int(quote.get("total_idr") or 0)


def _customer_fields(cart: Cart) -> tuple[str | None, str | None]:
    billing = cart.billing_json or {}
    email = billing.get("email") or billing.get("contact_email")
    name = billing.get("name") or billing.get("display_name")
    return email, name


def _items_for_xendit(cart: Cart) -> list[dict[str, Any]] | None:
    """Map cart items into Xendit's optional items array for receipts."""
    raw = cart.items_json or []
    if not raw:
        return None
    out: list[dict[str, Any]] = []
    quote = cart.quote_json or {}
    quote_lines = {l.get("sku_id"): l for l in (quote.get("lines") or [])}
    for it in raw:
        sku = it.get("sku_id") or it.get("sku")
        qty = int(it.get("qty") or 1)
        ql = quote_lines.get(sku, {})
        out.append({
            "name": ql.get("name") or sku or "item",
            "quantity": qty,
            "price": int(ql.get("unit_price_idr") or ql.get("price_idr") or 0),
        })
    return out or None


async def create_invoice_for_cart(db: AsyncSession, cart: Cart) -> dict:
    """Create a Xendit invoice for a cart that has reached /confirm.

    Persists ``cart.invoice_id`` (``invoice_provider="xendit"``) and
    ``cart.qr_image_url`` (the Xendit hosted-page URL) on the passed-in
    Cart instance (caller commits). Returns the raw Xendit response.
    """
    brand = await _resolve_brand_for_cart(db, cart)

    amount_idr = _cart_amount_idr(cart)

    # Mock fallback: real Xendit needs (a) a brand row matched on bpp_id,
    # (b) brand.xendit_sub_account_id, (c) XENDIT_SECRET_KEY in env. While
    # the Xendit account is pending business verification, fall back to the
    # seller's /api/mock-checkout/{id} page so the bot still surfaces a
    # clickable checkout URL and the demo flow works end-to-end. Drop this
    # branch once Xendit verifies and XENDIT_SECRET_KEY is populated.
    mock_mode = (
        brand is None
        or not brand.xendit_sub_account_id
        or not getattr(settings, "xendit_secret_key", "")
    )

    if not mock_mode and amount_idr <= 0:
        raise HTTPException(409, "Cart total is 0 — cannot create invoice")

    if mock_mode:
        mock_base = (
            getattr(settings, "mock_checkout_public_base", None)
            or "https://jaringan-dagang-seller-api.metatech.id"
        ).rstrip("/")
        mock_invoice_id = f"dev-{cart.order_id or cart.id}"
        cart.invoice_id = mock_invoice_id
        cart.invoice_provider = "xendit"
        cart.qr_image_url = f"{mock_base}/api/mock-checkout/{mock_invoice_id}"
        _LOG.warning(
            "create_invoice_for_cart: mock fallback for cart=%s bpp_id=%s "
            "(brand=%s sub_account=%s xendit_key=%s)",
            cart.id, cart.bpp_id,
            getattr(brand, "slug", None),
            getattr(brand, "xendit_sub_account_id", None),
            bool(getattr(settings, "xendit_secret_key", "")),
        )
        return {"id": mock_invoice_id, "invoice_url": cart.qr_image_url, "mock": True}

    email, name = _customer_fields(cart)
    items = _items_for_xendit(cart)

    base = settings.xendit_callback_base_url.rstrip("/")
    success_url = f"{base}/checkout/done?cart_id={cart.id}"
    failure_url = f"{base}/checkout/failed?cart_id={cart.id}"

    response = await xendit_client.create_invoice(
        for_user_id=brand.xendit_sub_account_id,
        external_id=f"cart-{cart.id}",
        amount_idr=amount_idr,
        description=f"{brand.name} order — {amount_idr:,} IDR",
        customer_email=email,
        customer_name=name,
        success_redirect_url=success_url,
        failure_redirect_url=failure_url,
        duration_seconds=settings.xendit_invoice_duration_seconds,
        items=items,
    )

    cart.invoice_id = response.get("id")
    cart.invoice_provider = "xendit"
    cart.qr_image_url = response.get("invoice_url")
    return response


async def create_invoice_for_order(db: AsyncSession, order: Order) -> dict:
    """Create a Xendit invoice for a CART_REVIEWED Order (SDK flow).

    Stores the invoice id on the OrderEvent timeline (caller persists) and
    returns the raw Xendit response. Unlike the cart path, the SDK polls
    ``GET /orders/{id}`` until state flips to ``ESCROW_HELD`` rather than
    binding the invoice id to the order row directly. We tag the
    ``payment_method_snapshot`` with the invoice id + URL so the webhook
    can find the order from the invoice payload.
    """
    brand_q = await db.execute(select(Brand).where(Brand.id == order.brand_id))
    brand = brand_q.scalar_one_or_none()
    if brand is None:
        raise HTTPException(500, f"Order's brand not found: {order.brand_id}")
    if not brand.xendit_sub_account_id:
        raise HTTPException(
            500,
            f"Brand '{brand.slug}' has no xendit_sub_account_id configured.",
        )

    base = settings.xendit_callback_base_url.rstrip("/")
    success_url = f"{base}/checkout/done?order_id={order.id}"
    failure_url = f"{base}/checkout/failed?order_id={order.id}"

    # Map the order items into the Xendit receipt shape.
    items = [
        {
            "name": (i.get("name") or i.get("sku") or "item")[:255],
            "quantity": int(i.get("qty") or 1),
            "price": int(i.get("unit_price_idr") or 0),
        }
        for i in (order.items or [])
    ]

    response = await xendit_client.create_invoice(
        for_user_id=brand.xendit_sub_account_id,
        external_id=f"order-{order.id}",
        amount_idr=order.total_idr,
        description=f"{brand.name} order — {order.total_idr:,} IDR",
        customer_email=(order.shipping_address or {}).get("email"),
        customer_name=(order.shipping_address or {}).get("recipient_name"),
        success_redirect_url=success_url,
        failure_redirect_url=failure_url,
        duration_seconds=settings.xendit_invoice_duration_seconds,
        items=items,
    )

    # Stash invoice id + url on the order snapshot so the webhook can find it.
    snap = dict(order.payment_method_snapshot or {})
    snap.update({
        "type": "xendit_invoice",
        "payment_provider": "xendit",
        "invoice_id": response.get("id"),
        "invoice_url": response.get("invoice_url"),
    })
    order.payment_method_snapshot = snap
    return response
