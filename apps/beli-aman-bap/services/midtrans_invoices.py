"""Hosted Midtrans invoices with durable, tenant-bound link reservations."""
import uuid
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from sqlalchemy import select
from config import settings
from models.brand import Brand
from models.bot_rest import Cart, CartStatus
from models.order import Order
from models.midtrans_payment import MidtransPaymentLink
from services import midtrans_client as client


def _result(link):
    return {"id": link.base_reference, "invoice_url": link.invoice_url,
            "expires_at": link.expires_at.isoformat()}


async def _create(db, owner, *, is_cart, reservation_db):
    if reservation_db is None:
        raise RuntimeError("Midtrans requires an independent reservation session")
    model = Cart if is_cart else Order
    brand_query = Brand.bpp_id == owner.bpp_id if is_cart else Brand.id == owner.brand_id
    session = reservation_db
    brand = (await session.execute(select(Brand).where(brand_query).with_for_update())).scalar_one_or_none()
    if brand is None or brand.payment_provider != "midtrans":
        raise HTTPException(409, "Midtrans brand configuration required")
    try:
        config = client.resolve_config(brand, settings)
        total = client.amount_idr((owner.quote_json or {}).get("total_idr") if is_cart else owner.total_idr)
    except ValueError:
        raise HTTPException(409, "Invalid Midtrans configuration or payment amount") from None
    # Match webhook lock order: link first, then checkout owner.
    condition = MidtransPaymentLink.cart_id == owner.id if is_cart else MidtransPaymentLink.order_id == owner.id
    link = (await session.execute(select(MidtransPaymentLink).where(condition).with_for_update())).scalar_one_or_none()
    locked = (await session.execute(select(model).where(model.id == owner.id).with_for_update())).scalar_one()
    try:
        locked_total = client.amount_idr((locked.quote_json or {}).get("total_idr") if is_cart else locked.total_idr)
    except ValueError:
        raise HTTPException(409, "Invalid checkout payment amount") from None
    if locked_total != total or (not is_cart and locked.brand_id != brand.id) or (is_cart and locked.bpp_id != brand.bpp_id):
        raise HTTPException(409, "Checkout changed before payment reservation")
    if not is_cart and locked.state.value != "CART_REVIEWED":
        raise HTTPException(409, "Checkout no longer awaiting payment")
    recovering = link is not None
    if link is None:
        prior = locked.invoice_id if is_cart else (locked.payment_method_snapshot or {}).get("invoice_id")
        if prior:
            raise HTTPException(409, "Checkout already has another provider invoice")
        link = MidtransPaymentLink(brand_id=brand.id, cart_id=owner.id if is_cart else None,
            order_id=None if is_cart else owner.id, base_reference="mt-" + uuid.uuid4().hex,
            amount_idr=total, environment=config.environment, status="creating",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=60))
        session.add(link)
        await session.flush()
    elif link.amount_idr != total or link.environment != config.environment or link.brand_id != brand.id:
        raise HTTPException(409, "Midtrans payment reservation does not match checkout")
    reference = link.base_reference
    if link.paid_attempt_id:
        if link.invoice_url:
            return _result(link)
        raise HTTPException(409, "Checkout payment already completed")
    if link.expires_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
        raise HTTPException(409, "Payment link expired; reconciliation required")
    if is_cart:
        locked.invoice_id = reference
        locked.invoice_provider = "midtrans"
        # Preserve confirmed order identity before a callback can arrive.
        if not locked.order_id:
            locked.order_id = owner.order_id
        locked.status = CartStatus.CONFIRMED
    else:
        snap = dict(locked.payment_method_snapshot or {})
        snap.update(payment_provider="midtrans", type="midtrans_link", invoice_id=reference,
                    invoice_status="ready" if link.invoice_url else "creating")
        locked.payment_method_snapshot = snap
    await session.commit()
    if not link.invoice_url:
        try:
            # Existing/uncertain reservations always reconcile the same ID first.
            if recovering:
                try:
                    response = await client.get_link(config, reference)
                except client.MidtransError as exc:
                    if exc.status_code != 404:
                        raise
                    # Stable reference makes simultaneous POSTs provider-idempotent.
                    response = await client.create_link(config, reference, total)
            else:
                response = await client.create_link(config, reference, total)
        except client.MidtransError as exc:
            if exc.status_code in {409, 502, 500, 503, 504}:
                try:
                    response = await client.get_link(config, reference)
                except client.MidtransError:
                    raise HTTPException(503, "Midtrans payment creation pending; retry checkout") from None
            else:
                raise HTTPException(502, "Midtrans rejected payment creation") from None
        if response.get("order_id") != reference:
            raise HTTPException(502, "Midtrans returned an invalid payment reference")
        if "gross_amount" in response and client.amount_idr(response["gross_amount"]) != total:
            raise HTTPException(502, "Midtrans returned an invalid payment amount")
        try:
            url = client.payment_url(response.get("payment_url") or response.get("payment_link_url") or "", config)
        except client.MidtransError:
            raise HTTPException(502, "Midtrans returned an invalid payment URL") from None
        link = (await session.execute(select(MidtransPaymentLink).where(
            MidtransPaymentLink.base_reference == reference).with_for_update().execution_options(populate_existing=True))).scalar_one()
        link.invoice_url = url
        if link.status != "paid":
            link.status = "ready"
        await session.commit()
    result = _result(link)
    if is_cart:
        # Discard pre-reservation cart mutations without flushing stale pending state
        # over a callback that may already have committed payment.
        with db.no_autoflush:
            await db.refresh(owner, attribute_names=["payment_state", "status", "order_id"])
        owner.invoice_id = reference
        owner.invoice_provider = "midtrans"
        owner.qr_image_url = result["invoice_url"]
        owner.qris_image_url = None
        owner.qris_content = None
        owner.expires_at = link.expires_at
    else:
        # Only merge invoice fields; never copy stale payment/fulfillment state.
        await db.refresh(owner, attribute_names=["payment_method_snapshot"])
        snap = dict(owner.payment_method_snapshot or {})
        snap.update(payment_provider="midtrans", type="midtrans_link", invoice_id=reference,
                    invoice_status="ready", invoice_url=result["invoice_url"], expires_at=result["expires_at"])
        owner.payment_method_snapshot = snap
    return result


async def create_invoice_for_cart(db, cart, *, reservation_db=None):
    return await _create(db, cart, is_cart=True, reservation_db=reservation_db)


async def create_invoice_for_order(db, order, *, buyer_email=None, reservation_db=None):
    return await _create(db, order, is_cart=False, reservation_db=reservation_db)
