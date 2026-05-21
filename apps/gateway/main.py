"""Beckn Gateway Service.

A lightweight gateway that receives search requests from BAPs and multicasts
them to all relevant BPPs registered for the matching domain and city.

Only the /search action is routed through the gateway. All subsequent actions
(select, init, confirm, etc.) flow directly from BAP to BPP using the
bpp_uri discovered during search.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config import settings

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("gateway")

# Module-level clients — set up during lifespan
http_client: httpx.AsyncClient | None = None
redis_client: aioredis.Redis | None = None


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class BecknAck(BaseModel):
    """Beckn ACK / NACK response."""
    status: str = "ACK"


class BecknMessageAck(BaseModel):
    """Standard Beckn acknowledgement wrapper."""
    message: dict = Field(default_factory=lambda: {"ack": {"status": "ACK"}})


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = "ok"
    service: str = "beckn-gateway"
    version: str = "0.1.0"


class RegistrySubscriber(BaseModel):
    """Minimal subscriber shape from registry lookup."""
    subscriber_id: str
    subscriber_url: str
    type: str
    domain: str
    city: str


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    global http_client, redis_client

    logger.info("Starting Beckn Gateway service")
    logger.info("Registry URL: %s", settings.registry_url)

    http_client = httpx.AsyncClient(timeout=settings.bpp_timeout)

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
        logger.warning("Redis unavailable — BPP lookup caching disabled")
        redis_client = None

    yield

    logger.info("Shutting down Beckn Gateway service")
    if http_client:
        await http_client.aclose()
    if redis_client:
        await redis_client.close()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Beckn Gateway",
    description="Jaringan Dagang — Beckn search multicast gateway",
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
# Helpers
# ---------------------------------------------------------------------------

def _get_http_client() -> httpx.AsyncClient:
    """Get or create the httpx client (handles serverless cold starts)."""
    global http_client
    if http_client is None:
        http_client = httpx.AsyncClient(timeout=settings.bpp_timeout)
    return http_client


async def _lookup_bpps(domain: str, city: str) -> list[RegistrySubscriber]:
    """Query the registry for BPPs matching domain + city.

    Results are cached in Redis to avoid hitting the registry on every request.
    """
    cache_key = f"gateway:bpps:{domain}:{city}"

    # Try cache
    if redis_client:
        try:
            cached = await redis_client.get(cache_key)
            if cached:
                data = json.loads(cached)
                logger.debug("Cache hit for BPPs domain=%s city=%s (%d)", domain, city, len(data))
                return [RegistrySubscriber(**s) for s in data]
        except Exception as exc:
            logger.warning("Cache read error: %s", exc)

    # Query registry
    lookup_body = {"type": "BPP", "domain": domain, "city": city}
    try:
        resp = await _get_http_client().post(
            f"{settings.registry_url}/lookup",
            json=lookup_body,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("Registry lookup failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Failed to query registry for BPPs",
        ) from exc

    payload = resp.json()
    subscribers = payload.get("subscribers", [])

    # Populate cache
    if redis_client and subscribers:
        try:
            await redis_client.set(
                cache_key,
                json.dumps(subscribers),
                ex=settings.cache_ttl,
            )
        except Exception as exc:
            logger.warning("Cache write error: %s", exc)

    return [RegistrySubscriber(**s) for s in subscribers]


async def _forward_search(bpp: RegistrySubscriber, payload: dict[str, Any]) -> dict[str, Any]:
    """Forward a search request to a single BPP.

    Returns a result dict with the BPP id and either the ACK or the error.
    """
    url = bpp.subscriber_url.rstrip("/") + "/search"

    # Inject bpp_id and bpp_uri into the context so the BPP and BAP know
    # which provider platform this search targets.
    enriched = json.loads(json.dumps(payload))  # deep copy
    enriched.setdefault("context", {})
    enriched["context"]["bpp_id"] = bpp.subscriber_id
    enriched["context"]["bpp_uri"] = bpp.subscriber_url

    try:
        resp = await _get_http_client().post(url, json=enriched)
        resp.raise_for_status()
        return {
            "bpp_id": bpp.subscriber_id,
            "status": "ACK",
        }
    except httpx.HTTPError as exc:
        logger.warning(
            "Search multicast to %s (%s) failed: %s",
            bpp.subscriber_id, url, exc,
        )
        return {
            "bpp_id": bpp.subscriber_id,
            "status": "NACK",
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/search")
async def search(request: Request):
    """Receive a Beckn search request and multicast to all matching BPPs."""
    try:
        return await _handle_search(request)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Search endpoint error")
        return {"error": str(exc), "type": type(exc).__name__}


async def _handle_search(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    context = payload.get("context")
    if not context:
        raise HTTPException(status_code=400, detail="Missing 'context' in request")

    domain = context.get("domain")
    action = context.get("action")

    if action and action != "search":
        raise HTTPException(
            status_code=400,
            detail=f"Gateway only handles 'search' action, got '{action}'",
        )

    # Extract city — support both flat string and nested dict formats
    city_raw = context.get("city")
    if isinstance(city_raw, dict):
        city = city_raw.get("code", "")
    elif isinstance(city_raw, str):
        city = city_raw
    else:
        # Try nested context.location.city.code
        location = context.get("location", {})
        city_obj = location.get("city", {})
        if isinstance(city_obj, dict):
            city = city_obj.get("code", "")
        elif isinstance(city_obj, str):
            city = city_obj
        else:
            city = ""

    if not domain:
        raise HTTPException(status_code=400, detail="Missing 'domain' in context")
    if not city:
        raise HTTPException(status_code=400, detail="Missing city in context (context.city or context.location.city.code)")

    logger.info(
        "Search request: domain=%s city=%s bap_id=%s message_id=%s",
        domain, city, context.get("bap_id"), context.get("message_id"),
    )

    # Look up BPPs
    bpps = await _lookup_bpps(domain, city)

    if not bpps:
        logger.info("No BPPs found for domain=%s city=%s", domain, city)
        return {
            "message": {"ack": {"status": "ACK"}},
            "context": context,
            "note": "No BPPs registered for the requested domain and city",
        }

    logger.info("Multicasting search to %d BPP(s)", len(bpps))

    # Multicast concurrently
    tasks = [_forward_search(bpp, payload) for bpp in bpps]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Log results
    for r in results:
        if isinstance(r, Exception):
            logger.error("Unexpected error during multicast: %s", r)
        elif isinstance(r, dict) and r.get("status") == "NACK":
            logger.warning("BPP %s returned NACK: %s", r.get("bpp_id"), r.get("error"))

    return {
        "message": {"ack": {"status": "ACK"}},
        "context": context,
    }


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
