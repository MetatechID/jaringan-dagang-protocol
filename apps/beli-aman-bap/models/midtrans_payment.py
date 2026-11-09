"""Durable Midtrans link intent and its individual payment attempts."""
from datetime import datetime
from sqlalchemy import String, Integer, ForeignKey, DateTime, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base, UUIDPrimaryKeyMixin, TimestampMixin


class MidtransPaymentLink(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "midtrans_payment_links"
    __table_args__ = (
        CheckConstraint("(order_id IS NULL) <> (cart_id IS NULL)", name="midtrans_one_owner"),
        CheckConstraint("amount_idr > 0", name="midtrans_positive_amount"),
    )
    brand_id: Mapped[str] = mapped_column(ForeignKey("brands.id"), index=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"), unique=True)
    cart_id: Mapped[str | None] = mapped_column(ForeignKey("bot_carts.id"), unique=True)
    base_reference: Mapped[str] = mapped_column(String(50), unique=True)
    amount_idr: Mapped[int] = mapped_column(Integer)
    environment: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), default="creating")
    invoice_url: Mapped[str | None] = mapped_column(String(1024))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    paid_attempt_id: Mapped[str | None] = mapped_column(String(128))


class MidtransPaymentAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "midtrans_payment_attempts"
    link_id: Mapped[str] = mapped_column(ForeignKey("midtrans_payment_links.id"), index=True)
    attempt_reference: Mapped[str] = mapped_column(String(128), unique=True)
    transaction_id: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(32))
