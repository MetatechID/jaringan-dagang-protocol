"""Dispute — buyer-initiated dispute on an order."""

import enum

from sqlalchemy import Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DisputeReason(str, enum.Enum):
    NOT_RECEIVED = "not_received"
    WRONG_ITEM = "wrong_item"
    DAMAGED = "damaged"
    OTHER = "other"


class DisputeStatus(str, enum.Enum):
    OPEN = "open"
    BRAND_RESPONDING = "brand_responding"
    OPS_REVIEW = "ops_review"
    RESOLVED = "resolved"


class Dispute(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "disputes"

    order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("orders.id"), index=True, nullable=False
    )
    opened_by: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[DisputeReason] = mapped_column(
        Enum(DisputeReason, name="dispute_reason"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    brand_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[DisputeStatus] = mapped_column(
        Enum(DisputeStatus, name="dispute_status"),
        nullable=False,
        default=DisputeStatus.OPEN,
    )
    resolution: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Correlation to seller's RefundRequest via Beckn /on_update tags.
    bpp_refund_request_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    # ONDC IGM (Task A5) — when the dispute is raised via /issue, this is
    # the IGM Issue id assigned at BAP side and echoed by the BPP in
    # /on_issue. ``bpp_resolution_note`` carries the BPP's short_desc /
    # long_desc on RESOLVED / REJECTED responses.
    bpp_issue_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    bpp_resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
