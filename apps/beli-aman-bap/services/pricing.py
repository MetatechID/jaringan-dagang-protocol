"""Price computation. Server-side validation against the catalog is the
guard against client-tampered prices."""

from __future__ import annotations

from typing import Iterable, TypedDict


class CartItem(TypedDict):
    sku: str
    name: str
    qty: int
    unit_price_idr: int
    image: str | None


class PriceBreakdown(TypedDict):
    subtotal_idr: int
    shipping_idr: int
    fee_idr: int
    total_idr: int


def compute_breakdown(items: Iterable[CartItem], *, shipping_idr: int = 0, fee_pct_bp: int = 0) -> PriceBreakdown:
    """Sum line items, add shipping + fee. fee_pct_bp is in basis points (100 = 1%)."""
    subtotal = sum(int(it["unit_price_idr"]) * int(it["qty"]) for it in items)
    fee = (subtotal * fee_pct_bp) // 10_000
    total = subtotal + shipping_idr + fee
    return {
        "subtotal_idr": subtotal,
        "shipping_idr": shipping_idr,
        "fee_idr": fee,
        "total_idr": total,
    }
