"""BeliAmanProfile — the consumer's identity, materialized from one of three sign-in methods.

Sign-in methods (all mint a Firebase ID token at the SDK layer):
  - Google SSO via Firebase (sets google_sub).
  - WhatsApp OTP, code delivered via the jd-wa-whatsmeow sidecar (sets phone_e164).
  - Email OTP, code delivered via SMTP (sets email).

A single human gets ONE profile row: the BAP auto-merges on verified contact
match (see ``deps._get_or_create_profile_by_contact``). google_sub, email, and
phone_e164 are each UNIQUE-but-nullable so any method can be the first one
used and the other columns get filled in later when that contact is verified.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class BeliAmanProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A person's identity. Canonical for the whole Jaringan Dagang network."""

    __tablename__ = "profiles"

    google_sub: Mapped[Optional[str]] = mapped_column(
        String(255), unique=True, index=True, nullable=True
    )
    email: Mapped[Optional[str]] = mapped_column(
        String(255), unique=True, index=True, nullable=True
    )
    phone_e164: Mapped[Optional[str]] = mapped_column(
        String(20), unique=True, index=True, nullable=True
    )
    display_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    photo_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Network-wide super admin — bypasses all StoreMembership checks.
    is_super_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
