"""Beli Aman BAP service — buyer-protection layer for Indonesian DTC commerce.

This is a sibling to the JD BAP (../app/main.py). It exposes:
  - REST endpoints used by the Beli Aman SDK (auth, brands, orders, payments,
    escrow, disputes)
  - Admin-only mock endpoints used by the demo cockpit at /admin?token=...

For v1 it does NOT speak Beckn on the network — it's wired only to a local
seller_bridge that posts orders to the seller dashboard. The directory
structure mirrors the JD BAP so future Beckn round-trips drop in cleanly.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Make this app's own modules importable as top-level (config, database, etc.)
_app_dir = Path(__file__).resolve().parent
if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))

# The ``beckn_protocol`` package next to this file is a vendored copy of
# ``packages/beckn-protocol/python/`` — kept in-tree because the Vercel
# project's rootDirectory=apps/beli-aman-bap means anything outside this
# directory is not uploaded to the function image. No external path
# bootstrap is needed.

# Import the dedicated canonical-failure exception type so the defensive
# router blocks below can DISCRIMINATE "canonical Beckn-protocol package
# missing/broken → fail LOUD" from "a router's genuinely-optional
# third-party dep missing → degrade gracefully". We guard this import
# itself: if even the class isn't importable, the shim is so broken that
# we want the resulting traceback to surface (fail loud is the desired
# behaviour for that case, exactly as for any canonical-pkg failure).
from beckn_protocol import BecknProtocolUnavailable  # noqa: E402


def _should_reraise_canonical_failure(exc: BaseException) -> bool:
    """Return True iff ``exc`` is — or wraps — a ``BecknProtocolUnavailable``.

    The defensive router-import blocks below catch ``Exception`` broadly
    so that a missing/broken **third-party** dep of those routers does
    NOT take the whole BAP offline. But the canonical Beckn-protocol
    package is mandatory: when it can't be imported, the shim raises
    :class:`BecknProtocolUnavailable`. That MUST propagate. A router
    can also wrap the canonical failure by raising its own
    :class:`ImportError`, in which case the canonical failure shows up
    in ``__cause__`` (explicit ``raise X from Y``) or ``__context__``
    (implicit chaining); we walk both chains.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, BecknProtocolUnavailable):
            return True
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return False

from config import settings  # noqa: E402
from database import engine  # noqa: E402
from deps import require_admin_token  # noqa: E402
from models import Base  # noqa: E402  (import side-effect: registers all models)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup / shutdown hooks."""
    import asyncio
    import os
    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info(
        "Starting %s (subscriber_id=%s) at %s",
        settings.service_name, settings.subscriber_id, settings.subscriber_url,
    )

    # SKIP_CREATE_ALL=true in serverless (Vercel) — calling
    # Base.metadata.create_all here is the wrong pattern for serverless: it
    # runs on EVERY cold start, holds an engine.begin() transaction while it
    # iterates every Table, and fails noisily when ANY single FK type drifts
    # from live (e.g. order_events.order_id VARCHAR vs orders.id UUID). The
    # exception IS caught here, but Neon's rollback + the 10+ KB traceback
    # log emitted on EVERY cold start has been blowing the Vercel cold-start
    # budget and surfacing as FUNCTION_INVOCATION_FAILED on every request.
    # Prod schema migrations are owned by scripts/add-*.py (idempotent,
    # dry-run-default, operator runs explicitly). Keep create_all available
    # for local/CI where SKIP_CREATE_ALL is unset.
    if os.environ.get("SKIP_CREATE_ALL", "false").lower() != "true":
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info("Database tables ensured")
        except Exception:
            logger.warning("Could not connect to database (skipping table creation)", exc_info=True)
    else:
        logger.info("SKIP_CREATE_ALL=true — relying on migration scripts for schema")

    # Start background workers (unless explicitly disabled, e.g. on Vercel serverless).
    puller_task = None
    if os.environ.get("BECKN_WORKERS_ENABLED", "true").lower() != "false":
        try:
            from workers.catalog_puller import run_forever as catalog_puller_loop
            puller_task = asyncio.create_task(catalog_puller_loop())
            logger.info("catalog_puller worker started")
        except Exception:
            logger.exception("catalog_puller failed to start")

    yield

    if puller_task is not None:
        puller_task.cancel()
    try:
        await engine.dispose()
    except Exception:
        pass
    logger.info("%s shut down", settings.service_name)


app = FastAPI(
    title="Beli Aman BAP",
    description=(
        "Buyer-protection layer for Indonesian DTC commerce. "
        "Built on the Jaringan Dagang Beckn-protocol open commerce network."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


_origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
# Plus a regex so every tenant subdomain on beliaman.com (safiya, gendes,
# antarestar, etc.), metatech.id, and the consolidated jaringandagang.com is
# allowed without us re-listing each.
_origin_regex = r"https://([a-z0-9-]+\.)?(beliaman\.com|metatech\.id|jaringandagang\.com)"
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_origin_regex=_origin_regex,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Admin-Token"],
)


# --- Mount routers ---
from routers.auth import router as auth_router  # noqa: E402
from routers.profiles import router as profiles_router  # noqa: E402
from routers.brands import router as brands_router  # noqa: E402
from routers.orders import router as orders_router  # noqa: E402
from routers.payments import router as payments_router  # noqa: E402
from routers.escrow import router as escrow_router  # noqa: E402
from routers.disputes import router as disputes_router  # noqa: E402
from routers.igm import router as igm_router  # noqa: E402
from routers.rsp_rating import router as rsp_rating_router  # noqa: E402
from routers.internal_mock import router as internal_mock_router  # noqa: E402
from routers.shipping import router as shipping_router  # noqa: E402
from routers.analytics import router as analytics_router  # noqa: E402
from routers.storefront_integrations import router as storefront_integrations_router  # noqa: E402
from routers.webhooks_xendit import router as webhooks_xendit_router  # noqa: E402
from routers.webhooks_biteship import router as webhooks_biteship_router  # noqa: E402
from routers.webhooks_jubelio import router as webhooks_jubelio_router  # noqa: E402
from routers.wishlist import router as wishlist_router  # noqa: E402
from routers.loyalty import router as loyalty_router  # noqa: E402
from routers.coupons import router as coupons_router  # noqa: E402

# Identity + Beckn routers import third-party deps. Import each defensively so
# a missing/broken OPTIONAL THIRD-PARTY dep in either can NEVER crash the
# whole BAP (which would take the entire buyer API offline). The canonical
# beckn-protocol package is a DIFFERENT story: if it's missing/broken the
# shim raises BecknProtocolUnavailable, which the discriminator below
# re-raises — silent 404s on every Beckn route would be a worse failure mode
# than a loud boot crash. Degraded boots ARE still logged loudly (error, not
# warning) so they aren't lost in the noise.
try:
    from routers.identity import router as identity_router  # noqa: E402
    _IDENTITY_ROUTER_AVAILABLE = True
except Exception as _ierr:  # noqa: BLE001
    if _should_reraise_canonical_failure(_ierr):
        raise
    identity_router = None  # type: ignore
    _IDENTITY_ROUTER_AVAILABLE = False
    logger.error("Identity router unavailable: %r", _ierr)

try:
    from routers.beckn import router as beckn_router  # noqa: E402
    _BECKN_ROUTER_AVAILABLE = True
except Exception as _err:  # noqa: BLE001
    if _should_reraise_canonical_failure(_err):
        raise
    beckn_router = None  # type: ignore
    _BECKN_ROUTER_AVAILABLE = False
    logger.error("Beckn router unavailable: %r", _err)

# Bot-facing REST surface (Task B3a) — search / cart / checkout, gated by
# Authorization: Bearer <BOT_API_TOKEN>. Mounted defensively for the same
# reason as identity/beckn above: an optional dep gone missing must NEVER
# take the BAP offline, but a canonical-pkg failure (BecknProtocolUnavailable)
# MUST still propagate.
try:
    from routers.search import router as bot_search_router  # noqa: E402
    _BOT_SEARCH_ROUTER_AVAILABLE = True
except Exception as _bsr_err:  # noqa: BLE001
    if _should_reraise_canonical_failure(_bsr_err):
        raise
    bot_search_router = None  # type: ignore
    _BOT_SEARCH_ROUTER_AVAILABLE = False
    logger.error("Bot search router unavailable: %r", _bsr_err)

try:
    from routers.cart import router as bot_cart_router  # noqa: E402
    _BOT_CART_ROUTER_AVAILABLE = True
except Exception as _bcr_err:  # noqa: BLE001
    if _should_reraise_canonical_failure(_bcr_err):
        raise
    bot_cart_router = None  # type: ignore
    _BOT_CART_ROUTER_AVAILABLE = False
    logger.error("Bot cart router unavailable: %r", _bcr_err)

try:
    from routers.checkout import router as bot_checkout_router  # noqa: E402
    _BOT_CHECKOUT_ROUTER_AVAILABLE = True
except Exception as _bckr_err:  # noqa: BLE001
    if _should_reraise_canonical_failure(_bckr_err):
        raise
    bot_checkout_router = None  # type: ignore
    _BOT_CHECKOUT_ROUTER_AVAILABLE = False
    logger.error("Bot checkout router unavailable: %r", _bckr_err)

app.include_router(auth_router)
app.include_router(profiles_router)
app.include_router(brands_router)
app.include_router(orders_router)
app.include_router(payments_router)
app.include_router(escrow_router)
app.include_router(disputes_router)
app.include_router(igm_router)
app.include_router(rsp_rating_router)
app.include_router(internal_mock_router)
app.include_router(shipping_router)
app.include_router(analytics_router)
app.include_router(storefront_integrations_router)
app.include_router(webhooks_xendit_router)
app.include_router(webhooks_biteship_router)
app.include_router(webhooks_jubelio_router)
app.include_router(wishlist_router)
app.include_router(loyalty_router)
app.include_router(coupons_router)
if _IDENTITY_ROUTER_AVAILABLE and identity_router is not None:
    app.include_router(identity_router)
if _BECKN_ROUTER_AVAILABLE and beckn_router is not None:
    app.include_router(beckn_router)
if _BOT_SEARCH_ROUTER_AVAILABLE and bot_search_router is not None:
    app.include_router(bot_search_router, prefix="/api/v1")
if _BOT_CART_ROUTER_AVAILABLE and bot_cart_router is not None:
    app.include_router(bot_cart_router, prefix="/api/v1")
if _BOT_CHECKOUT_ROUTER_AVAILABLE and bot_checkout_router is not None:
    app.include_router(bot_checkout_router, prefix="/api/v1")

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name}


@app.get("/debug/config", dependencies=[Depends(require_admin_token)])
async def debug_config() -> dict:
    # Admin-token-gated. Never echoes secrets — only flags presence.
    return {
        "subscriber_id": settings.subscriber_id,
        "subscriber_url": settings.subscriber_url,
        "database_configured": bool(settings.database_url),
        "allowed_origins": _origins,
        "seller_bridge_enabled": settings.seller_bridge_enabled,
        "auto_release_days": settings.auto_release_days,
        "xendit_configured": bool(settings.xendit_secret_key),
        "xendit_webhook_configured": bool(settings.xendit_webhook_token),
        "biteship_configured": bool(settings.biteship_api_key),
        "biteship_webhook_configured": bool(settings.biteship_webhook_token),
        "environment": settings.environment,
    }
