"""OrderRating — buyer's post-fulfillment rating of an order.

Task A6 (ONDC /rating, narrow): persists the rating set the buyer
submitted via ``POST /api/v1/orders/{order_id}/rating`` and tracks the
BPP's /on_rating acknowledgement. v1 is one-row-per-order (idempotent
re-submit overwrites); rating revisions / per-item drill-down are
deferred to v2.

Schema:
  - ``ratings`` is a JSONB array of ``{category, value, id?, comments?}``
    dicts (so we can carry mixed Item + Provider + Fulfillment ratings
    in one row without modeling each as a separate table).
  - ``acknowledged`` flips True on /on_rating receipt; the storefront can
    poll ``GET /api/v1/orders/{id}`` and surface "feedback received".
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class OrderRating(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "order_ratings"

    order_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("orders.id"),
        index=True,
        nullable=False,
        unique=True,            # one rating row per order
    )
    ratings: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False
    )
    acknowledged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
