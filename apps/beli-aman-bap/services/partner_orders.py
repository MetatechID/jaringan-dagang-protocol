"""Partner order lifecycle — create / fetch / mark-paid for external
merchants registering payments via ``/api/v1/partner``.

Deliberately escrow-free (see ``models/partner_order.py``): the partner
fulfills instantly on the paid callback. ``mark_partner_order_paid`` is
idempotent — a duplicate webhook never re-triggers the callback chain.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.brand import Brand
from models.partner_order import PartnerOrder, PartnerOrderStatus

_LOG = logging.getLogger("beli_aman_bap.partner_orders")


async def get_partner_order_by_external_id(
    db: AsyncSession, *, brand_id: str, external_order_id: str
) -> PartnerOrder | None:
    return (
        await db.execute(
            select(PartnerOrder).where(
                PartnerOrder.brand_id == brand_id,
                PartnerOrder.external_order_id == external_order_id,
            )
        )
    ).scalar_one_or_none()


async def create_partner_order(
    db: AsyncSession,
    *,
    brand: Brand,
    external_order_id: str,
    amount_idr: int,
    description: str,
    buyer: dict | None,
    items: list | None,
    success_url: str | None,
) -> PartnerOrder:
    """Insert a pending partner order. Raises 409 when the partner already
    registered this ``external_order_id`` (idempotency key per brand)."""
    existing = await get_partner_order_by_external_id(
        db, brand_id=brand.id, external_order_id=external_order_id
    )
    if existing is not None:
        raise HTTPException(
            409,
            f"Order {external_order_id!r} already registered for this partner",
        )

    order = PartnerOrder(
        # Explicit id — the column default only fires on a real DB flush, and
        # callers (tests, mock mode) read order.id before any flush happens.
        id=str(uuid.uuid4()),
        brand_id=brand.id,
        external_order_id=external_order_id,
        amount_idr=amount_idr,
        description=description,
        buyer=buyer,
        items=items,
        status=PartnerOrderStatus.PENDING,
        success_url=success_url,
    )
    db.add(order)
    await db.flush()
    return order


async def get_partner_order(db: AsyncSession, order_id: str) -> PartnerOrder | None:
    return (
        await db.execute(select(PartnerOrder).where(PartnerOrder.id == order_id))
    ).scalar_one_or_none()


async def get_partner_order_by_invoice(
    db: AsyncSession, invoice_id: str
) -> PartnerOrder | None:
    return (
        await db.execute(
            select(PartnerOrder).where(PartnerOrder.invoice_id == invoice_id)
        )
    ).scalar_one_or_none()


async def mark_partner_order_paid(
    *,
    order: PartnerOrder,
    invoice_id: str,
    actor: str = "system:xendit_webhook",
) -> PartnerOrder:
    """Idempotently flip pending → paid and stamp the invoice id.

    Caller is responsible for committing. Returns the order (already-paid
    duplicates return it unchanged).
    """
    if order.status == PartnerOrderStatus.PAID:
        if not order.invoice_id:
            order.invoice_id = invoice_id
        return order

    if order.status != PartnerOrderStatus.PENDING:
        _LOG.warning(
            "Partner order %s in unexpected state %s on paid webhook (%s) — leaving",
            order.id, order.status, actor,
        )
        return order

    order.status = PartnerOrderStatus.PAID
    order.paid_at = datetime.now(timezone.utc)
    if not order.invoice_id:
        order.invoice_id = invoice_id
    if not order.invoice_provider:
        order.invoice_provider = "xendit"
    return order


async def mark_partner_order_expired(order: PartnerOrder) -> PartnerOrder:
    """Flip pending → expired on the Xendit invoice.expired callback.
    Paid orders are never demoted."""
    if order.status == PartnerOrderStatus.PENDING:
        order.status = PartnerOrderStatus.EXPIRED
    return order
