"""Mirror tables — hint-only catalog cache populated by Beckn /on_search.

The buyer storefront reads from these tables; they are NEVER authoritative
for price/stock decisions (those re-validate via Beckn /select + /init at
checkout).

Refresh paths:
  - PUSH (primary): seller emits /on_search on product writes; handler upserts here.
  - PULL (safety net): every 5 min a worker calls /search to each known BPP.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class MirrorStore(Base):
    __tablename__ = "mirror_stores"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    bpp_id: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    logo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(100), nullable=True)
    city: Mapped[str | None] = mapped_column(String(50), nullable=True)
    bpp_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Per-store CDN/origin base for resolving relative image URLs (Task A7
    # parity). The seller's catalog_builder prepends ``Store.image_base_url``
    # before emitting Beckn item images; the buyer keeps the resolved
    # absolute URLs in mirror_*_images today. This column is reserved for
    # any future cases where the buyer wants to re-derive a different base
    # (e.g. CDN swap) without re-pulling the catalog.
    image_base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_pulled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    catalog_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    products: Mapped[list["MirrorProduct"]] = relationship(
        back_populates="store", cascade="all, delete-orphan"
    )


class MirrorProduct(Base):
    __tablename__ = "mirror_products"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    store_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mirror_stores.id", ondelete="CASCADE"), index=True, nullable=False
    )
    bpp_product_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    sku: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)
    attributes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    store: Mapped["MirrorStore"] = relationship(back_populates="products")
    skus: Mapped[list["MirrorSKU"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    images: Mapped[list["MirrorProductImage"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class MirrorSKU(Base):
    __tablename__ = "mirror_skus"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mirror_products.id", ondelete="CASCADE"), index=True, nullable=False
    )
    bpp_sku_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    variant_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    variant_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sku_code: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    original_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stock: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    weight_grams: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    product: Mapped["MirrorProduct"] = relationship(back_populates="skus")
    images: Mapped[list["MirrorSKUImage"]] = relationship(
        back_populates="sku", cascade="all, delete-orphan"
    )


class MirrorProductImage(Base):
    __tablename__ = "mirror_product_images"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mirror_products.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    product: Mapped["MirrorProduct"] = relationship(back_populates="images")


class MirrorSKUImage(Base):
    __tablename__ = "mirror_sku_images"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    sku_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mirror_skus.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    sku: Mapped["MirrorSKU"] = relationship(back_populates="images")
