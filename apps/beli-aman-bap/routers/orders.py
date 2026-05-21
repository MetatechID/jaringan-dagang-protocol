"""Order CRUD + state-advance endpoints used by the SDK flow."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import get_db
from deps import get_current_profile
from models.address import Address
from models.brand import Brand
from models.escrow_ledger import EscrowEntryType, EscrowLedger
from models.order import Order, OrderState
from models.order_event import OrderEvent
from models.profile import BeliAmanProfile
from services import catalog as catalog_service
from services import pricing
from services.state_machine import (
    StateTransitionError,
    lock_order_for_update,
    transition,
)

router = APIRouter(prefix="/api/v1/orders", tags=["orders"])


# ---------- Schemas ----------


class CartItemIn(BaseModel):
    sku: str
    qty: int = Field(ge=1)


class ShippingChoiceIn(BaseModel):
    courier_code: str
    courier_service_code: str | None = None
    courier_service_name: str | None = None
    price_idr: int = Field(ge=0)
    duration: str | None = None


class CreateOrderIn(BaseModel):
    brand_slug: str
    items: list[CartItemIn]
    shipping: ShippingChoiceIn | None = None


def _serialize_order(o: Order) -> dict[str, Any]:
    return {
        "id": o.id,
        "state": o.state.value,
        "brand_id": o.brand_id,
        "items": o.items,
        "subtotal_idr": o.subtotal_idr,
        "shipping_idr": o.shipping_idr,
        "fee_idr": o.fee_idr,
        "total_idr": o.total_idr,
        "shipping_address": o.shipping_address,
        "payment_method_snapshot": o.payment_method_snapshot,
        "bap_id": o.bap_id,
        "bpp_id": o.bpp_id,
        "shipped_simulated_at": o.shipped_simulated_at.isoformat() if o.shipped_simulated_at else None,
        "delivered_simulated_at": o.delivered_simulated_at.isoformat() if o.delivered_simulated_at else None,
        "auto_release_at": o.auto_release_at.isoformat() if o.auto_release_at else None,
        "released_at": o.released_at.isoformat() if o.released_at else None,
        "created_at": o.created_at.isoformat(),
        "updated_at": o.updated_at.isoformat(),
    }


# ---------- Endpoints ----------


@router.post("")
async def create_order(
    body: CreateOrderIn,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a PRE_AUTH order from a cart (server validates prices vs catalog)."""
    brand_result = await db.execute(select(Brand).where(Brand.slug == body.brand_slug))
    brand = brand_result.scalar_one_or_none()
    if not brand:
        raise HTTPException(404, f"Brand '{body.brand_slug}' not found")

    # Validate items + materialize line snapshots. Resolve SKUs that may be
    # either a parent SKU or a variant SKU on a parent product.
    products = await catalog_service.list_products(body.brand_slug)
    parent_by_sku: dict[str, dict[str, Any]] = {p["sku"]: p for p in products}
    variant_lookup: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for p in products:
        for v in p.get("variants", []) or []:
            variant_lookup[v["sku"]] = (p, v)

    line_snapshots: list[dict[str, Any]] = []
    for item in body.items:
        if item.sku in variant_lookup:
            parent, variant = variant_lookup[item.sku]
            line_snapshots.append({
                "sku": item.sku,
                "name": f'{parent["name"]} - {variant.get("label", "")}',
                "qty": item.qty,
                "unit_price_idr": int(variant.get("price_idr") or parent.get("price_idr") or 0),
                "image": variant.get("image") or parent.get("image"),
            })
        elif item.sku in parent_by_sku:
            product = parent_by_sku[item.sku]
            line_snapshots.append({
                "sku": item.sku,
                "name": product["name"],
                "qty": item.qty,
                "unit_price_idr": int(product["price_idr"]),
                "image": product.get("image"),
            })
        else:
            raise HTTPException(400, f"Unknown SKU '{item.sku}' for brand {body.brand_slug}")

    shipping_idr = int(body.shipping.price_idr) if body.shipping else 0
    breakdown = pricing.compute_breakdown(line_snapshots, shipping_idr=shipping_idr, fee_pct_bp=brand.fee_pct_bp)

    shipping_snapshot = body.shipping.model_dump() if body.shipping else None
    order = Order(
        profile_id=profile.id,
        brand_id=brand.id,
        items=line_snapshots,
        subtotal_idr=breakdown["subtotal_idr"],
        shipping_idr=breakdown["shipping_idr"],
        fee_idr=breakdown["fee_idr"],
        total_idr=breakdown["total_idr"],
        shipping_address={"courier": shipping_snapshot} if shipping_snapshot else None,
        bap_id=settings.subscriber_id,
        bpp_id=brand.bpp_id,
    )
    db.add(order)
    await db.flush()

    # Genesis event
    db.add(OrderEvent(
        order_id=order.id,
        from_state=None,
        to_state=OrderState.PRE_AUTH,
        actor=f"buyer:{profile.id}",
        payload={"event": "created"},
    ))
    return _serialize_order(order)


@router.get("")
async def list_my_orders(
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    result = await db.execute(
        select(Order).where(Order.profile_id == profile.id).order_by(Order.created_at.desc())
    )
    return [_serialize_order(o) for o in result.scalars().all()]


@router.get("/{order_id}")
async def get_order(
    order_id: str,
    profile: BeliAmanProfile | None = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Fetch an order. Side-effect: lazy auto-release if the D+3 window passed."""
    order = await lock_order_for_update(db, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    if profile and order.profile_id != profile.id:
        raise HTTPException(403, "Not your order")

    # Lazy auto-release: if RECEIVED and auto_release_at is in the past, release now.
    if (
        order.state == OrderState.RECEIVED
        and order.auto_release_at is not None
        and order.auto_release_at <= datetime.now(timezone.utc)
    ):
        try:
            await transition(
                db, order, OrderState.ESCROW_RELEASED,
                actor="system:auto_release",
                payload={"reason": "D+3 elapsed (lazy release on read)"},
            )
            db.add(EscrowLedger(
                order_id=order.id,
                entry_type=EscrowEntryType.RELEASE,
                amount_idr=order.total_idr,
                description="Auto-release after D+3 (lazy)",
            ))
            order.released_at = datetime.now(timezone.utc)
        except StateTransitionError:
            pass

    # Also include the ledger
    ledger_result = await db.execute(
        select(EscrowLedger).where(EscrowLedger.order_id == order.id).order_by(EscrowLedger.created_at)
    )
    ledger_rows = [
        {
            "entry_type": e.entry_type.value,
            "amount_idr": e.amount_idr,
            "description": e.description,
            "created_at": e.created_at.isoformat(),
        }
        for e in ledger_result.scalars().all()
    ]

    out = _serialize_order(order)
    out["escrow_ledger"] = ledger_rows
    return out


# ---------- State-advance endpoints (used by the SDK flow) ----------


class AdvanceWithAddressIn(BaseModel):
    address_id: str | None = None
    address_inline: dict | None = None
    payment_method_id: str | None = None


@router.patch("/{order_id}/auth")
async def advance_to_authed(
    order_id: str,
    body: AdvanceWithAddressIn,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """PRE_AUTH → AUTHED. Attaches address + payment method."""
    order = await lock_order_for_update(db, order_id)
    if not order or order.profile_id != profile.id:
        raise HTTPException(404, "Order not found")

    # Resolve address
    addr_snapshot: dict | None = None
    if body.address_id:
        addr_q = await db.execute(
            select(Address).where(Address.id == body.address_id, Address.profile_id == profile.id)
        )
        addr = addr_q.scalar_one_or_none()
        if not addr:
            raise HTTPException(400, "Address not found")
        addr_snapshot = {
            "recipient_name": addr.recipient_name,
            "phone_e164": addr.phone_e164,
            "line1": addr.line1,
            "line2": addr.line2,
            "kelurahan": addr.kelurahan,
            "kecamatan": addr.kecamatan,
            "kota": addr.kota,
            "provinsi": addr.provinsi,
            "postal_code": addr.postal_code,
        }
    elif body.address_inline:
        addr_snapshot = body.address_inline
    else:
        raise HTTPException(400, "Must provide address_id or address_inline")

    order.shipping_address = addr_snapshot
    order.payment_method_snapshot = {
        "type": "virtual_account",
        "display_label": "BCA Virtual Account — Demo",
    }

    try:
        await transition(db, order, OrderState.AUTHED, actor=f"buyer:{profile.id}")
    except StateTransitionError as e:
        raise HTTPException(409, str(e))
    return _serialize_order(order)


@router.patch("/{order_id}/review")
async def advance_to_reviewed(
    order_id: str,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """AUTHED → CART_REVIEWED."""
    order = await lock_order_for_update(db, order_id)
    if not order or order.profile_id != profile.id:
        raise HTTPException(404, "Order not found")
    try:
        await transition(db, order, OrderState.CART_REVIEWED, actor=f"buyer:{profile.id}")
    except StateTransitionError as e:
        raise HTTPException(409, str(e))
    return _serialize_order(order)
