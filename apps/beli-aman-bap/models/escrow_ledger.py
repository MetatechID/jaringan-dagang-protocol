"""EscrowLedger — append-only ledger of escrow movements per order.

Sum of (HOLD - RELEASE - REFUND) per order = current held balance. With our
v1 single-payment model that's only ever 0 or `total_idr`, but the ledger
shape is right for partial refunds + multi-payment in v2.
"""

import enum

from sqlalchemy import BigInteger, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class EscrowEntryType(str, enum.Enum):
    HOLD = "HOLD"
    RELEASE = "RELEASE"
    REFUND = "REFUND"


class EscrowLedger(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per escrow movement. Never updated."""

    __tablename__ = "escrow_ledger"

    order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("orders.id"), index=True, nullable=False
    )
    entry_type: Mapped[EscrowEntryType] = mapped_column(
        Enum(EscrowEntryType, name="escrow_entry_type"), nullable=False
    )
    amount_idr: Mapped[int] = mapped_column(BigInteger, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
