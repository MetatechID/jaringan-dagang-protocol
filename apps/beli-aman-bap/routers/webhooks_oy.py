"""OY Indonesia webhook receivers.

Mirrors ``routers/webhooks_xendit.py`` — same /invoice flow, but with OY's
shape:

- Auth: HMAC SHA-256 of the raw request body keyed by the per-brand
  ``oy_callback_secret`` (``x-oy-signature`` header). Brand isn't on the
  path, so we resolve it from the body's invoice id → Cart or Order.
- Body: product-specific. We accept the QRIS / VA / E-Wallet shapes by
  reading whatever id is present (``trx_id`` / ``invoice_id`` /
  ``reference_id``).

State transitions land in the same ``mark_order_paid`` seam as Xendit.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import get_db
from models.bot_rest import Cart, CartStatus
from models.brand import Brand
from models.escrow_ledger import EscrowEntryStatus, EscrowEntryType, EscrowLedger
from services import order_paid

_LOG = logging.getLogger("beli_aman_bap.webhooks_oy")

router = APIRouter(prefix="/webhooks/oy", tags=["webhooks"])


def _verify_signature(
    body_bytes: bytes,
    signature: str | None,
    brand: Brand,
) -> None:
    secret = brand.oy_callback_secret
    if not secret:
        # Mis-configured BAP — refuse rather than silently accept.
        raise HTTPException(503, "Brand has no oy_callback_secret configured")
    if not signature:
        raise HTTPException(401, "Missing OY signature header")
    expected = hmac.new(
        secret.encode("utf-8"),
        body_bytes,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "Invalid OY signature")


def _extract_invoice_id(body: dict) -> str | None:
    """OY's per-product body shape — try the common keys in order."""
    for key in ("trx_id", "invoice_id", "reference_id", "id"):
        v = body.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def _oy_status(body: dict) -> str:
    """Return a normalized status string. OY uses status codes; we map."""
    raw = (
        body.get("status")
        or body.get("transaction_status")
        or body.get("payment_status")
        or ""
    )
    return str(raw).upper().strip()


async def _resolve_brand_for_invoice_id(
    db: AsyncSession, invoice_id: str
) -> Brand | None:
    """Find the brand that owns ``invoice_id`` via cart or order snapshot."""
    # 1. Direct cart lookup: cart.invoice_id + invoice_provider=='oy'.
    cart_q = await db.execute(
        select(Cart).where(
            Cart.invoice_id == invoice_id,
            Cart.invoice_provider == "oy",
        )
    )
    cart = cart_q.scalars().first()
    if cart is not None:
        # Cart stores bpp_id, not brand_id — brand rows key on bpp_id.
        brand_q = await db.execute(
            select(Brand).where(Brand.bpp_id == cart.bpp_id)
        )
        return brand_q.scalars().first()

    # 2. Order snapshot lookup: payment_method_snapshot.invoice_id.
    from models.order import Order

    # JSONB filter — Postgres-side lookup. Fall back to Python-side scan
    # via a small bounded query if the database doesn't index JSON well;
    # orders table is small in v1.
    order_q = await db.execute(
        select(Order).where(Order.payment_method_snapshot["invoice_id"].astext == invoice_id)
    )
    order = order_q.scalars().first()
    if order is not None:
        brand_q = await db.execute(select(Brand).where(Brand.id == order.brand_id))
        return brand_q.scalars().first()

    return None


@router.post("/invoice")
async def invoice_callback(
    request: Request,
    x_oy_signature: str | None = Header(default=None, alias="x-oy-signature"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Receive OY payment status callbacks (PAID / EXPIRED / FAILED).

    Brand isn't on the path; we resolve it from the body's invoice id.
    """
    body_bytes = await request.body()
    body: dict[str, Any] = {}
    if body_bytes:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "Invalid JSON body")

    invoice_id = _extract_invoice_id(body)
    if not invoice_id:
        raise HTTPException(400, "Missing OY invoice id in callback")

    brand = await _resolve_brand_for_invoice_id(db, invoice_id)
    if brand is None:
        _LOG.warning(
            "OY callback: no Brand for invoice_id=%s — refusing to verify",
            invoice_id,
        )
        raise HTTPException(401, "Unknown OY invoice")

    _verify_signature(body_bytes, x_oy_signature, brand)

    status = _oy_status(body)
    _LOG.info(
        "OY invoice callback: brand=%s invoice_id=%s status=%s",
        getattr(brand, "slug", None), invoice_id, status,
    )

    if status in ("PAID", "SUCCESS", "000", "COMPLETED"):
        return await _handle_paid(db, invoice_id, body)
    if status in ("EXPIRED",):
        return await _handle_expired(db, invoice_id, body)
    if status in ("FAILED", "CANCELLED", "EXPIRED_30", "300", "DECLINED"):
        return await _handle_failed(db, invoice_id, status)
    # Other status (PENDING etc.) — acknowledge so OY stops retrying.
    return {"ok": True, "ignored_status": status}


async def _resolve_external_ref(body: dict, invoice_id: str) -> str:
    """OY typically wraps the BAP's own external_id in the body — try to
    recover it before falling back to the invoice_id (which matches the
    Xendit path's external_id-startswith convention)."""
    for key in ("external_id", "merchant_ref", "ref_id"):
        v = body.get(key)
        if isinstance(v, str) and v:
            return v
    return invoice_id


async def _handle_paid(db: AsyncSession, invoice_id: str, body: dict) -> dict:
    external_ref = await _resolve_external_ref(body, invoice_id)
    actor = "system:oy_webhook"

    if external_ref.startswith("order-"):
        order_id = external_ref[len("order-"):]
        order = await order_paid.mark_order_paid(
            db,
            order_id=order_id,
            invoice_id=invoice_id,
            actor=actor,
        )
        return {"ok": True, "order_id": order.id, "state": order.state.value}

    if external_ref.startswith("cart-"):
        cart_id = external_ref[len("cart-"):]
        cart = (
            await db.execute(select(Cart).where(Cart.id == cart_id))
        ).scalar_one_or_none()
        if cart is None:
            raise HTTPException(404, f"No cart for external_ref={external_ref}")
        if cart.payment_state != "paid":
            cart.payment_state = "paid"
        if cart.invoice_id is None:
            cart.invoice_id = invoice_id
        cart.invoice_provider = "oy"
        if cart.status != CartStatus.CONFIRMED:
            cart.status = CartStatus.CONFIRMED
        if cart.order_id:
            existing = await db.execute(
                select(EscrowLedger).where(
                    EscrowLedger.order_id == cart.order_id,
                    EscrowLedger.external_ref == invoice_id,
                )
            )
            if existing.scalars().first() is None:
                amount = int((cart.quote_json or {}).get("total_idr") or 0)
                db.add(EscrowLedger(
                    order_id=cart.order_id,
                    entry_type=EscrowEntryType.HOLD,
                    amount_idr=amount,
                    description=f"Bot-cart funds held — oy trx {invoice_id}",
                    external_ref=invoice_id,
                    status=EscrowEntryStatus.COMPLETED,
                ))
        return {"ok": True, "cart_id": cart.id, "payment_state": cart.payment_state}

    raise HTTPException(400, f"Unrecognized external_ref prefix: {external_ref!r}")


async def _handle_expired(db: AsyncSession, invoice_id: str, body: dict) -> dict:
    external_ref = await _resolve_external_ref(body, invoice_id)
    if external_ref.startswith("cart-"):
        cart_id = external_ref[len("cart-"):]
        cart = (
            await db.execute(select(Cart).where(Cart.id == cart_id))
        ).scalar_one_or_none()
        if cart and cart.payment_state == "pending":
            cart.payment_state = "expired"
            cart.status = CartStatus.EXPIRED
            return {"ok": True, "cart_id": cart.id, "payment_state": cart.payment_state}
    return {"ok": True, "expired_external_ref": external_ref}


async def _handle_failed(db: AsyncSession, invoice_id: str, status: str) -> dict:
    # OY surfaces failure codes Xendit doesn't. Mark the cart.payment_state
    # ``failed`` so the bot doesn't loop on a doomed payment — but don't
    # touch order rows (those flow through mark_order_paid only on PAID).
    cart_q = await db.execute(
        select(Cart).where(
            Cart.invoice_id == invoice_id,
            Cart.invoice_provider == "oy",
        )
    )
    cart = cart_q.scalars().first()
    if cart and cart.payment_state in ("pending", None):
        cart.payment_state = "failed"
        return {"ok": True, "cart_id": cart.id, "payment_state": cart.payment_state}
    return {"ok": True, "invoice_id": invoice_id, "failed_status": status}
