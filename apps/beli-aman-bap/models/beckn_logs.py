"""Beckn message logs — inbound & outbound — for idempotency + audit."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class BecknInboundLog(Base):
    """Records every signed Beckn message received by the BAP.

    Used for both audit and idempotency: if message_id is already present,
    the middleware returns the cached response instead of re-executing.
    """

    __tablename__ = "beckn_inbound_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    message_id: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    transaction_id: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    bpp_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bap_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class BecknOutboundLog(Base):
    """Records every outbound Beckn request the BAP sends.

    Multiple rows per message_id when retries happen.
    """

    __tablename__ = "beckn_outbound_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    message_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    transaction_id: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    target_url: Mapped[str] = mapped_column(String(512), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    request_body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
