"""PaymentMethod — stored payment options. v1 only seeds the BCA VA mock."""

import enum

from sqlalchemy import Boolean, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PaymentMethodType(str, enum.Enum):
    """Xendit-shaped payment-method buckets."""

    VIRTUAL_ACCOUNT = "virtual_account"
    EWALLET = "ewallet"
    QRIS = "qris"
    CARD = "card"
    RETAIL = "retail"


class PaymentMethod(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A saved payment method on a profile."""

    __tablename__ = "payment_methods"

    profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("profiles.id"), index=True, nullable=False
    )
    type: Mapped[PaymentMethodType] = mapped_column(
        Enum(PaymentMethodType, name="payment_method_type"), nullable=False
    )
    display_label: Mapped[str] = mapped_column(String(255), nullable=False)
    # For real Xendit later: tokenized card / wallet ref. Mocked in v1.
    xendit_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
