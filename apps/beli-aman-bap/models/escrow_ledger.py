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


class EscrowEntryStatus(str, enum.Enum):
    """Lifecycle of a single ledger entry vs. its PSP-side execution.

    - HOLD entries are written from the Xendit ``invoice.paid`` webhook —
      money is already in the seller's Xendit sub-account, so HOLD lands
      directly as COMPLETED.
    - RELEASE entries are PENDING from the moment they're written (a Xendit
      disbursement was just kicked off) and flip to COMPLETED when the
      ``disbursement.completed`` callback arrives. FAILED is set if Xendit
      rejects the disbursement; ops takes manual recovery from there.
    - REFUND entries are PENDING → COMPLETED similarly when Xendit's
      refund callback fires.
    """

    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class EscrowLedger(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per escrow movement. Never updated except for ``status``
    (which flips PENDING → COMPLETED on the matching PSP callback)."""

    __tablename__ = "escrow_ledger"

    order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("orders.id"), index=True, nullable=False
    )
    entry_type: Mapped[EscrowEntryType] = mapped_column(
        Enum(EscrowEntryType, name="escrow_entry_type"), nullable=False
    )
    amount_idr: Mapped[int] = mapped_column(BigInteger, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # External ref = the Xendit invoice id (for HOLD), disbursement id (for
    # RELEASE), or refund id (for REFUND). Lets webhooks find the ledger
    # row to mark COMPLETED.
    external_ref: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    status: Mapped[EscrowEntryStatus] = mapped_column(
        Enum(EscrowEntryStatus, name="escrow_entry_status"),
        nullable=False,
        default=EscrowEntryStatus.COMPLETED,
    )
