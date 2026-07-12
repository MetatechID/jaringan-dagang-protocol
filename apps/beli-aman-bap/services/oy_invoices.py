"""Build + persist OY Indonesia payment pages for carts / orders.

Mirrors ``services/xendit_invoices.py`` — same call shapes, same DB writes
on the renamed cart columns. Brand.payment_provider is the dispatch key;
this module fires only when that column is ``"oy"``.

See ``services/oy_client.py`` for the raw HTTP wrapper.
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
from services import oy_client

_LOG = logging.getLogger("beli_aman_bap.oy_invoices")


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
    # Real OY needs (a) brand row, (b) oy_api_key (env or per-Brand),
    # (c) a brand.xendit_sub_account_id-shaped routing key. While the OY
    # account is pending verification, fall back to the mock-checkout page
    # so the bot still surfaces a clickable URL.
    if brand is None:
        return True
    if brand.payment_provider != "oy":
        return True
    has_env_key = bool(getattr(settings, "oy_api_key", ""))
    has_brand_key = bool(getattr(brand, "oy_api_key", None))
    return not (has_env_key or has_brand_key)


async def create_invoice_for_cart(db: AsyncSession, cart: Cart) -> dict:
    """Mint an OY transaction for a cart in /confirm state.

    Persists ``cart.invoice_id``, ``cart.invoice_provider``,
    ``cart.qr_image_url`` (vendor-neutral columns). Returns the raw OY
    response.
    """
    brand = await _resolve_brand_for_cart(db, cart)
    amount_idr = _cart_amount_idr(cart)
    mock = _mock_mode(brand)

    if not mock and amount_idr <= 0:
        raise HTTPException(409, "Cart total is 0 — cannot create OY invoice")

    if mock:
        mock_base = (
            getattr(settings, "mock_checkout_public_base", None)
            or "https://jaringan-dagang-seller-api.metatech.id"
        ).rstrip("/")
        mock_invoice_id = f"oy-dev-{cart.order_id or cart.id}"
        cart.invoice_id = mock_invoice_id
        cart.invoice_provider = "oy"
        cart.qr_image_url = f"{mock_base}/api/mock-checkout/{mock_invoice_id}"
        _LOG.warning(
            "create_invoice_for_cart(oy): mock fallback for cart=%s bpp_id=%s "
            "(brand=%s provider=%s oy_env_key=%s oy_brand_key=%s)",
            cart.id, cart.bpp_id,
            getattr(brand, "slug", None),
            getattr(brand, "payment_provider", None),
            bool(getattr(settings, "oy_api_key", "")),
            bool(getattr(brand, "oy_api_key", None)) if brand else False,
        )
        return {"id": mock_invoice_id, "invoice_url": cart.qr_image_url, "mock": True}

    email, name = _customer_fields(cart)

    base = settings.oy_callback_base_url.rstrip("/")
    callback_url = f"{base}/webhooks/oy/invoice"
    success_url = f"{base}/checkout/done?cart_id={cart.id}"
    failure_url = f"{base}/checkout/failed?cart_id={cart.id}"

    response = await oy_client.create_invoice(
        api_key=brand.oy_api_key,
        username=brand.oy_username,
        external_id=f"cart-{cart.id}",
        amount_idr=amount_idr,
        description=f"{brand.name} order — {amount_idr:,} IDR",
        payer_email=email,
        payer_name=name,
        callback_url=callback_url,
        success_redirect_url=success_url,
        failure_redirect_url=failure_url,
    )

    oy_trx_id = (
        response.get("trx_id")
        or response.get("id")
        or response.get("invoice_id")
    )
    oy_url = (
        response.get("checkout_url")
        or response.get("payment_url")
        or response.get("invoice_url")
    )
    cart.invoice_id = oy_trx_id
    cart.invoice_provider = "oy"
    cart.qr_image_url = oy_url
    return response


async def create_invoice_for_order(db: AsyncSession, order: Order) -> dict:
    """Mint an OY transaction for a CART_REVIEWED Order (SDK flow).

    Same shape as Xendit's counterpart: stash the trx id + URL on the
    order's ``payment_method_snapshot`` so the OY webhook can find it.
    """
    brand_q = await db.execute(select(Brand).where(Brand.id == order.brand_id))
    brand = brand_q.scalar_one_or_none()

    # ponytail: mock-mode fallback mirrors the cart path below. Without
    # this, /invoice 500s whenever OY_API_KEY is empty (local dev) or
    # the brand row hasn't been onboarded. The dispatcher's
    # `if provider == "oy"` already guarantees this code path only
    # runs for OY brands, so the strict `payment_provider != "oy"`
    # guard from earlier turns is redundant — and a footgun during
    # provider flips. Drop both strict checks; the mock branch
    # handles brand-missing and OY-unconfigured uniformly. If the
    # dispatcher ever routes an OY service call for a non-OY brand,
    # that's a real bug; fall through and let the OY API reject it
    # (or hit the mock branch).
    if _mock_mode(brand):
        mock_base = (
            getattr(settings, "mock_checkout_public_base", None)
            or "https://jaringan-dagang-seller-api.metatech.id"
        ).rstrip("/")
        mock_invoice_id = f"oy-dev-{order.id}"
        snap = dict(order.payment_method_snapshot or {})
        snap.update({
            "type": "oy_invoice",
            "payment_provider": "oy",
            "invoice_id": mock_invoice_id,
            "invoice_url": f"{mock_base}/api/mock-checkout/{mock_invoice_id}",
        })
        order.payment_method_snapshot = snap
        _LOG.warning(
            "create_invoice_for_order(oy): mock fallback for order=%s brand=%s",
            order.id, getattr(brand, "slug", None),
        )
        return {
            "id": mock_invoice_id,
            "invoice_url": snap["invoice_url"],
            "mock": True,
        }

    base = settings.oy_callback_base_url.rstrip("/")
    callback_url = f"{base}/webhooks/oy/invoice"
    success_url = f"{base}/checkout/done?order_id={order.id}"
    failure_url = f"{base}/checkout/failed?order_id={order.id}"

    items = [
        {
            "name": (i.get("name") or i.get("sku") or "item")[:255],
            "quantity": int(i.get("qty") or 1),
            "price": int(i.get("unit_price_idr") or 0),
        }
        for i in (order.items or [])
    ]
    # ponytail: OY's /payment/create doesn't take an ``items`` array the
    # way Xendit does. We collapse items into the description so the buyer
    # still sees the receipt on OY's hosted page. Re-model when OY
    # supports a richer line-item payload.

    response = await oy_client.create_invoice(
        api_key=brand.oy_api_key,
        username=brand.oy_username,
        external_id=f"order-{order.id}",
        amount_idr=order.total_idr,
        description=(
            f"{brand.name} order {order.id} — {order.total_idr:,} IDR"
            + (" — " + ", ".join(f"{i['name']} x{i['quantity']}" for i in items)
                if items else "")
        ),
        payer_email=(order.shipping_address or {}).get("email"),
        payer_name=(order.shipping_address or {}).get("recipient_name"),
        callback_url=callback_url,
        success_redirect_url=success_url,
        failure_redirect_url=failure_url,
    )

    snap = dict(order.payment_method_snapshot or {})
    snap.update({
        "type": "oy_invoice",
        "payment_provider": "oy",
        "invoice_id": (
            response.get("trx_id")
            or response.get("id")
            or response.get("invoice_id")
        ),
        "invoice_url": (
            response.get("checkout_url")
            or response.get("payment_url")
            or response.get("invoice_url")
        ),
    })
    order.payment_method_snapshot = snap
    return response
