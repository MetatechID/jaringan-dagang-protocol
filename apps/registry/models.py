"""SQLAlchemy models for the Beckn Registry."""

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class Subscriber(Base):
    """A registered Beckn network participant (BAP, BPP, or BG)."""

    __tablename__ = "subscribers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    subscriber_id: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True,
        comment="Unique identifier for the subscriber (e.g. 'bap.jaringan.id')",
    )
    subscriber_url: Mapped[str] = mapped_column(
        String(512), nullable=False,
        comment="Base URL for the subscriber's Beckn API",
    )
    type: Mapped[str] = mapped_column(
        String(10), nullable=False,
        comment="Participant type: BAP, BPP, or BG",
    )
    domain: Mapped[str] = mapped_column(
        String(100), nullable=False,
        comment="Beckn domain code (e.g. 'ONDC:RET10', 'nic2004:52110')",
    )
    city: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="City code (e.g. 'ID:JKT', 'std:080')",
    )
    signing_public_key: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="Ed25519 public key for request signing verification",
    )
    encryption_public_key: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="X25519 public key for payload encryption",
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="INITIATED",
        comment="Subscription status: INITIATED, SUBSCRIBED, INVALID_SSL, UNSUBSCRIBED",
    )
    valid_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="Start of subscription validity period",
    )
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="End of subscription validity period",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_subscribers_type_domain_city", "type", "domain", "city"),
    )

    def __repr__(self) -> str:
        return f"<Subscriber {self.subscriber_id} ({self.type}) [{self.status}]>"
