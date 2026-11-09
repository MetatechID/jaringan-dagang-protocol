"""Midtrans notifications are hints; financial decisions use authenticated status."""
import asyncio
import json
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from config import settings
from database import get_db
from models.brand import Brand
from models.bot_rest import Cart, CartStatus
from models.order import Order, OrderState
from models.escrow_ledger import EscrowLedger, EscrowEntryType, EscrowEntryStatus
from models.midtrans_payment import MidtransPaymentLink, MidtransPaymentAttempt
from services import midtrans_client as client, order_paid

router = APIRouter(prefix="/webhooks", tags=["midtrans"])


def verified_status(status, *, attempt_id, amount, config):
    if status.get("order_id") != attempt_id or status.get("currency") != "IDR":
        raise ValueError("Payment reference or currency mismatch")
    if client.amount_idr(status.get("gross_amount")) != amount:
        raise ValueError("Payment amount mismatch")
    if config.merchant_id and status.get("merchant_id") != config.merchant_id:
        raise ValueError("Payment merchant mismatch")
    if not isinstance(status.get("transaction_id"), str) or not status["transaction_id"]:
        raise ValueError("Missing transaction identity")
    state = status.get("transaction_status")
    paid = state in {"settlement", "capture"} and str(status.get("status_code")) == "200"
    fraud = status.get("fraud_status")
    if fraud is not None and fraud != "accept":
        paid = False
    if state == "capture" and fraud != "accept":
        paid = False
    return paid


@router.post("/midtrans")
async def midtrans_notification(request: Request, db: AsyncSession = Depends(get_db)):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 65536:
            raise HTTPException(413, "Notification too large")
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("order_id"), str):
            raise ValueError
        attempt_id = payload["order_id"]
        reference = client.base_reference(attempt_id)
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid Midtrans notification") from None
    link = (await db.execute(select(MidtransPaymentLink).where(
        MidtransPaymentLink.base_reference == reference))).scalar_one_or_none()
    if link is None:
        raise HTTPException(404, "Payment reference not found")
    brand = (await db.execute(select(Brand).where(Brand.id == link.brand_id))).scalar_one()
    try:
        config = client.resolve_config(brand, settings)
    except ValueError:
        raise HTTPException(503, "Payment verification unavailable") from None
    if config.environment != link.environment or not client.verify_signature(payload, config):
        raise HTTPException(403, "Invalid Midtrans signature")
    try:
        status = await client.get_status(config, attempt_id)
        paid = verified_status(status, attempt_id=attempt_id, amount=link.amount_idr, config=config)
        # Signed amount is additionally bound to the durable reservation.
        if client.amount_idr(payload.get("gross_amount")) != link.amount_idr:
            raise ValueError
    except client.MidtransError:
        raise HTTPException(503, "Payment verification unavailable") from None
    except ValueError:
        raise HTTPException(403, "Payment verification mismatch") from None
    # Refresh under lock after HTTP; another callback may have completed meantime.
    link = (await db.execute(select(MidtransPaymentLink).where(
        MidtransPaymentLink.id == link.id).with_for_update().execution_options(populate_existing=True))).scalar_one()
    attempt = (await db.execute(select(MidtransPaymentAttempt).where(
        MidtransPaymentAttempt.attempt_reference == attempt_id))).scalar_one_or_none()
    if attempt is None:
        attempt = MidtransPaymentAttempt(link_id=link.id, attempt_reference=attempt_id,
            transaction_id=status["transaction_id"], status=status.get("transaction_status") or "unknown")
        db.add(attempt)
    elif attempt.link_id != link.id or attempt.transaction_id != status["transaction_id"]:
        raise HTTPException(403, "Payment attempt identity mismatch")
    else:
        attempt.status = status.get("transaction_status") or "unknown"
    if not paid:
        await db.commit()
        return {"ok": True}
    if link.paid_attempt_id:
        # A second successful charge requires manual reconciliation, not another HOLD.
        if link.paid_attempt_id != attempt_id:
            attempt.status = "duplicate_paid"
        await db.commit()
        return {"ok": True}
    if link.order_id:
        order = (await db.execute(select(Order).where(Order.id == link.order_id).with_for_update())).scalar_one()
        if order.brand_id != link.brand_id or order.total_idr != link.amount_idr:
            raise HTTPException(409, "Checkout changed after payment reservation")
        if order.state != OrderState.CART_REVIEWED:
            # Existing paid/advanced/cancelled state must not gain a new HOLD.
            attempt.status = "needs_reconciliation"
            await db.commit()
            return {"ok": True}
        order = await order_paid.mark_order_paid(db, order_id=order.id, invoice_id=reference,
                                        actor="system:midtrans_webhook", schedule_effects=False)
        if order.state != OrderState.ESCROW_HELD:
            raise HTTPException(409, "Paid transition requires reconciliation")
    else:
        cart = (await db.execute(select(Cart).where(Cart.id == link.cart_id).with_for_update())).scalar_one()
        if client.amount_idr((cart.quote_json or {}).get("total_idr")) != link.amount_idr or not cart.order_id:
            raise HTTPException(409, "Checkout changed after payment reservation")
        if cart.payment_state == "paid":
            attempt.status = "needs_reconciliation"
            await db.commit()
            return {"ok": True}
        cart.payment_state = "paid"
        cart.status = CartStatus.CONFIRMED
        cart.invoice_provider = "midtrans"
        existing = (await db.execute(select(EscrowLedger).where(
            EscrowLedger.order_id == cart.order_id,
            EscrowLedger.entry_type == EscrowEntryType.HOLD))).scalars().first()
        if existing is None:
            db.add(EscrowLedger(order_id=cart.order_id, external_ref=reference,
                entry_type=EscrowEntryType.HOLD, amount_idr=link.amount_idr,
                status=EscrowEntryStatus.COMPLETED, description=f"Midtrans funds held — {reference}"))
    link.status = "paid"
    link.paid_attempt_id = attempt_id
    await db.commit()
    # Never dispatch fulfillment/attribution for a transaction that rolled back.
    if link.order_id:
        asyncio.create_task(order_paid._dispatch_to_seller(order))
        asyncio.create_task(order_paid._fire_capi_purchase_bg(order.id))
    return {"ok": True}
