"""Partner API — external merchants register transactions and receive a
pay-page URL (``/pay/{id}``).

  POST /api/v1/partner/orders         — register a payment, mint the invoice
  GET  /api/v1/partner/orders/{id}    — auth'd status fetch (owner only)
  GET  /api/v1/partner/public/orders/{id} — read-only status for the pay page

All ``/orders`` endpoints require ``Authorization: Bearer <partner_api_key>``
(see ``auth/partner_auth.py``). The public status endpoint is deliberately
minimal (status + amount only) so the pay page can poll it without auth.

Invoice creation reuses ``services.xendit_client.create_invoice`` — the
same raw call ``services/xendit_invoices.py`` makes for carts/orders, with
``external_id = "partner-<uuid>"`` so ``routers/webhooks_xendit.py`` can
route the ``invoice.paid`` callback back here. Brands without a Xendit
sub-account fall back to mock mode (same rule as ``xendit_invoices._is_mock_mode``)
so the flow is testable end-to-end before Xendit onboarding completes.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from auth.partner_auth import require_partner
from config import settings
from database import get_db
from models.brand import Brand
from models.partner_order import PartnerOrder
from services import xendit_client
from services.partner_orders import create_partner_order, get_partner_order
from services.xendit_client import XenditError

_LOG = logging.getLogger("beli_aman_bap.partner")

router = APIRouter(prefix="/api/v1/partner", tags=["partner"])

_MAX_EXPIRY_SECONDS = 24 * 3600
_DEFAULT_EXPIRY_SECONDS = 3600


# ---------- Schemas ----------


class PartnerBuyerIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=5, max_length=255)
    phone: str = Field(min_length=5, max_length=30)

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        # Plain regex — avoids the email-validator dependency that EmailStr
        # pulls in; Xendit accepts any string as payer_email anyway.
        if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", v):
            raise ValueError("invalid email address")
        return v


class PartnerItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    qty: int = Field(default=1, ge=1)
    unit_price_idr: int = Field(ge=0)


class PartnerOrderIn(BaseModel):
    external_order_id: str = Field(min_length=1, max_length=255)
    amount_idr: int = Field(gt=0, le=200_000_000)
    description: str = Field(min_length=1, max_length=500)
    buyer: PartnerBuyerIn
    items: list[PartnerItemIn] = Field(default_factory=list)
    success_url: str | None = Field(default=None, max_length=2048)
    expires_in_seconds: int = Field(
        default=_DEFAULT_EXPIRY_SECONDS, ge=60, le=_MAX_EXPIRY_SECONDS
    )

    @field_validator("external_order_id")
    @classmethod
    def _strip_external(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("external_order_id must not be blank")
        return v


class PartnerOrderOut(BaseModel):
    order_id: str
    external_order_id: str
    status: str
    amount_idr: int
    payment_url: str
    expires_at: str | None = None


class PartnerStatusOut(BaseModel):
    order_id: str
    external_order_id: str
    status: str
    amount_idr: int
    paid_at: str | None = None
    payment_url: str | None = None


class PublicStatusOut(BaseModel):
    status: str
    amount_idr: int
    paid_at: str | None = None
    success_url: str | None = None


# ---------- Helpers ----------


def _pay_page_url(order: PartnerOrder) -> str:
    base = settings.partner_pay_base_url.rstrip("/")
    return f"{base}/pay/{order.id}"


def _is_mock_mode(brand: Brand) -> bool:
    """Same rule as ``services/xendit_invoices._is_mock_mode`` — brands not
    yet onboarded to Xendit still get a working (sandbox) payment flow."""
    return (
        not brand.xendit_sub_account_id
        or not getattr(settings, "xendit_secret_key", "")
    )


async def _create_invoice(
    db: AsyncSession, brand: Brand, order: PartnerOrder, expires_in: int
) -> None:
    """Mint the Xendit invoice for a partner order and stamp the row."""
    if _is_mock_mode(brand):
        mock_invoice_id = f"dev-partner-{order.id}"
        order.invoice_id = mock_invoice_id
        order.invoice_provider = "xendit"
        order.payment_url = _pay_page_url(order)
        _LOG.warning(
            "partner invoice: mock fallback for order=%s brand=%s "
            "(sub_account=%s xendit_key=%s)",
            order.id, brand.slug,
            brand.xendit_sub_account_id,
            bool(getattr(settings, "xendit_secret_key", "")),
        )
        return

    base = settings.partner_pay_base_url.rstrip("/")
    response = await xendit_client.create_invoice(
        for_user_id=brand.xendit_sub_account_id,
        external_id=f"partner-{order.id}",
        amount_idr=order.amount_idr,
        description=order.description[:255],
        customer_email=(order.buyer or {}).get("email"),
        customer_name=(order.buyer or {}).get("name"),
        success_redirect_url=order.success_url or f"{base}/pay/{order.id}/done",
        failure_redirect_url=f"{base}/pay/{order.id}/failed",
        duration_seconds=expires_in,
        items=[
            {"name": i.name[:255], "quantity": i.qty, "price": i.unit_price_idr}
            for i in (order.items or [])
        ] or None,
    )
    order.invoice_id = response.get("id")
    order.invoice_provider = "xendit"
    order.payment_url = _pay_page_url(order)


# ---------- Endpoints ----------


@router.post("/orders", response_model=PartnerOrderOut)
async def register_order(
    body: PartnerOrderIn,
    brand: Brand = Depends(require_partner),
    db: AsyncSession = Depends(get_db),
) -> PartnerOrderOut:
    """Register a payment and get the pay-page URL to redirect the buyer to."""
    order = await create_partner_order(
        db,
        brand=brand,
        external_order_id=body.external_order_id,
        amount_idr=body.amount_idr,
        description=body.description,
        buyer=body.buyer.model_dump(),
        items=[i.model_dump() for i in body.items],
        success_url=body.success_url,
    )

    try:
        await _create_invoice(db, brand, order, body.expires_in_seconds)
    except XenditError as e:
        await db.rollback()
        _LOG.exception("Xendit invoice creation failed for partner order")
        raise HTTPException(502, f"Payment provider error: {e.status_code}")

    await db.commit()
    await db.refresh(order)

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=body.expires_in_seconds)
    return PartnerOrderOut(
        order_id=order.id,
        external_order_id=order.external_order_id,
        status=order.status,
        amount_idr=order.amount_idr,
        payment_url=order.payment_url,
        expires_at=expires_at.isoformat(),
    )


@router.get("/orders/{order_id}", response_model=PartnerStatusOut)
async def get_order_status(
    order_id: str,
    brand: Brand = Depends(require_partner),
    db: AsyncSession = Depends(get_db),
) -> PartnerStatusOut:
    order = await get_partner_order(db, order_id)
    if order is None or order.brand_id != brand.id:
        raise HTTPException(404, "Order not found")
    return PartnerStatusOut(
        order_id=order.id,
        external_order_id=order.external_order_id,
        status=order.status,
        amount_idr=order.amount_idr,
        paid_at=order.paid_at.isoformat() if order.paid_at else None,
        payment_url=order.payment_url,
    )


@router.get("/public/orders/{order_id}", response_model=PublicStatusOut)
async def get_public_order_status(
    order_id: str,
    db: AsyncSession = Depends(get_db),
) -> PublicStatusOut:
    """Read-only status for the /pay page poller. Exposes only what the
    buyer already sees on the pay page."""
    order = await get_partner_order(db, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    return PublicStatusOut(
        status=order.status,
        amount_idr=order.amount_idr,
        paid_at=order.paid_at.isoformat() if order.paid_at else None,
        success_url=order.success_url,
    )
