"""PartnerOrder — an external merchant's transaction registered via the
partner API (e.g. a Consumerland ticket order paid through Oito).

Deliberately simpler than ``Order``: partners fulfill instantly on the
``paid`` callback (Consumerland issues e-tickets), so there is no escrow
state machine here — just ``pending`` → ``paid`` (or ``expired`` when the
Xendit invoice expires unpaid).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PartnerOrderStatus:
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"


class PartnerOrder(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A payment registered by an external partner via ``/api/v1/partner``."""

    __tablename__ = "partner_orders"

    brand_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("brands.id"), nullable=False, index=True,
    )
    # The partner's own order id (e.g. Consumerland's "CL26-XXXXXX").
    # Unique per brand — the idempotency key for order registration.
    external_order_id: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True,
    )
    amount_idr: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # {name, email, phone} — forwarded to the Xendit invoice as the payer.
    buyer: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    items: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PartnerOrderStatus.PENDING, index=True,
    )

    invoice_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    invoice_provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # The buyer-facing pay page this API returns (a /pay/{id} URL). The
    # underlying Xendit invoice URL is reachable from that page.
    payment_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Where to send the buyer after payment — supplied by the partner.
    success_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
