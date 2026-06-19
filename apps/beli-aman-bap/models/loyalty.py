"""LoyaltyTransaction — append-only points ledger per profile.

One row per points movement. The current balance is ``SUM(points)`` over a
profile's rows. ``points`` is positive for "earn"/"adjust"-up and negative for
"redeem"/"adjust"-down; ``kind`` records the intent.

Earning is driven by ``accrue_for_order``: 1 point per Rp1000 of an order's
total, inserted (idempotently) when escrow is released for that order. The
escrow-release code calls ``accrue_for_order`` directly — see its docstring for
the call signature.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import ForeignKey, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class LoyaltyTransaction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single points movement belonging to a Beli Aman profile."""

    __tablename__ = "loyalty_transactions"

    profile_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("profiles.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # The order that earned the points (null for manual adjust / redeem).
    order_id: Mapped[Optional[str]] = mapped_column(
        String(36), index=True, nullable=True
    )
    # Positive = earn, negative = redeem.
    points: Mapped[int] = mapped_column(Integer, nullable=False)
    # "earn" | "redeem" | "adjust".
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


async def accrue_for_order(
    db: AsyncSession,
    *,
    profile_id: str,
    order_id: str,
    total_idr: int,
) -> None:
    """Idempotently award loyalty points for a paid/released order.

    Awards ``total_idr // 1000`` points (1 point per Rp1000) as a single
    "earn" transaction tied to ``order_id``. Idempotent: if an "earn" row
    already exists for this ``order_id`` it is a no-op, so calling this more
    than once per order (e.g. webhook retries) never double-credits.

    The caller owns the transaction — this only adds to the session and
    flushes; it does not commit.
    """
    existing = await db.execute(
        select(LoyaltyTransaction.id).where(
            LoyaltyTransaction.order_id == order_id,
            LoyaltyTransaction.kind == "earn",
        )
    )
    if existing.scalars().first() is not None:
        return

    points = total_idr // 1000
    if points <= 0:
        return

    db.add(
        LoyaltyTransaction(
            profile_id=profile_id,
            order_id=order_id,
            points=points,
            kind="earn",
            description=f"Earned {points} points from order {order_id}",
        )
    )
    await db.flush()
