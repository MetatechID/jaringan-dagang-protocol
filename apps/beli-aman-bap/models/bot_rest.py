"""Bot-REST session + cart models (Task B3a).

Ephemeral state for the Beckn ``search → select → init → confirm`` REST
surface exposed to the B3 jd-sell MCP bot. The buyer storefront does NOT
use these tables — it persists orders directly via ``models.order.Order``
once /confirm-payment fires.

Rationale: the bot's UX is "find products, build a cart, get a payment QR
back". That intermediate state has no home in the existing models. Rather
than overload ``Order`` with PRE_AUTH-pre-PRE_AUTH lifecycle (drafts that
may never become orders), we keep a small dedicated pair:

- ``SearchSession``  — one row per ``POST /api/v1/search`` call. Holds
  the transaction_id used for the Beckn /search envelope so that the
  ``/on_search`` callback can correlate results back. TTL ~30 min.
- ``Cart``           — one row per ``POST /api/v1/cart/select`` call. Holds
  the items + quote + billing/shipping captured during the /select →
  /init → /confirm dance. Becomes an Order once /confirm completes (the
  ``order_id`` FK points at the resulting row). TTL ~30 min if not
  confirmed.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


# Search sessions are short-lived catalog caches; 30 min is plenty.
_DEFAULT_SESSION_TTL = timedelta(minutes=30)
# Carts MUST live longer than a typical chat-then-pay flow. Customer
# may search products, add a cart, then come back hours later to pay.
# 30 min was causing carts to expire mid-conversation and the rendered
# receipt PNG to flip to empty/Rp0 (BAP returned 410 → next.js render
# served the "(keranjang masih kosong)" fallback). 7 days survives a
# weekend; abandoned ones get swept by future housekeeping.
_DEFAULT_CART_TTL = timedelta(days=7)


def _default_expires_at() -> datetime:
    return datetime.now(timezone.utc) + _DEFAULT_SESSION_TTL


def _default_cart_expires_at() -> datetime:
    return datetime.now(timezone.utc) + _DEFAULT_CART_TTL


class SearchSessionStatus(str, enum.Enum):
    PENDING = "pending"
    RESULTS = "results"
    EXPIRED = "expired"


class CartStatus(str, enum.Enum):
    OPEN = "open"
    QUOTED = "quoted"
    DRAFTED = "drafted"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"


class SearchSession(Base):
    """One ``POST /api/v1/search`` call.

    The Beckn /search ACK is fast but the actual catalog arrives via
    one or more /on_search callbacks on a separate connection. We store
    the transaction_id so the bot can poll ``GET /api/v1/search/{id}/results``
    and we can pull accumulated MirrorProduct rows for that BPP.
    """

    __tablename__ = "bot_search_sessions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # The bot does NOT authenticate as a Firebase customer; this column is
    # reserved so that a future "bot-on-behalf-of-customer" mode can attach
    # the BeliAmanProfile.id. NULL = pure-bot session.
    customer_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True
    )
    query: Mapped[str] = mapped_column(String(500), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    city: Mapped[str] = mapped_column(String(50), nullable=False, default="std:021")
    status: Mapped[SearchSessionStatus] = mapped_column(
        Enum(SearchSessionStatus, name="bot_search_session_status"),
        nullable=False,
        default=SearchSessionStatus.PENDING,
        index=True,
    )
    transaction_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    bpp_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Optional snapshot of /on_search results — populated when the bot polls
    # for results, frozen at first poll so the bot sees a consistent view.
    results_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_default_expires_at,
    )


class Cart(Base):
    """One ``POST /api/v1/cart/select`` call.

    Carries Beckn /select → /init → /confirm progress. The bot mutates it
    via ``POST /cart/{id}/init`` (attaches billing/shipping) then
    ``POST /checkout/{cart_id}/confirm`` (creates the Order).

    TTL: 7 days (see ``_default_cart_expires_at``). Long enough to survive
    a chat-then-pay-later flow over a weekend.
    """

    __tablename__ = "bot_carts"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    customer_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True
    )
    search_session_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("bot_search_sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    bpp_id: Mapped[str] = mapped_column(String(255), nullable=False)
    bpp_uri: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    provider_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Items shaped as [{"sku_id": str, "qty": int}, ...]
    items_json: Mapped[list] = mapped_column(JSON, nullable=False)
    # /on_init response payload (or a digest) — opaque to the BAP, surfaced
    # back to the bot via GET /cart/{id}.
    quote_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    quote_token: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[CartStatus] = mapped_column(
        Enum(CartStatus, name="bot_cart_status"),
        nullable=False,
        default=CartStatus.OPEN,
        index=True,
    )
    transaction_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    billing_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    shipping_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Synthetic bot-side order id minted at /confirm time. No FK to
    # ``orders.id`` because the bot doesn't materialize a ``models.order.Order``
    # row (it has no Firebase profile_id). Keeping the column name stable so
    # the REST response shape doesn't churn for the B3 MCP consumer.
    order_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        nullable=True,
        index=True,
    )
    # Surfaced to the bot post-confirm — extracted from /on_confirm.
    # In v2 this points to the Xendit hosted-invoice URL (works as both a
    # QR-bearing checkout page and a direct-pay URL — Xendit's hosted page
    # renders QRIS + VA + e-wallet + retail on one screen).
    qr_image_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    payment_state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )
    # Xendit invoice id (PSP-side primary key on the hosted-invoice resource).
    # Set when the BAP creates the invoice; looked up by the
    # ``invoice.paid`` / ``invoice.expired`` webhook to find the cart.
    xendit_invoice_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    # 7-day TTL (vs SearchSession's 30 min) — see _default_cart_expires_at.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_default_cart_expires_at,
    )
