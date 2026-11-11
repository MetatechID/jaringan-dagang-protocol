"""Partner exchange log: one row per HTTP exchange with a partner, either direction.

Backend-only, best-effort evidence written outside the business transaction;
no business foreign keys. ``error_class`` answers "was it us or the partner?"
(see ``jaringan_dagang_ops.exchanges``).
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, LargeBinary, String, false
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDPrimaryKeyMixin


class PartnerExchange(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "partner_exchanges"

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    direction: Mapped[str] = mapped_column(String(16))
    integration: Mapped[str] = mapped_column(String(32))
    operation: Mapped[str] = mapped_column(String(64))
    method: Mapped[str] = mapped_column(String(16))
    endpoint: Mapped[str] = mapped_column(String(2048))
    status: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(String(64))
    error_class: Mapped[str | None] = mapped_column(String(32), index=True)
    partner_code: Mapped[str | None] = mapped_column(String(64))
    attempt: Mapped[int | None] = mapped_column(Integer)
    delivery_id: Mapped[str | None] = mapped_column(String(36), index=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), index=True)
    span_id: Mapped[str | None] = mapped_column(String(16))
    job_id: Mapped[str | None] = mapped_column(String(36), index=True)
    brand_id: Mapped[str | None] = mapped_column(String(36), index=True)
    order_id: Mapped[str | None] = mapped_column(String(36), index=True)
    cart_id: Mapped[str | None] = mapped_column(String(36), index=True)
    partner_order_id: Mapped[str | None] = mapped_column(String(36), index=True)
    external_order_id: Mapped[str | None] = mapped_column(String(255), index=True)
    provider_ref: Mapped[str | None] = mapped_column(String(255), index=True)
    dipay_reference: Mapped[str | None] = mapped_column(String(255), index=True)
    redacted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    request: Mapped[dict] = mapped_column(JSONB)
    response: Mapped[dict] = mapped_column(JSONB)
    request_body_raw: Mapped[bytes | None] = mapped_column(LargeBinary)
    response_body_raw: Mapped[bytes | None] = mapped_column(LargeBinary)
    request_body_json: Mapped[object | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    response_body_json: Mapped[object | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
