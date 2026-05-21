"""Beckn Registry Service.

A lightweight registry for managing Beckn network participants (BAP, BPP, BG).
Provides subscription and lookup APIs so that gateways and participants can
discover each other.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import async_session, close_db, init_db
from models import Subscriber
from schemas import (
    HealthResponse,
    LookupRequest,
    LookupResponse,
    SubscribeRequest,
    SubscribeResponse,
    SubscriberResponse,
)

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("registry")

# Redis client — initialized on startup
redis_client: aioredis.Redis | None = None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    global redis_client

    logger.info("Starting Beckn Registry service")
    logger.info("Initializing database tables")
    try:
        await init_db()
    except Exception:
        logger.warning("Could not connect to database (skipping table creation)", exc_info=True)

    logger.info("Connecting to Redis at %s", settings.redis_url)
    try:
        redis_client = aioredis.from_url(
            settings.redis_url, decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        await redis_client.ping()
        logger.info("Redis connection established")
    except Exception:
        logger.warning("Redis unavailable — lookup caching disabled")
        redis_client = None

    yield

    logger.info("Shutting down Beckn Registry service")
    try:
        if redis_client:
            await redis_client.close()
        await close_db()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Beckn Registry",
    description="Jaringan Dagang — Beckn network participant registry",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

async def get_db() -> AsyncSession:
    """Yield a database session."""
    async with async_session() as session:
        yield session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cache_key(req: LookupRequest) -> str:
    """Build a deterministic Redis key for a lookup query."""
    parts = [
        f"sid:{req.subscriber_id or ''}",
        f"type:{req.type.value if req.type else ''}",
        f"dom:{req.domain or ''}",
        f"city:{req.city or ''}",
    ]
    return "registry:lookup:" + "|".join(parts)


async def _invalidate_cache() -> None:
    """Drop all cached lookups (called after mutations)."""
    if redis_client is None:
        return
    try:
        cursor = "0"
        while cursor:
            cursor, keys = await redis_client.scan(
                cursor=cursor, match="registry:lookup:*", count=100,
            )
            if keys:
                await redis_client.delete(*keys)
    except Exception as exc:
        logger.warning("Cache invalidation failed: %s", exc)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/subscribe", response_model=SubscribeResponse, status_code=201)
async def subscribe(body: SubscribeRequest, db: AsyncSession = Depends(get_db)):
    """Register a new network participant.

    In this simplified v1 the subscriber is automatically moved from
    INITIATED to SUBSCRIBED — no challenge/response handshake.
    """
    # Check for duplicate subscriber_id
    existing = await db.execute(
        select(Subscriber).where(Subscriber.subscriber_id == body.subscriber_id)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Subscriber '{body.subscriber_id}' already registered",
        )

    now = datetime.now(timezone.utc)
    subscriber = Subscriber(
        subscriber_id=body.subscriber_id,
        subscriber_url=body.subscriber_url,
        type=body.type.value,
        domain=body.domain,
        city=body.city,
        signing_public_key=body.signing_public_key,
        encryption_public_key=body.encryption_public_key,
        status="SUBSCRIBED",  # auto-approve in v1
        valid_from=now,
        valid_until=now + timedelta(days=365),
        created_at=now,
        updated_at=now,
    )
    db.add(subscriber)
    await db.commit()
    await db.refresh(subscriber)

    logger.info(
        "Registered subscriber %s (%s) for domain=%s city=%s",
        subscriber.subscriber_id,
        subscriber.type,
        subscriber.domain,
        subscriber.city,
    )

    await _invalidate_cache()

    return SubscribeResponse(
        subscriber=SubscriberResponse.model_validate(subscriber),
    )


@app.post("/lookup", response_model=LookupResponse)
async def lookup(body: LookupRequest, db: AsyncSession = Depends(get_db)):
    """Look up network participants by optional filters."""
    # Try cache first
    cache_key = _cache_key(body)
    if redis_client:
        try:
            cached = await redis_client.get(cache_key)
            if cached:
                data = json.loads(cached)
                logger.debug("Cache hit for %s", cache_key)
                return LookupResponse(**data)
        except Exception as exc:
            logger.warning("Cache read error: %s", exc)

    # Build query
    stmt = select(Subscriber).where(Subscriber.status == "SUBSCRIBED")
    if body.subscriber_id:
        stmt = stmt.where(Subscriber.subscriber_id == body.subscriber_id)
    if body.type:
        stmt = stmt.where(Subscriber.type == body.type.value)
    if body.domain:
        stmt = stmt.where(Subscriber.domain == body.domain)
    if body.city:
        stmt = stmt.where(Subscriber.city == body.city)

    result = await db.execute(stmt)
    subscribers = result.scalars().all()

    response = LookupResponse(
        count=len(subscribers),
        subscribers=[SubscriberResponse.model_validate(s) for s in subscribers],
    )

    # Populate cache
    if redis_client:
        try:
            await redis_client.set(
                cache_key,
                response.model_dump_json(),
                ex=settings.cache_ttl,
            )
        except Exception as exc:
            logger.warning("Cache write error: %s", exc)

    return response


@app.get("/subscribers", response_model=list[SubscriberResponse])
async def list_subscribers(db: AsyncSession = Depends(get_db)):
    """Admin: list all subscribers regardless of status."""
    result = await db.execute(select(Subscriber).order_by(Subscriber.created_at.desc()))
    return [SubscriberResponse.model_validate(s) for s in result.scalars().all()]


@app.delete("/subscribers/{subscriber_id}", status_code=204)
async def delete_subscriber(subscriber_id: str, db: AsyncSession = Depends(get_db)):
    """Admin: remove a subscriber by subscriber_id."""
    result = await db.execute(
        delete(Subscriber).where(Subscriber.subscriber_id == subscriber_id)
    )
    await db.commit()

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Subscriber not found")

    logger.info("Deleted subscriber %s", subscriber_id)
    await _invalidate_cache()


@app.get("/health", response_model=HealthResponse)
async def health():
    """Health check endpoint."""
    return HealthResponse()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
