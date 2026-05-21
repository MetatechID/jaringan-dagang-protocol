"""OrderEvent — append-only audit log of state transitions."""

from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from .order import OrderState


class OrderEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per state transition. Append-only — never updated."""

    __tablename__ = "order_events"

    order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("orders.id"), index=True, nullable=False
    )
    from_state: Mapped[OrderState | None] = mapped_column(
        Enum(OrderState, name="order_state"), nullable=True
    )
    to_state: Mapped[OrderState] = mapped_column(
        Enum(OrderState, name="order_state"), nullable=False
    )
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
