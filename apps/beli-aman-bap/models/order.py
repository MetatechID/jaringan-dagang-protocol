"""Order — the unit of escrow. State machine lives in services/state_machine.py."""

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class OrderState(str, enum.Enum):
    """Beli Aman order state machine.

    Allowed transitions live in services.state_machine.ALLOWED — keep this
    enum and that dict in lockstep.
    """

    PRE_AUTH = "PRE_AUTH"               # cart turned into order, no auth yet
    AUTHED = "AUTHED"                   # signed in, address + payment chosen
    CART_REVIEWED = "CART_REVIEWED"     # final review confirmed
    ESCROW_HELD = "ESCROW_HELD"         # mock-paid, funds "held"
    FULFILLING = "FULFILLING"           # seller marked shipped (mock)
    RECEIVED = "RECEIVED"               # buyer marked received OR D+3 elapsed
    ESCROW_RELEASED = "ESCROW_RELEASED" # terminal happy path
    REFUNDED = "REFUNDED"               # terminal sad path
    DISPUTED = "DISPUTED"               # branch from FULFILLING/RECEIVED


class Order(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A Beli Aman order — the unit of escrow."""

    __tablename__ = "orders"

    profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("profiles.id"), index=True, nullable=False
    )
    brand_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("brands.id"), index=True, nullable=False
    )
    state: Mapped[OrderState] = mapped_column(
        Enum(OrderState, name="order_state"),
        nullable=False,
        default=OrderState.PRE_AUTH,
        index=True,
    )

    # Snapshot of the cart at PRE_AUTH time. Server-validated against catalog.
    items: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    subtotal_idr: Mapped[int] = mapped_column(BigInteger, nullable=False)
    shipping_idr: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    fee_idr: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_idr: Mapped[int] = mapped_column(BigInteger, nullable=False)

    shipping_address: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    payment_method_snapshot: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Network identity (constants in v1; ready for Beckn round-trip later).
    # Canonical subscriber_id (Task A3): beli-aman.bap.jaringan-dagang.id.
    bap_id: Mapped[str] = mapped_column(
        String(255), nullable=False, default="beli-aman.bap.jaringan-dagang.id"
    )
    bpp_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    seller_order_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Mock fulfillment timeline (driven by /internal-mock/* admin endpoints)
    shipped_simulated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_simulated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    auto_release_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    released_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Fulfillment state synced from seller's BPP via Beckn /on_status
    fulfillment_status: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, index=True
    )
    fulfillment_awb: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    fulfillment_tracking_url: Mapped[Optional[str]] = mapped_column(
        String(1024), nullable=True
    )
    fulfillment_last_event_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ONDC RSP settlement state (Task A6). Populated when the BAP
    # requests a settlement record via ``POST /api/v1/orders/{id}/settle``
    # and again when the BPP responds via /on_settle.
    # Values mirror network-extension/enums/rsp.yaml settlement_status
    # / settlement_basis / settlement_window keys.
    settlement_status: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True
    )
    settlement_basis: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True
    )
    settlement_window: Mapped[Optional[str]] = mapped_column(
        String(8), nullable=True
    )
    settlement_reference: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
