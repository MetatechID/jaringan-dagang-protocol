"""Address — multiple shipping addresses per profile."""

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Address(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A shipping address belonging to a Beli Aman profile."""

    __tablename__ = "addresses"

    profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("profiles.id"), index=True, nullable=False
    )
    recipient_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False)
    line1: Mapped[str] = mapped_column(String(255), nullable=False)
    line2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kelurahan: Mapped[str | None] = mapped_column(String(120), nullable=True)
    kecamatan: Mapped[str | None] = mapped_column(String(120), nullable=True)
    kota: Mapped[str] = mapped_column(String(120), nullable=False)
    provinsi: Mapped[str] = mapped_column(String(120), nullable=False)
    postal_code: Mapped[str] = mapped_column(String(10), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
