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

    # Fulfillment timeline. ``shipped_at`` is set when the seller books a
    # Biteship shipment via POST /orders/{id}/ship; ``delivered_at`` is set
    # by the Biteship tracking webhook on a "delivered" event.
    # ``auto_release_at`` is computed from ``delivered_at +
    # settings.auto_release_days`` (Jakarta-tz end-of-day) by
    # services/release_clock.py.
    shipped_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    auto_release_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    released_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Biteship's internal shipment order id (returned by POST /v1/orders).
    # We hit GET /v1/orders/{id} with this and Biteship webhooks deliver
    # this in the ``order.id`` payload field.
    biteship_order_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )

    # Which carrier this shipment was booked with ("biteship" | "jubelio").
    # Null until a shipment is booked. Keeps fulfillment_* generic so either
    # carrier's webhook can drive the same columns.
    carrier: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # Jubelio's internal shipment id (returned by POST /shipments/create).
    # Jubelio webhooks deliver it as ``shipment_id``; parallel to
    # biteship_order_id.
    jubelio_shipment_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
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

    # Snapshot of ad-attribution data captured by the storefront when the
    # order was created (or backfilled via POST /orders/{id}/attribution).
    # Read by services/fb_capi.py to send a server-side Purchase event to
    # Meta's Conversions API on ESCROW_HELD, which gives FB enough data to
    # credit the order back to the originating ad — even when the browser
    # Pixel event is lost to ad blockers / iOS ITP.
    #
    # Shape (all fields optional):
    #   {
    #     "fbc": "fb.1.<ts>.<fbclid>",
    #     "fbp": "fb.1.<ts>.<random>",
    #     "fbclid": "<id>",
    #     "user_agent": "<UA>",
    #     "ip": "<v4 or v6>",
    #     "landing_url": "https://safiya.beliaman.com/?fbclid=...",
    #     "ctwa_clid": "<click-to-WhatsApp id, if WA-originated>"
    #   }
    attribution: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
