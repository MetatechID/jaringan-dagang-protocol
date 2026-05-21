"""Storefront analytics event — per-version conversion tracking.

One row per buyer-side event (page_view / product_view / add_to_cart /
view_cart / checkout_start / etc.). Tagged with the deploy SHA so we can
roll up conversion rates per storefront version.

Lightweight by design: no joins on hot path, no PII (session ID only,
no email/phone), and a small set of indexed columns for funnel queries.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, BigInteger
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, UUIDPrimaryKeyMixin


class StorefrontEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "storefront_events"

    tenant_slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version_sha: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    client_ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    props: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_storefront_events_tenant_version", "tenant_slug", "version_sha"),
        Index("ix_storefront_events_session_event", "session_id", "event_name"),
    )

    def __repr__(self) -> str:
        return (
            f"<StorefrontEvent(id={self.id}, tenant={self.tenant_slug}, "
            f"version={self.version_sha[:8]}, event={self.event_name})>"
        )
