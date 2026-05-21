"""Brand — a partner brand (BPP) using Beli Aman.

In v1 we seed three brands: antarestar, gendes, yourbrand.
"""

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Brand(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A partner brand with its own storefront and (eventually) its own BPP."""

    __tablename__ = "brands"

    slug: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    bpp_id: Mapped[str] = mapped_column(String(255), nullable=False)
    bpp_uri: Mapped[str | None] = mapped_column(String(255), nullable=True)
    default_warehouse_address: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    fee_pct_bp: Mapped[int] = mapped_column(default=0, nullable=False)  # 0 in v1
