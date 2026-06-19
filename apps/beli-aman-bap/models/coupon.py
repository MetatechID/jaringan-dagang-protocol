"""Coupon + BuyerCoupon — vouchers a buyer can browse and claim.

v1 scope is list + claim only. Redemption-at-checkout is deferred, so there is
no FK enforcement onto orders here: ``BuyerCoupon.order_id`` is a plain nullable
String(36) that a future checkout-integration pass will populate.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Coupon(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A voucher definition. ``brand_slug`` null means it applies to all stores."""

    __tablename__ = "coupons"

    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    discount_type: Mapped[str] = mapped_column(String(16), nullable=False)
    discount_value: Mapped[int] = mapped_column(Integer, nullable=False)
    min_spend_idr: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    brand_slug: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    valid_from: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    valid_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    max_uses: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class BuyerCoupon(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A buyer's claim on a coupon. One row per (profile, coupon)."""

    __tablename__ = "buyer_coupons"
    __table_args__ = (
        UniqueConstraint("profile_id", "coupon_id", name="uq_buyer_coupons_profile_coupon"),
    )

    profile_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("profiles.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    coupon_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("coupons.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    claimed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    order_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
