"""Bot-facing REST: ``/api/v1/cart/*`` (Task B3a).

The bot-side analogue of the storefront's checkout flow:

  POST /api/v1/cart/select         — Beckn /select, creates a Cart row
  GET  /api/v1/cart/{cart_id}      — current cart + quote
  POST /api/v1/cart/{cart_id}/init — Beckn /init, attaches billing/shipping
  GET  /api/v1/cart/{cart_id}/order-draft — assembled draft for /confirm

All endpoints require ``Authorization: Bearer <BOT_API_TOKEN>``. Firebase
auth is NOT used here.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.bot_auth import require_bot
from database import get_db
from models.bot_rest import Cart, CartStatus, SearchSession
from models.mirror import MirrorProduct, MirrorSKU, MirrorSKUImage
from services import order_flow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cart", tags=["bot-rest"])


# ---------- Schemas ----------


class CartItemIn(BaseModel):
    item_id: str = Field(min_length=1)
    qty: int = Field(ge=1)


class CartSelectIn(BaseModel):
    session_id: str | None = None
    bpp_id: str = Field(min_length=1)
    bpp_uri: str | None = None
    provider_id: str | None = None
    items: list[CartItemIn] = Field(min_length=1)


class CartSelectOut(BaseModel):
    cart_id: str
    transaction_id: str
    status: str


class AddressIn(BaseModel):
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    line1: str | None = None
    line2: str | None = None
    city: str | None = None
    postal_code: str | None = None
    country: str | None = None


class CartInitIn(BaseModel):
    billing: AddressIn
    shipping: AddressIn


class CartInitOut(BaseModel):
    cart_id: str
    status: str
    quote_token: str | None = None


class CartOut(BaseModel):
    cart_id: str
    status: str
    bpp_id: str
    bpp_uri: str | None
    provider_id: str | None
    transaction_id: str
    items: list[dict[str, Any]]
    quote: dict[str, Any] | None
    quote_token: str | None
    billing: dict[str, Any] | None
    shipping: dict[str, Any] | None


# ---------- Helpers ----------


def _now_utc_aware(value: datetime | None) -> datetime | None:
    """Coerce a possibly-naive datetime to UTC-aware (sqlite returns naive)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def _load_cart_row(db: AsyncSession, cart_id: str) -> Cart:
    """Read the cart row + reconcile expiry status; never raises 410.

    The original ``_load_cart`` raised HTTP 410 on expiry, which was the
    right call for state-altering endpoints (init / confirm / cart_add)
    but broke the GET path: the rendered cart-receipt PNG and the bot's
    ``cart_view`` tool would suddenly serve an empty receipt the moment
    a cart aged past its TTL, even though the items_json snapshot was
    still in the row. Split the helper into read-tolerant and write-
    strict variants. This is the read-tolerant one — used by GET endpoints
    that should always reflect the snapshot.
    """
    cart = (
        await db.execute(select(Cart).where(Cart.id == cart_id))
    ).scalar_one_or_none()
    if cart is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cart not found")
    expires_at = _now_utc_aware(cart.expires_at) or datetime.now(timezone.utc)
    if expires_at <= datetime.now(timezone.utc) and cart.status not in (
        CartStatus.CONFIRMED,
        CartStatus.EXPIRED,
    ):
        cart.status = CartStatus.EXPIRED
    return cart


async def _load_cart(db: AsyncSession, cart_id: str) -> Cart:
    """Read the cart row; raises 410 if expired (write-strict).

    Use this on state-altering endpoints (cart_add, /init, /confirm) so
    the bot can't mutate a stale cart. GET endpoints should use
    ``_load_cart_row`` instead.
    """
    cart = await _load_cart_row(db, cart_id)
    if cart.status == CartStatus.EXPIRED:
        raise HTTPException(status.HTTP_410_GONE, "Cart expired")
    return cart


def _serialize_cart(cart: Cart) -> CartOut:
    return CartOut(
        cart_id=cart.id,
        status=cart.status.value,
        bpp_id=cart.bpp_id,
        bpp_uri=cart.bpp_uri,
        provider_id=cart.provider_id,
        transaction_id=cart.transaction_id,
        items=cart.items_json or [],
        quote=cart.quote_json,
        quote_token=cart.quote_token,
        billing=cart.billing_json,
        shipping=cart.shipping_json,
    )


async def _enrich_cart_items(
    db: AsyncSession,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Join cart items with mirror_skus / mirror_products so the bot's
    cart_view + the rendered PNG receipt can show product name, price,
    and a thumbnail URL — even before /on_select fills cart.quote_json.

    The bot can identify an item via several shapes:
      - bpp_sku_id UUID (the canonical id returned by /search/results)
      - sku_code (the merchant-friendly slug like "saf-suk-500")
      - product_id  (less common, but the model sometimes passes it)
    We look up MirrorSKU by any of these and merge.

    Returns a new list; never mutates the input. Items without a
    resolvable sku are passed through unmodified.
    """
    if not items:
        return []
    raw_ids = [str(it.get("sku_id")) for it in items if it.get("sku_id")]
    if not raw_ids:
        return list(items)

    # Match against bpp_sku_id OR sku_code so we cover both:
    #  - real flow via /search/results (returns bpp_sku_id UUID)
    #  - model shortcuts (sku_code like "saf-suk-500" — friendlier)
    # Plus fallback to MirrorProduct.bpp_product_id (model often passes
    # the product UUID instead of the SKU UUID; in that case we pick
    # the first / cheapest SKU of the product as the default variant).
    skus = (
        await db.execute(
            select(MirrorSKU).where(
                or_(
                    MirrorSKU.bpp_sku_id.in_(raw_ids),
                    MirrorSKU.sku_code.in_(raw_ids),
                )
            )
        )
    ).scalars().all()
    sku_lookup: dict[str, MirrorSKU] = {}
    for s in skus:
        if s.bpp_sku_id:
            sku_lookup[s.bpp_sku_id] = s
        if s.sku_code:
            sku_lookup[s.sku_code] = s

    # Product-id fallback: for any raw_id we still haven't resolved, try
    # MirrorProduct.bpp_product_id; if the product has SKUs, pick the
    # cheapest one as the default variant.
    unresolved = [r for r in raw_ids if r not in sku_lookup]
    if unresolved:
        matched_products = (
            await db.execute(
                select(MirrorProduct).where(
                    MirrorProduct.bpp_product_id.in_(unresolved)
                )
            )
        ).scalars().all()
        if matched_products:
            prod_skus = (
                await db.execute(
                    select(MirrorSKU)
                    .where(MirrorSKU.product_id.in_([p.id for p in matched_products]))
                    .order_by(MirrorSKU.price)
                )
            ).scalars().all()
            sku_by_product: dict[str, MirrorSKU] = {}
            for s in prod_skus:
                sku_by_product.setdefault(s.product_id, s)
            for p in matched_products:
                sk = sku_by_product.get(p.id)
                if sk:
                    sku_lookup[p.bpp_product_id] = sk
                    skus.append(sk)

    prod_ids = list({s.product_id for s in skus})
    products = (
        await db.execute(
            select(MirrorProduct).where(MirrorProduct.id.in_(prod_ids))
        )
    ).scalars().all() if prod_ids else []
    prod_by_id = {p.id: p for p in products}

    sku_pk_ids = [s.id for s in skus]
    sku_imgs = (
        await db.execute(
            select(MirrorSKUImage).where(MirrorSKUImage.sku_id.in_(sku_pk_ids))
        )
    ).scalars().all() if sku_pk_ids else []
    img_by_sku_pk: dict[str, str] = {}
    for im in sorted(sku_imgs, key=lambda i: i.position):
        img_by_sku_pk.setdefault(im.sku_id, im.url)

    out: list[dict[str, Any]] = []
    for it in items:
        sid = str(it.get("sku_id") or "")
        sku = sku_lookup.get(sid)
        if sku is None:
            out.append(dict(it))
            continue
        prod = prod_by_id.get(sku.product_id)
        qty = int(it.get("qty") or 1)
        price = int(sku.price) if sku.price is not None else None
        enriched = dict(it)
        enriched["name"] = (
            f"{prod.name} ({sku.variant_value})"
            if prod and sku.variant_value
            else (prod.name if prod else sid)
        )
        if price is not None:
            enriched["price_idr"] = price
            enriched["line_total_idr"] = price * qty
        if img_by_sku_pk.get(sku.id):
            enriched["image_url"] = img_by_sku_pk[sku.id]
        out.append(enriched)
    return out


# Flat shipping fee for v1. Will become per-route via Biteship later.
_SHIPPING_FEE_IDR = 15000


def _build_quote(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Compute a fallback quote (subtotal + shipping + total) when the
    seller's /on_select hasn't filled cart.quote_json yet. Customer sees
    a real total + ongkir line on the receipt PNG."""
    subtotal = sum(int(it.get("line_total_idr") or 0) for it in items)
    if subtotal <= 0:
        return None
    return {
        "subtotal_idr": subtotal,
        "shipping_idr": _SHIPPING_FEE_IDR,
        "total_idr": subtotal + _SHIPPING_FEE_IDR,
    }


async def _serialize_cart_enriched(
    db: AsyncSession, cart: Cart,
) -> CartOut:
    """Like _serialize_cart but enriches items + computes a fallback
    quote (subtotal + ongkir + total) when cart.quote_json is None."""
    items = await _enrich_cart_items(db, cart.items_json or [])
    quote = cart.quote_json
    if quote is None:
        fallback = _build_quote(items)
        if fallback is not None:
            quote = fallback
    return CartOut(
        cart_id=cart.id,
        status=cart.status.value,
        bpp_id=cart.bpp_id,
        bpp_uri=cart.bpp_uri,
        provider_id=cart.provider_id,
        transaction_id=cart.transaction_id,
        items=items,
        quote=quote,
        quote_token=cart.quote_token,
        billing=cart.billing_json,
        shipping=cart.shipping_json,
    )


# ---------- Endpoints ----------


@router.post(
    "/select",
    dependencies=[Depends(require_bot)],
    response_model=CartSelectOut,
)
async def cart_select(
    body: CartSelectIn,
    db: AsyncSession = Depends(get_db),
) -> CartSelectOut:
    """Create a Cart and fire Beckn /select to validate items + pricing."""
    transaction_id: str
    if body.session_id:
        session = (
            await db.execute(
                select(SearchSession).where(SearchSession.id == body.session_id)
            )
        ).scalar_one_or_none()
        if session is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "Search session not found"
            )
        transaction_id = session.transaction_id
    else:
        transaction_id = str(uuid.uuid4())

    cart_items = [{"sku_id": it.item_id, "qty": it.qty} for it in body.items]
    cart = Cart(
        search_session_id=body.session_id,
        bpp_id=body.bpp_id,
        bpp_uri=body.bpp_uri,
        provider_id=body.provider_id,
        items_json=cart_items,
        transaction_id=transaction_id,
        status=CartStatus.OPEN,
    )
    db.add(cart)
    await db.flush()

    try:
        await order_flow.select_cart(
            cart_items=cart_items,
            bpp_id=body.bpp_id,
            bpp_uri=body.bpp_uri,
            transaction_id=transaction_id,
        )
    except Exception:
        logger.exception("select_cart failed for cart %s", cart.id)

    return CartSelectOut(
        cart_id=cart.id,
        transaction_id=cart.transaction_id,
        status=cart.status.value,
    )


class CartItemsAddIn(BaseModel):
    items: list[CartItemIn] = Field(min_length=1)


@router.post(
    "/{cart_id}/items",
    dependencies=[Depends(require_bot)],
    response_model=CartOut,
)
async def cart_add_items(
    cart_id: str,
    body: CartItemsAddIn,
    db: AsyncSession = Depends(get_db),
) -> CartOut:
    """Merge new items into an existing cart.

    Beckn semantics: ``/select`` is re-fired with the **full updated
    item list** under the **same ``transaction_id``** so the seller's
    BPP returns a fresh quote via ``/on_select``. We mutate the existing
    cart row (stable ``cart_id``) instead of opening a fresh one.

    Only OPEN carts can be mutated: once ``/init`` has been fired
    (DRAFTED) the buyer has already provided billing/shipping and a
    quote was issued; mutating items at that point requires a fresh
    pre-init decision flow, which the bot is expected to drive
    explicitly. CONFIRMED and EXPIRED are terminal/invalid.
    """
    cart = await _load_cart(db, cart_id)
    if cart.status != CartStatus.OPEN:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cart in state {cart.status.value} — cannot merge items",
        )

    # Merge by sku_id (sum qty). Preserve insertion order: existing items
    # first, then any new sku_ids the buyer just asked for.
    merged: dict[str, int] = {}
    order: list[str] = []
    for it in (cart.items_json or []):
        sid = str(it.get("sku_id"))
        if not sid:
            continue
        merged[sid] = merged.get(sid, 0) + int(it.get("qty") or 0)
        if sid not in order:
            order.append(sid)
    for it in body.items:
        sid = it.item_id
        merged[sid] = merged.get(sid, 0) + it.qty
        if sid not in order:
            order.append(sid)

    cart_items = [{"sku_id": sid, "qty": merged[sid]} for sid in order]
    cart.items_json = cart_items
    # Stale quote: a new /on_select round-trip will refill these. We keep
    # quote_json=None (the GET path's fallback recomputes a subtotal from
    # items so the receipt PNG stays usable) and explicitly clear
    # quote_token so the bot can tell the buyer "menunggu konfirmasi
    # harga dari toko" if it ever surfaces token state.
    cart.quote_json = None
    cart.quote_token = None

    try:
        await order_flow.select_cart(
            cart_items=cart_items,
            bpp_id=cart.bpp_id,
            bpp_uri=cart.bpp_uri,
            transaction_id=cart.transaction_id,
        )
    except Exception:
        logger.exception("select_cart (merge) failed for cart %s", cart.id)

    await db.flush()
    return await _serialize_cart_enriched(db, cart)


@router.get(
    "/{cart_id}",
    dependencies=[Depends(require_bot)],
    response_model=CartOut,
)
async def get_cart(
    cart_id: str,
    db: AsyncSession = Depends(get_db),
) -> CartOut:
    # Read-tolerant: return the cart even when expired so the rendered
    # receipt PNG keeps showing the snapshot. Status will be "expired"
    # in the response; the bot UI is expected to surface that.
    cart = await _load_cart_row(db, cart_id)
    return await _serialize_cart_enriched(db, cart)


@router.post(
    "/{cart_id}/init",
    dependencies=[Depends(require_bot)],
    response_model=CartInitOut,
)
async def cart_init(
    cart_id: str,
    body: CartInitIn,
    db: AsyncSession = Depends(get_db),
) -> CartInitOut:
    """Persist billing/shipping; fire Beckn /init."""
    cart = await _load_cart(db, cart_id)
    if cart.status in (CartStatus.CONFIRMED, CartStatus.EXPIRED):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cart in state {cart.status.value} — cannot /init",
        )

    cart.billing_json = body.billing.model_dump(exclude_none=True)
    cart.shipping_json = body.shipping.model_dump(exclude_none=True)
    cart.status = CartStatus.DRAFTED

    try:
        await order_flow.init_order(
            cart_items=cart.items_json or [],
            bpp_id=cart.bpp_id,
            bpp_uri=cart.bpp_uri,
            transaction_id=cart.transaction_id,
            billing=cart.billing_json,
            shipping_address=cart.shipping_json,
        )
    except Exception:
        logger.exception("init_order failed for cart %s", cart.id)

    return CartInitOut(
        cart_id=cart.id,
        status=cart.status.value,
        quote_token=cart.quote_token,
    )


@router.get(
    "/{cart_id}/order-draft",
    dependencies=[Depends(require_bot)],
)
async def get_order_draft(
    cart_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return the assembled order draft the bot would send to /confirm.

    Read-tolerant: even an expired cart should be observable so the bot
    can decide whether to re-create. Mutators (/init, cart_add) still
    enforce 410.
    """
    cart = await _load_cart_row(db, cart_id)
    quote = cart.quote_json or {}
    return {
        "cart_id": cart.id,
        "status": cart.status.value,
        "bpp_id": cart.bpp_id,
        "bpp_uri": cart.bpp_uri,
        "provider_id": cart.provider_id,
        "transaction_id": cart.transaction_id,
        "items": cart.items_json or [],
        "billing": cart.billing_json,
        "shipping": cart.shipping_json,
        "quote": quote,
        "quote_token": cart.quote_token,
    }
