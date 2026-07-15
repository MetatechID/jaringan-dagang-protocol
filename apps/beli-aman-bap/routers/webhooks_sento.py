"""Sento payment gateway webhook receiver.

Mirror of ``routers/webhooks_oy.py`` — same /invoice flow, but:

- Auth: NO HMAC signature. Sento's docs do not document HMAC signing, so
  we verify state via the status API instead. The webhook body alone is
  not sufficient to mutate state — we re-check Sento's live status before
  doing anything. The webhook is treated as an advisory notification.

- Body: **Payment Link** shape (flat body, no ``payment_info`` wrapper).
  ``status`` is lowercase: ``created | waiting_payment | expired |
  charge_in_progress | failed | complete | closed``. The invoice id is
  read from ``partner_tx_id``; Sento's internal id echoes back as
  ``tx_ref_number`` and is used as the ledger ``external_ref`` when
  available.

Brand isn't on the path. We resolve it from the body's ``partner_tx_id``
via the same cart/order snapshot lookup used by OY. If Sento says the
invoice is not found (404 from status API), we 404 the webhook — defense
against forged POSTs hitting the receiver.

Reference: https://api-docs.sento.id/docs-page/payment-link
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.bot_rest import Cart, CartStatus
from models.brand import Brand
from models.escrow_ledger import EscrowEntryStatus, EscrowEntryType, EscrowLedger
from services import order_paid
from services import sento_client
from services.sento_client import SentoError

_LOG = logging.getLogger("beli_aman_bap.webhooks_sento")

router = APIRouter(prefix="/webhooks/sento", tags=["webhooks"])


def _parse_status(body: dict) -> str | None:
    """Payment Link callback uses lowercase ``status``. Returns one of
    ``complete | expired | failed | closed | pending | None``.
    """
    raw = str(body.get("status") or "").lower().strip()
    if not raw:
        return None
    if raw in {"created", "waiting_payment", "charge_in_progress"}:
        return "pending"
    if raw in {"complete", "expired", "failed", "closed"}:
        return raw
    return None


async def _resolve_brand_for_invoice_id(
    db: AsyncSession, partner_tx_id: str
) -> Brand | None:
    """Find the brand that owns ``partner_tx_id`` via cart or order snapshot."""
    # 1. Direct cart lookup: cart.invoice_id + invoice_provider=='sento'.
    cart_q = await db.execute(
        select(Cart).where(
            Cart.invoice_id == partner_tx_id,
            Cart.invoice_provider == "sento",
        )
    )
    cart = cart_q.scalars().first()
    if cart is not None:
        brand_q = await db.execute(
            select(Brand).where(Brand.bpp_id == cart.bpp_id)
        )
        return brand_q.scalars().first()

    # 2. Order snapshot lookup: payment_method_snapshot.partner_tx_id
    # (the value we sent to Sento, which Sento echoes back unchanged).
    # Falls back to invoice_id for older orders created before we stored
    # partner_tx_id explicitly.
    from models.order import Order

    order_q = await db.execute(
        select(Order).where(
            or_(
                Order.payment_method_snapshot["partner_tx_id"].astext == partner_tx_id,
                Order.payment_method_snapshot["invoice_id"].astext == partner_tx_id,
            )
        )
    )
    order = order_q.scalars().first()
    if order is not None:
        brand_q = await db.execute(select(Brand).where(Brand.id == order.brand_id))
        return brand_q.scalars().first()

    return None


@router.post("/invoice")
async def invoice_callback(
    request: Request,
    # ponytail: header accepted for future Sento-side signing; v1 verifies
    # state via the status API instead. Remove when Sento documents signing.
    x_sento_signature: str | None = Header(default=None, alias="x-sento-signature"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Receive Sento Payment Link status callbacks (flat body, lowercase
    ``status``)."""
    body_bytes = await request.body()
    body: dict[str, Any] = {}
    if body_bytes:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "Invalid JSON body")

    partner_tx_id = body.get("partner_tx_id")
    if not partner_tx_id or not isinstance(partner_tx_id, str):
        raise HTTPException(400, "Missing partner_tx_id in callback")

    brand = await _resolve_brand_for_invoice_id(db, partner_tx_id)
    if brand is None:
        _LOG.warning(
            "Sento callback: no Brand for partner_tx_id=%s — refusing",
            partner_tx_id,
        )
        raise HTTPException(404, "Unknown Sento invoice")

    # Verify the live state via Sento's status API — webhook is advisory.
    # If Sento says the invoice is not found (404), bounce — defense against
    # forged webhooks hitting us for partner_tx_ids we never issued. For
    # other Sento errors (5xx, network), we proceed with the body's status:
    # webhooks are best-effort, status API may be transient.
    try:
        await sento_client.get_status(
            partner_tx_id=partner_tx_id,
            api_key=brand.sento_api_key,
            username=brand.sento_username,
        )
    except SentoError as e:
        if e.status_code == 404:
            _LOG.warning(
                "Sento status API returned 404 for partner_tx_id=%s — refusing",
                partner_tx_id,
            )
            raise HTTPException(404, "Sento reports invoice not found")
        _LOG.warning(
            "Sento status API error %s for partner_tx_id=%s — proceeding "
            "with body status (advisory)",
            e.status_code, partner_tx_id,
        )

    status = _parse_status(body)
    if not status:
        return {"ok": True, "ignored_status": body.get("status", "")}
    _LOG.info(
        "Sento invoice callback: brand=%s partner_tx_id=%s status=%s",
        getattr(brand, "slug", None), partner_tx_id, status,
    )

    if status == "complete":
        return await _handle_paid(db, partner_tx_id, body)
    if status == "expired":
        return await _handle_expired(db, partner_tx_id, body)
    if status in ("failed", "closed"):
        return await _handle_failed(db, partner_tx_id)
    return {"ok": True, "ignored_status": status}


async def _handle_paid(db: AsyncSession, partner_tx_id: str, body: dict) -> dict:
    """Process a ``complete`` callback (Payment Link).

    The Payment Link flat body carries ``tx_ref_number`` (Sento's internal
    id) directly. We prefer it as ``invoice_id`` so the ledger row's
    ``external_ref`` matches what Sento echoes in the callback; falls back
    to ``partner_tx_id`` if absent.
    """
    invoice_id = str(body.get("tx_ref_number") or partner_tx_id)
    actor = "system:sento_webhook"

    if partner_tx_id.startswith("order-"):
        order_id = partner_tx_id[len("order-"):]
        order = await order_paid.mark_order_paid(
            db,
            order_id=order_id,
            invoice_id=invoice_id,
            actor=actor,
        )
        return {"ok": True, "order_id": order.id, "state": order.state.value}

    if partner_tx_id.startswith("cart-"):
        cart_id = partner_tx_id[len("cart-"):]
        cart = (
            await db.execute(select(Cart).where(Cart.id == cart_id))
        ).scalar_one_or_none()
        if cart is None:
            raise HTTPException(404, f"No cart for partner_tx_id={partner_tx_id}")
        if cart.payment_state != "paid":
            cart.payment_state = "paid"
        if cart.invoice_id is None:
            cart.invoice_id = partner_tx_id
        cart.invoice_provider = "sento"
        if cart.status != CartStatus.CONFIRMED:
            cart.status = CartStatus.CONFIRMED
        if cart.order_id:
            existing = await db.execute(
                select(EscrowLedger).where(
                    EscrowLedger.order_id == cart.order_id,
                    EscrowLedger.external_ref == partner_tx_id,
                )
            )
            if existing.scalars().first() is None:
                amount = int((cart.quote_json or {}).get("total_idr") or 0)
                db.add(EscrowLedger(
                    order_id=cart.order_id,
                    entry_type=EscrowEntryType.HOLD,
                    amount_idr=amount,
                    description=(
                        f"Bot-cart funds held — sento invoice {invoice_id}"
                    ),
                    external_ref=partner_tx_id,
                    status=EscrowEntryStatus.COMPLETED,
                ))
        return {"ok": True, "cart_id": cart.id, "payment_state": cart.payment_state}

    # ponytail: mock-mode invoice ids look like ``sento-dev-{order_id}`` —
    # no prefix to dispatch on. Recover the order via the snapshot and
    # mark it paid. This keeps the resolver uniform across real + mock.
    from models.order import Order as _Order
    recovered = (
        await db.execute(
            select(_Order).where(
                _Order.payment_method_snapshot["invoice_id"].astext == partner_tx_id
            )
        )
    ).scalars().first()
    if recovered is not None:
        order = await order_paid.mark_order_paid(
            db,
            order_id=recovered.id,
            invoice_id=invoice_id,
            actor=actor,
        )
        return {"ok": True, "order_id": order.id, "state": order.state.value}

    raise HTTPException(400, f"Unrecognized partner_tx_id prefix: {partner_tx_id!r}")


async def _handle_expired(db: AsyncSession, partner_tx_id: str, body: dict) -> dict:
    if partner_tx_id.startswith("cart-"):
        cart_id = partner_tx_id[len("cart-"):]
        cart = (
            await db.execute(select(Cart).where(Cart.id == cart_id))
        ).scalar_one_or_none()
        if cart and cart.payment_state == "pending":
            cart.payment_state = "expired"
            cart.status = CartStatus.EXPIRED
            return {"ok": True, "cart_id": cart.id, "payment_state": cart.payment_state}
    return {"ok": True, "expired_partner_tx_id": partner_tx_id}


async def _handle_failed(db: AsyncSession, partner_tx_id: str) -> dict:
    """``failed`` / ``closed`` callback — mark cart as failed if matched.
    We don't touch order rows here; those flow through ``_handle_paid``
    only.
    """
    cart_q = await db.execute(
        select(Cart).where(
            Cart.invoice_id == partner_tx_id,
            Cart.invoice_provider == "sento",
        )
    )
    cart = cart_q.scalars().first()
    if cart and cart.payment_state in ("pending", None):
        cart.payment_state = "failed"
        return {"ok": True, "cart_id": cart.id, "payment_state": cart.payment_state}
    return {"ok": True, "partner_tx_id": partner_tx_id, "failed": True}