"""OtpCode — short-lived passwordless-login codes for WhatsApp + email channels.

Mirrors karya1's ``otp_codes`` table shape: one active row per (channel, contact),
sha256-hashed code (never store raw), 7-minute TTL, 5-attempt cap, 60s resend
rate-limit. A successful verify DELETES the row (single-use).
"""

from datetime import datetime
from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDPrimaryKeyMixin


class OtpCode(UUIDPrimaryKeyMixin, Base):
    """One pending OTP per (channel, contact). New issue overwrites the prior row."""

    __tablename__ = "otp_codes"
    __table_args__ = (
        UniqueConstraint("channel", "contact", name="uq_otp_codes_channel_contact"),
    )

    channel: Mapped[str] = mapped_column(String(16), nullable=False)  # "wa" | "email"
    contact: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256 hex
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
