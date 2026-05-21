"""Bot-facing REST: ``/api/v1/checkout/*`` (Task B3a).

  POST /api/v1/checkout/{cart_id}/confirm — Beckn /confirm, mints an Order
  GET  /api/v1/checkout/{cart_id}/status  — payment / order state

All endpoints require ``Authorization: Bearer <BOT_API_TOKEN>``.

The /confirm endpoint reuses ``services.order_flow.confirm_order_v2`` — the
exact same call site that ``routers/payments.py::confirm_payment`` uses. We
do NOT re-implement the envelope; we shape the bot's cart into the
``order_dict`` shape ``confirm_order_v2`` expects and let it sign + POST.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.bot_auth import require_bot
from config import settings
from database import get_db
from models.bot_rest import Cart, CartStatus
from services import order_flow
from services import xendit_invoices

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/checkout", tags=["bot-rest"])


# ---------- Schemas ----------


class ConfirmIn(BaseModel):
    quote_token: str | None = None
    payment_proof: dict[str, Any] | None = None


class ConfirmPaymentBlock(BaseModel):
    qr_image_url: str | None = None
    invoice_url: str | None = None
    expires_at: str | None = None


class ConfirmOut(BaseModel):
    cart_id: str
    order_id: str | None
    status: str
    payment: ConfirmPaymentBlock


class StatusOut(BaseModel):
    cart_id: str
    order_id: str | None
    payment_state: str
    status: str
    # Surface the payment-page URL so the bot's payment_status tool
    # can show it. Populated either by the seller's /on_confirm
    # callback (when delivery works) or by the post-confirm
    # backchannel poll below (Vercel/network workaround).
    qr_image_url: str | None = None
    invoice_url: str | None = None


# ---------- Endpoints ----------


@router.post(
    "/{cart_id}/confirm",
    dependencies=[Depends(require_bot)],
    response_model=ConfirmOut,
)
async def confirm_cart(
    cart_id: str,
    body: ConfirmIn,
    db: AsyncSession = Depends(get_db),
) -> ConfirmOut:
    """Send Beckn /confirm; extract QR/invoice from the seller's response."""
    cart = (
        await db.execute(select(Cart).where(Cart.id == cart_id))
    ).scalar_one_or_none()
    if cart is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cart not found")
    if cart.status == CartStatus.CONFIRMED:
        # Idempotent — return the existing handle.
        return ConfirmOut(
            cart_id=cart.id,
            order_id=cart.order_id,
            status=cart.status.value,
            payment=ConfirmPaymentBlock(qr_image_url=cart.qr_image_url),
        )
    if cart.status not in (CartStatus.DRAFTED, CartStatus.QUOTED, CartStatus.OPEN):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cart in state {cart.status.value} — cannot /confirm",
        )

    quote_token = body.quote_token or cart.quote_token
    pseudo_order_id = str(uuid.uuid4())

    order_dict = {
        "order_id": pseudo_order_id,
        "bap_id": settings.subscriber_id,
        "bpp_id": cart.bpp_id,
        "transaction_id": cart.transaction_id,
        "buyer": cart.billing_json or {},
        "items": cart.items_json or [],
        "shipping_address": cart.shipping_json,
        "total_idr": (
            int((cart.quote_json or {}).get("total_idr", 0))
            if cart.quote_json
            else 0
        ),
        "escrow_status": "held",
    }

    try:
        await order_flow.confirm_order_v2(
            order_dict=order_dict, quote_token=quote_token
        )
    except Exception:
        logger.exception("confirm_order_v2 failed for cart %s", cart.id)

    cart.status = CartStatus.CONFIRMED
    cart.order_id = pseudo_order_id
    cart.payment_state = "pending"

    # Mint a real Xendit hosted invoice routed to the brand's XenPlatform
    # sub-account (via the ``for-user-id`` header). The invoice URL is the
    # canonical payment surface — works as both a QR-bearing checkout page
    # and a direct-pay link. Funds land in the brand's Xendit balance, not
    # ours. The matching ``invoice.paid`` webhook flips the cart and order
    # to paid/ESCROW_HELD.
    try:
        await xendit_invoices.create_invoice_for_cart(db, cart)
    except HTTPException:
        raise
    except Exception:
        logger.exception("xendit invoice creation failed for cart %s", cart.id)
        raise HTTPException(502, "Could not create Xendit invoice — see logs")

    return ConfirmOut(
        cart_id=cart.id,
        order_id=cart.order_id,
        status=cart.status.value,
        payment=ConfirmPaymentBlock(
            qr_image_url=cart.qr_image_url,
        ),
    )


@router.get(
    "/{cart_id}/status",
    dependencies=[Depends(require_bot)],
    response_model=StatusOut,
)
async def checkout_status(
    cart_id: str,
    db: AsyncSession = Depends(get_db),
) -> StatusOut:
    cart = (
        await db.execute(select(Cart).where(Cart.id == cart_id))
    ).scalar_one_or_none()
    if cart is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cart not found")

    expires_at = (
        cart.expires_at.replace(tzinfo=timezone.utc)
        if cart.expires_at and cart.expires_at.tzinfo is None
        else cart.expires_at
    )
    if (
        expires_at is not None
        and expires_at <= datetime.now(timezone.utc)
        and cart.status != CartStatus.CONFIRMED
    ):
        cart.status = CartStatus.EXPIRED
        if cart.payment_state == "pending":
            cart.payment_state = "expired"

    # Xendit's ``invoice.paid`` webhook (routers/webhooks_xendit.py) is the
    # authoritative signal that flips cart.payment_state to "paid". No
    # lazy-backfill here — if the webhook hasn't fired yet the bot just
    # sees payment_state="pending" and polls again.

    return StatusOut(
        cart_id=cart.id,
        order_id=cart.order_id,
        payment_state=cart.payment_state,
        status=cart.status.value,
        qr_image_url=cart.qr_image_url,
        invoice_url=cart.qr_image_url,  # same URL; bot may render either
    )
