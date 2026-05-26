"""StorefrontIntegration — per-tenant marketing/analytics tags.

One row per storefront slug. Holds opaque third-party IDs the storefront
injects on every page (Google Analytics measurement ID, Facebook Pixel ID).
Edited from the buyer-side Vibe admin (`/<slug>/admin`); read by the
storefront layout to render `<script>` tags.
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class StorefrontIntegration(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "storefront_integrations"

    tenant_slug: Mapped[str] = mapped_column(
        String(100), unique=True, index=True, nullable=False
    )
    ga_measurement_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fb_pixel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Server-only secrets — NEVER returned by the public GET endpoint that
    # the storefront layout uses to fetch fb_pixel_id. Read directly from
    # the DB by services/fb_capi.py to send server-side Purchase events to
    # Meta's Conversions API on ESCROW_HELD.
    fb_capi_access_token: Mapped[str | None] = mapped_column(String(512), nullable=True)
    fb_capi_test_event_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
