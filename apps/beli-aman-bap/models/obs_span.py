"""OTel-shaped spans for the BAP (same shape as the seller's ``bot_events``).

Written best-effort on a separate small pool; ``store_id`` and other keys
without a column stay in ``attributes``.
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDPrimaryKeyMixin


class ObsSpan(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "obs_spans"

    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    span_id: Mapped[str] = mapped_column(String(16))
    parent_span_id: Mapped[str | None] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16))
    service: Mapped[str] = mapped_column(String(32))
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text)
    order_id: Mapped[str | None] = mapped_column(String(36), index=True)
    cart_id: Mapped[str | None] = mapped_column(String(36))
    partner_order_id: Mapped[str | None] = mapped_column(String(36), index=True)
    brand_id: Mapped[str | None] = mapped_column(String(36), index=True)
    provider_ref: Mapped[str | None] = mapped_column(String(255))
    job_id: Mapped[str | None] = mapped_column(String(36))
    attributes: Mapped[dict] = mapped_column(JSONB)
