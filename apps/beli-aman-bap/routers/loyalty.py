"""Loyalty points — the signed-in buyer's points balance + ledger."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_profile
from models.loyalty import LoyaltyTransaction
from models.profile import BeliAmanProfile

router = APIRouter(prefix="/api/v1/me/loyalty", tags=["loyalty"])


def _serialize_txn(t: LoyaltyTransaction) -> dict:
    return {
        "id": t.id,
        "points": t.points,
        "kind": t.kind,
        "description": t.description,
        "order_id": t.order_id,
        "created_at": t.created_at,
    }


@router.get("")
async def get_loyalty(
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict:
    balance = (
        await db.execute(
            select(func.coalesce(func.sum(LoyaltyTransaction.points), 0)).where(
                LoyaltyTransaction.profile_id == profile.id
            )
        )
    ).scalar_one()

    result = await db.execute(
        select(LoyaltyTransaction)
        .where(LoyaltyTransaction.profile_id == profile.id)
        .order_by(LoyaltyTransaction.created_at.desc())
    )
    transactions = [_serialize_txn(t) for t in result.scalars().all()]

    return {"balance": int(balance), "transactions": transactions}
