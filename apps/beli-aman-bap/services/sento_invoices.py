"""Build + persist Sento payment pages for carts / orders.

Mirrors ``services/oy_invoices.py`` — same call shapes, same DB writes
on the renamed cart columns. Brand.payment_provider is the dispatch key;
this module fires only when that column is ``"sento"``.

See ``services/sento_client.py`` for the raw HTTP wrapper.

Sento Payment Link API contract used here:
- ``partner_tx_id`` → maps to our external_id (``cart-{id}`` /
  ``order-{id}``); echoed back as ``partner_trx_id`` in callbacks.
- Response carries ``payment_link_id`` (Sento-side id we stash as
  ``invoice_id``), ``url`` (the hosted checkout page surfaced to the
  buyer as ``invoice_url``), ``expiration``, and optionally ``qr_url``
  in future Sento releases. ``trx_id`` + ``payment_info.qris_url`` belong
  to Payment Routing and are NOT used here.
- We normalize at the boundary so downstream consumers still see
  ``id`` + ``invoice_url`` (matching Xendit / OY counterparts).

Docs: https://api-docs.sento.id/docs-page/payment-link
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
from services import sento_client

_LOG = logging.getLogger("beli_aman_bap.sento_invoices")


def _normalize_response(response: dict, partner_tx_id: str) -> dict:
    """Flatten Payment Link's response into the vendor-neutral shape the
    rest of the BAP expects: ``id`` + ``invoice_url`` + optional
    ``qris_image_url`` + optional ``expires_at``.
    """
    return {
        "id": response.get("payment_link_id") or partner_tx_id,
        "invoice_url": response.get("url"),
        # Payment Link doesn't expose qris_url directly in create-v2
        # response (the hosted page renders the QR). Surface top-level
        # qr_url if a future Sento release adds it.
        "qris_image_url": response.get("qr_url"),
        "expires_at": response.get("expiration"),
    }


async def _resolve_brand_for_cart(db: AsyncSession, cart: Cart) -> Brand | None:
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


def _mock_mode(brand: Brand | None) -> bool:
    """Real Sento needs (a) brand row, (b) provider=='sento', (c) sento_api_key
    (env or per-Brand). Until all three, fall back to the mock-checkout page.
    """
    if brand is None:
        return True
    if brand.payment_provider != "sento":
        return True
    has_env_key = bool(getattr(settings, "sento_api_key", ""))
    has_brand_key = bool(getattr(brand, "sento_api_key", None))
    return not (has_env_key or has_brand_key)


async def create_invoice_for_cart(db: AsyncSession, cart: Cart) -> dict:
    """Mint a Sento Payment Link transaction for a cart in /confirm state.

    Persists ``cart.invoice_id``, ``cart.invoice_provider``,
    ``cart.qr_image_url``, and the new ``cart.qris_image_url`` (vendor-neutral
    columns). Returns the normalized response.
    """
    brand = await _resolve_brand_for_cart(db, cart)
    amount_idr = _cart_amount_idr(cart)
    mock = _mock_mode(brand)

    if not mock and amount_idr <= 0:
        raise HTTPException(409, "Cart total is 0 — cannot create Sento invoice")

    if mock:
        mock_base = (
            getattr(settings, "mock_checkout_public_base", None)
            or "https://jaringan-dagang-seller-api.metatech.id"
        ).rstrip("/")
        mock_invoice_id = f"sento-dev-{cart.order_id or cart.id}"
        cart.invoice_id = mock_invoice_id
        cart.invoice_provider = "sento"
        cart.qr_image_url = f"{mock_base}/api/mock-checkout/{mock_invoice_id}"
        _LOG.warning(
            "create_invoice_for_cart(sento): mock fallback for cart=%s bpp_id=%s "
            "(brand=%s provider=%s sento_env_key=%s sento_brand_key=%s)",
            cart.id, cart.bpp_id,
            getattr(brand, "slug", None),
            getattr(brand, "payment_provider", None),
            bool(getattr(settings, "sento_api_key", "")),
            bool(getattr(brand, "sento_api_key", None)) if brand else False,
        )
        return {"id": mock_invoice_id, "invoice_url": cart.qr_image_url, "mock": True}

    email, name = _customer_fields(cart)
    partner_tx_id = f"cart-{cart.id}"

    response = await sento_client.create_invoice(
        api_key=brand.sento_api_key,
        username=brand.sento_username,
        partner_tx_id=partner_tx_id,
        amount_idr=amount_idr,
        sender_name=name or "Buyer",
        email=email,
        description=f"{brand.name} order — {amount_idr:,} IDR",
    )

    norm = _normalize_response(response, partner_tx_id)
    cart.invoice_id = partner_tx_id  # Sento echoes this back in the callback
    cart.invoice_provider = "sento"
    cart.qr_image_url = norm["invoice_url"]
    cart.qris_image_url = norm.get("qris_image_url")
    return norm


async def create_invoice_for_order(db: AsyncSession, order: Order) -> dict:
    """Mint a Sento Payment Link transaction for a CART_REVIEWED Order (SDK flow).

    Stashes the result on the order's ``payment_method_snapshot`` so the Sento
    webhook can recover it. Returns the normalized response.
    """
    brand_q = await db.execute(select(Brand).where(Brand.id == order.brand_id))
    brand = brand_q.scalar_one_or_none()

    # ponytail: same shape as the OY order path — drop the strict provider
    # / brand-missing guards so the dispatcher isn't a footgun during
    # provider flips. Mock-mode covers brand-missing and Sento-unconfigured
    # uniformly.
    if _mock_mode(brand):
        mock_base = (
            getattr(settings, "mock_checkout_public_base", None)
            or "https://jaringan-dagang-seller-api.metatech.id"
        ).rstrip("/")
        mock_invoice_id = f"sento-dev-{order.id}"
        snap = dict(order.payment_method_snapshot or {})
        snap.update({
            "type": "sento_invoice",
            "payment_provider": "sento",
            "invoice_id": mock_invoice_id,
            "invoice_url": f"{mock_base}/api/mock-checkout/{mock_invoice_id}",
        })
        order.payment_method_snapshot = snap
        _LOG.warning(
            "create_invoice_for_order(sento): mock fallback for order=%s brand=%s",
            order.id, getattr(brand, "slug", None),
        )
        return {
            "id": mock_invoice_id,
            "invoice_url": snap["invoice_url"],
            "mock": True,
        }

    partner_tx_id = f"order-{order.id}"
    items = [
        {
            "name": (i.get("name") or i.get("sku") or "item")[:255],
            "quantity": int(i.get("qty") or 1),
            "price": int(i.get("unit_price_idr") or 0),
        }
        for i in (order.items or [])
    ]
    # ponytail: Payment Link doesn't take an ``items`` array. Fold line
    # items into the description so the buyer still sees them on the
    # hosted page. Re-model when Sento adds line-item support.
    description = (
        f"{brand.name} order {order.id} — {order.total_idr:,} IDR"
        + (" — " + ", ".join(f"{i['name']} x{i['quantity']}" for i in items)
            if items else "")
    )
    email = (order.shipping_address or {}).get("email")
    sender_name = (order.shipping_address or {}).get("recipient_name") or "Buyer"

    response = await sento_client.create_invoice(
        api_key=brand.sento_api_key,
        username=brand.sento_username,
        partner_tx_id=partner_tx_id,
        amount_idr=order.total_idr,
        sender_name=sender_name,
        email=email,
        description=description,
    )

    norm = _normalize_response(response, partner_tx_id)
    snap = dict(order.payment_method_snapshot or {})
    snap.update({
        "type": "sento_invoice",
        "payment_provider": "sento",
        # partner_tx_id is the lookup key Sento echoes back in the
        # callback body — it MUST be stored verbatim on the snapshot
        # so the receiver can resolve the brand. The Sento-side
        # payment_link_id (norm["id"]) is stored separately as
        # ``sento_payment_link_id`` so we don't lose it.
        "partner_tx_id": partner_tx_id,
        "sento_payment_link_id": norm["id"],
        "invoice_id": norm["id"],
        "invoice_url": norm["invoice_url"],
    })
    if norm.get("qris_image_url"):
        snap["qris_image_url"] = norm["qris_image_url"]
    if norm.get("expires_at"):
        snap["expires_at"] = norm["expires_at"]
    order.payment_method_snapshot = snap
    return norm
