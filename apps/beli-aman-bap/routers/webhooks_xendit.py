"""Xendit webhook receivers.

Two endpoints:
- POST /webhooks/xendit/invoice      — invoice.paid / invoice.expired
- POST /webhooks/xendit/disbursement — disbursement.completed / failed

Auth: Xendit signs callbacks with a static token set in
``Dashboard → Settings → Callbacks → Callback verification token``. We
compare it against ``settings.xendit_webhook_token``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import get_db
from models.bot_rest import Cart, CartStatus
from models.escrow_ledger import EscrowEntryStatus, EscrowEntryType, EscrowLedger
from models.order import Order
from services import order_paid

_LOG = logging.getLogger("beli_aman_bap.webhooks_xendit")

router = APIRouter(prefix="/webhooks/xendit", tags=["webhooks"])


def _verify_callback_token(x_callback_token: str | None) -> None:
    expected = settings.xendit_webhook_token
    if not expected:
        # Mis-configured BAP — refuse rather than silently accept anything.
        raise HTTPException(503, "XENDIT_WEBHOOK_TOKEN not configured")
    if x_callback_token != expected:
        raise HTTPException(401, "Invalid Xendit callback token")


@router.post("/invoice")
async def invoice_callback(
    request: Request,
    x_callback_token: str | None = Header(default=None, alias="x-callback-token"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Handle Xendit invoice status callbacks.

    Payload shape (PAID):
        {
          "id": "<xendit invoice id>",
          "external_id": "cart-<uuid>" | "order-<uuid>",
          "status": "PAID",
          "paid_amount": <int>,
          "paid_at": "<ISO8601>",
          ...
        }
    EXPIRED is the same shape with status="EXPIRED".
    """
    _verify_callback_token(x_callback_token)
    body = await request.json()
    invoice_id = body.get("id")
    external_id = body.get("external_id") or ""
    status_raw = (body.get("status") or "").upper()

    if not invoice_id or not external_id:
        raise HTTPException(400, "Missing id or external_id in callback")

    _LOG.info(
        "Xendit invoice callback: id=%s external_id=%s status=%s",
        invoice_id, external_id, status_raw,
    )

    if status_raw == "PAID":
        return await _handle_paid(db, invoice_id, external_id)
    if status_raw == "EXPIRED":
        return await _handle_expired(db, invoice_id, external_id)
    # Any other status (e.g. PENDING) is informational — ignore.
    return {"ok": True, "ignored_status": status_raw}


async def _handle_paid(db: AsyncSession, invoice_id: str, external_id: str) -> dict:
    if external_id.startswith("order-"):
        order_id = external_id[len("order-"):]
        order = await order_paid.mark_order_paid(
            db, order_id=order_id, invoice_id=invoice_id,
        )
        return {"ok": True, "order_id": order.id, "state": order.state.value}

    if external_id.startswith("cart-"):
        cart_id = external_id[len("cart-"):]
        cart = (
            await db.execute(select(Cart).where(Cart.id == cart_id))
        ).scalar_one_or_none()
        if cart is None:
            raise HTTPException(404, f"No cart for external_id={external_id}")
        if cart.payment_state != "paid":
            cart.payment_state = "paid"
        if cart.xendit_invoice_id is None:
            cart.xendit_invoice_id = invoice_id
        if cart.status != CartStatus.CONFIRMED:
            cart.status = CartStatus.CONFIRMED
        # Bot flow's Order (if materialized) lives only on the seller's BPP;
        # mark_order_paid is a no-op for bot carts. We still surface a HOLD
        # ledger row keyed on the cart's synthetic order_id for audit.
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
                    description=f"Bot-cart funds held — xendit invoice {invoice_id}",
                    external_ref=invoice_id,
                    status=EscrowEntryStatus.COMPLETED,
                ))
        return {"ok": True, "cart_id": cart.id, "payment_state": cart.payment_state}

    raise HTTPException(400, f"Unrecognized external_id prefix: {external_id!r}")


async def _handle_expired(db: AsyncSession, invoice_id: str, external_id: str) -> dict:
    if external_id.startswith("cart-"):
        cart_id = external_id[len("cart-"):]
        cart = (
            await db.execute(select(Cart).where(Cart.id == cart_id))
        ).scalar_one_or_none()
        if cart and cart.payment_state == "pending":
            cart.payment_state = "expired"
            cart.status = CartStatus.EXPIRED
            return {"ok": True, "cart_id": cart.id, "payment_state": cart.payment_state}
    # For SDK-keyed orders we leave the state alone — the order timeline
    # can show "invoice expired, mint a new one" via /orders/{id}/invoice.
    return {"ok": True, "expired_external_id": external_id}


@router.post("/disbursement")
async def disbursement_callback(
    request: Request,
    x_callback_token: str | None = Header(default=None, alias="x-callback-token"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Flip the RELEASE ledger entry's status to COMPLETED or FAILED."""
    _verify_callback_token(x_callback_token)
    body = await request.json()
    disbursement_id = body.get("id")
    external_id = body.get("external_id") or ""
    status_raw = (body.get("status") or "").upper()

    if not disbursement_id:
        raise HTTPException(400, "Missing disbursement id in callback")

    _LOG.info(
        "Xendit disbursement callback: id=%s external_id=%s status=%s",
        disbursement_id, external_id, status_raw,
    )

    result = await db.execute(
        select(EscrowLedger).where(
            EscrowLedger.external_ref == disbursement_id,
            EscrowLedger.entry_type == EscrowEntryType.RELEASE,
        )
    )
    entry = result.scalars().first()
    if entry is None:
        # No matching ledger row — surface via 200 OK so Xendit doesn't retry
        # forever, but log loudly for ops.
        _LOG.error(
            "No RELEASE ledger entry for disbursement %s (external_id=%s)",
            disbursement_id, external_id,
        )
        return {"ok": True, "matched": False}

    if status_raw == "COMPLETED":
        entry.status = EscrowEntryStatus.COMPLETED
    elif status_raw == "FAILED":
        entry.status = EscrowEntryStatus.FAILED

    return {"ok": True, "order_id": entry.order_id, "status": entry.status.value}
