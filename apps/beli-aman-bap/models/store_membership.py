"""StoreMembership — the network-wide ACL, owned by Beli Aman (the IdP).

Beli Aman is the identity provider for the whole Jaringan Dagang network.
A person signs in once ("Sign in with Beli Aman"); their permissions on each
toko are these rows. Both the seller dashboard and the buyer-side Vibe editor
resolve "what can this person do for store X" from here.

Roles (2-level):
  owner — full control: products, orders, refunds, settings, team management
  staff — operational: products, orders, refunds (no team management)

Pending invite: profile_id NULL, keyed by invited_email. On that person's
first Beli Aman sign-in the row auto-links to their profile.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class StoreRole(str, enum.Enum):
    OWNER = "owner"
    STAFF = "staff"


class StoreMembership(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "store_memberships"

    profile_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("profiles.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    invited_email: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    # The toko's store id as known by the seller catalog service (seller_db
    # Store.id, a UUID string). Beli Aman doesn't own the catalog — just the
    # permission mapping — so this is a loose reference, not an FK.
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # The toko's storefront slug (e.g. "safiyafood"). The buyer Vibe admin keys
    # by slug; the seller dashboard keys by store_id. Both query the same row.
    store_slug: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, index=True
    )
    role: Mapped[StoreRole] = mapped_column(
        SAEnum(StoreRole, name="beliaman_store_role", create_constraint=True),
        nullable=False,
        default=StoreRole.STAFF,
    )
    invited_by_email: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    accepted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return f"<StoreMembership(email={self.invited_email!r}, store={self.store_id}, role={self.role})>"
