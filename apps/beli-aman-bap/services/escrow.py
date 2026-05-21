"""Escrow ledger writes — append-only, never updates an existing row."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.escrow_ledger import EscrowEntryType, EscrowLedger


async def hold(db: AsyncSession, *, order_id: str, amount_idr: int, description: str = "") -> EscrowLedger:
    entry = EscrowLedger(
        order_id=order_id,
        entry_type=EscrowEntryType.HOLD,
        amount_idr=amount_idr,
        description=description or "Funds held by Beli Aman pending receipt",
    )
    db.add(entry)
    await db.flush()
    return entry


async def release(db: AsyncSession, *, order_id: str, amount_idr: int, description: str = "") -> EscrowLedger:
    entry = EscrowLedger(
        order_id=order_id,
        entry_type=EscrowEntryType.RELEASE,
        amount_idr=amount_idr,
        description=description or "Funds released to seller after delivery confirmed",
    )
    db.add(entry)
    await db.flush()
    return entry


async def refund(db: AsyncSession, *, order_id: str, amount_idr: int, description: str = "") -> EscrowLedger:
    entry = EscrowLedger(
        order_id=order_id,
        entry_type=EscrowEntryType.REFUND,
        amount_idr=amount_idr,
        description=description or "Funds refunded to buyer",
    )
    db.add(entry)
    await db.flush()
    return entry


async def held_balance(db: AsyncSession, *, order_id: str) -> int:
    """Sum of HOLD - RELEASE - REFUND for an order. Always 0 or total in v1."""
    result = await db.execute(
        select(EscrowLedger).where(EscrowLedger.order_id == order_id)
    )
    rows = result.scalars().all()
    total = 0
    for r in rows:
        if r.entry_type == EscrowEntryType.HOLD:
            total += r.amount_idr
        else:
            total -= r.amount_idr
    return total
