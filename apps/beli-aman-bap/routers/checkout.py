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

    # Mock-mode default: BAP-hosted mock-pay page keyed on the BAP
    # cart_id. Independent of seller state — guarantees the bot has
    # a working payment link to surface even when the Beckn /confirm
    # round-trip drops items or assigns a different beckn_order_id.
    # The backchannel poll below tries to override with the real
    # seller-issued URL when it lands; until then, /api/v1/mock-pay
    # below serves a "Mark paid (sandbox)" button.
    cart.qr_image_url = (
        f"https://api.beli-aman.metatech.id"
        f"/api/v1/mock-pay/{cart.id}"
    )

    # Backchannel poll: the seller's /on_confirm callback to our
    # /api/v1/beckn/on_confirm endpoint isn't reliably delivering on
    # Vercel (cold-start drops the BackgroundTask before it fires,
    # likely). Until that's fixed, query the seller's bot-auth'd
    # payment-lookup endpoint right here so the bot has the
    # Xendit / mock-checkout URL ready by the time it polls
    # /checkout/{cart_id}/status. Short timeout, best-effort.
    try:
        import httpx, os
        seller_base = os.environ.get(
            "DEFAULT_BPP_URL", "https://jaringan-dagang-seller-api.metatech.id/beckn"
        ).rstrip("/")
        # bpp_uri usually points at /beckn; strip that suffix to get the API root.
        api_root = seller_base.removesuffix("/beckn")
        bot_token = os.environ.get("BOT_API_TOKEN")
        if bot_token:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    f"{api_root}/api/orders/by-beckn-id/{pseudo_order_id}/payment",
                    headers={"authorization": f"Bearer {bot_token}"},
                )
                if r.status_code == 200:
                    data = r.json() or {}
                    url = data.get("invoice_url") or data.get("xendit_invoice_url")
                    if url:
                        cart.qr_image_url = url
                        logger.info(
                            "Backchannel payment URL captured for cart %s: %s",
                            cart.id, url,
                        )
    except Exception:
        logger.exception("Backchannel payment-URL poll failed for cart %s", cart.id)

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

    # Lazy backfill from the seller's bot-facing payment endpoint. Two
    # things can drift between BPP and BAP:
    #   1. qr_image_url — if /confirm's backchannel poll missed it (cold
    #      start, deploy lag), the cart has no payment link to show.
    #   2. payment_state — the BPP learns of actual payment via Xendit
    #      webhook; the BAP has no equivalent signal (handle_on_confirm
    #      only fires on the Beckn confirm ACK, not on payment receipt),
    #      so payment_state stays "pending" forever even after the buyer
    #      pays. We poll the seller's /by-beckn-id/{id}/payment endpoint
    #      (which returns the BPP's authoritative payment_status) and
    #      propagate "paid" across. Best-effort; silent on failure.
    needs_url = not cart.qr_image_url
    needs_paid = cart.payment_state == "pending"
    if cart.order_id and cart.status == CartStatus.CONFIRMED and (
        needs_url or needs_paid
    ):
        try:
            import httpx, os
            seller_base = (
                os.environ.get("DEFAULT_BPP_URL")
                or "https://jaringan-dagang-seller-api.metatech.id/beckn"
            ).rstrip("/")
            api_root = seller_base.removesuffix("/beckn")
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.get(
                    f"{api_root}/api/orders/by-beckn-id/{cart.order_id}/payment",
                )
                if r.status_code == 200:
                    data = r.json() or {}
                    url = data.get("invoice_url") or data.get("xendit_invoice_url")
                    if url and not cart.qr_image_url:
                        cart.qr_image_url = url
                        logger.info(
                            "lazy-backfilled payment URL for cart %s", cart.id
                        )
                    seller_pay = (data.get("payment_status") or "").lower()
                    # Never overwrite a terminal state (cancelled/expired).
                    if (
                        seller_pay == "paid"
                        and cart.payment_state not in ("cancelled", "expired", "paid")
                    ):
                        cart.payment_state = "paid"
                        logger.info(
                            "lazy-promoted payment_state=paid for cart %s "
                            "(seller reported paid)", cart.id
                        )
        except Exception:
            logger.exception("Lazy payment backfill failed for cart %s", cart.id)

    return StatusOut(
        cart_id=cart.id,
        order_id=cart.order_id,
        payment_state=cart.payment_state,
        status=cart.status.value,
        qr_image_url=cart.qr_image_url,
        invoice_url=cart.qr_image_url,  # same URL; bot may render either
    )
