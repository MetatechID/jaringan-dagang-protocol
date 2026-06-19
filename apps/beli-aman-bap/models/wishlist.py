"""WishlistItem — a buyer's saved product, scoped to one profile per SKU."""

from typing import Optional

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class WishlistItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A product a Beli Aman profile has wishlisted. One row per (profile, sku)."""

    __tablename__ = "wishlist_items"
    __table_args__ = (
        UniqueConstraint("profile_id", "sku", name="uq_wishlist_items_profile_sku"),
    )

    profile_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("profiles.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    brand_slug: Mapped[str] = mapped_column(String(120), nullable=False)
    sku: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    price_idr: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    image: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
