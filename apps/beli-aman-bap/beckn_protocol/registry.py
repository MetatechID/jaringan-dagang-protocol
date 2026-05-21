"""Registry client — looks up subscriber pubkeys + URLs from network registry.

Caches results in Redis (TTL 1h). Falls back to in-process LRU cache if Redis
unavailable.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class SubscriberNotFound(Exception):
    pass


@dataclass
class Subscriber:
    subscriber_id: str
    subscriber_url: str
    signing_public_key_b64: str
    encryption_public_key_b64: str | None
    type: str
    status: str

    @property
    def public_key_bytes(self) -> bytes:
        return base64.b64decode(self.signing_public_key_b64)


class RegistryClient:
    """Lookup subscriber metadata + pubkeys from the network registry.

    Args:
        registry_url: Base URL of network registry (e.g. http://localhost:3030).
        redis: Optional redis.asyncio.Redis instance for distributed cache.
        ttl: Cache TTL in seconds (default 1h).
    """

    def __init__(
        self,
        registry_url: str,
        redis: Any | None = None,
        ttl: int = 3600,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        # strip whitespace + trailing slashes (env vars sometimes have \n)
        self.registry_url = (registry_url or "").strip().rstrip("/")
        self.redis = redis
        self.ttl = ttl
        self._http = http_client
        # in-process fallback when redis unavailable
        self._mem_cache: dict[str, tuple[float, Subscriber]] = {}
        # Static fallback: subscribers loadable from env BECKN_STATIC_SUBSCRIBERS
        # (JSON: {"subscriber_id": {"url": "...", "pubkey_b64": "...", "type": "BPP"}})
        # Used when the registry is unreachable.
        self._static = self._load_static()

    @staticmethod
    def _load_static() -> dict[str, Subscriber]:
        """Load static subscriber fallback from env or a bundled JSON file.

        Tried in order:
          1. BECKN_STATIC_SUBSCRIBERS env (JSON string)
          2. BECKN_STATIC_SUBSCRIBERS_PATH env (path to JSON file)
          3. Discover dev/static-subscribers.json or dev-static-subscribers.json
             next to common app roots.
        """
        import json as _json
        import os
        from pathlib import Path

        raw = os.environ.get("BECKN_STATIC_SUBSCRIBERS", "")
        data: dict | None = None
        if raw:
            try:
                data = _json.loads(raw)
            except Exception:
                logger.exception("BECKN_STATIC_SUBSCRIBERS not valid JSON")

        if data is None:
            path_env = os.environ.get("BECKN_STATIC_SUBSCRIBERS_PATH", "")
            candidates = []
            if path_env:
                candidates.append(Path(path_env))
            # Walk up from this file to look for dev/static-subscribers.json
            here = Path(__file__).resolve()
            for parent in [here.parent, *here.parents][:6]:
                candidates.append(parent / "dev" / "static-subscribers.json")
                candidates.append(parent / "dev-static-subscribers.json")
                candidates.append(parent / "static-subscribers.json")
            for p in candidates:
                try:
                    if p.is_file():
                        data = _json.loads(p.read_text())
                        logger.info("loaded static subscribers from %s (%d entries)", p, len(data))
                        break
                except Exception:
                    continue

        if not data:
            return {}

        out: dict[str, Subscriber] = {}
        for sid, meta in data.items():
            out[sid] = Subscriber(
                subscriber_id=sid,
                subscriber_url=meta.get("url", ""),
                signing_public_key_b64=meta.get("pubkey_b64", ""),
                encryption_public_key_b64=meta.get("pubkey_b64", ""),
                type=meta.get("type", "BPP"),
                status=meta.get("status", "SUBSCRIBED"),
            )
        return out

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=5.0)
        return self._http

    def _key(self, subscriber_id: str) -> str:
        return f"beckn:registry:{subscriber_id}"

    async def lookup(self, subscriber_id: str) -> Subscriber:
        """Look up a subscriber by ID. Raises SubscriberNotFound if missing.

        Tries (in order): in-process cache → Redis → network registry → static fallback.
        """
        sid = (subscriber_id or "").strip()
        # try cache
        cached = await self._cache_get(sid)
        if cached is not None:
            return cached

        # Try network registry first (if URL configured), then fall back to static.
        if self.registry_url:
            http = await self._get_http()
            try:
                resp = await http.post(
                    f"{self.registry_url}/lookup",
                    json={"subscriber_id": sid},
                )
                resp.raise_for_status()
                body = resp.json()
                subs = body.get("subscribers") or []
                if subs:
                    raw = subs[0]
                    sub = Subscriber(
                        subscriber_id=raw["subscriber_id"],
                        subscriber_url=raw["subscriber_url"],
                        signing_public_key_b64=raw["signing_public_key"],
                        encryption_public_key_b64=raw.get("encryption_public_key"),
                        type=raw.get("type", "BPP"),
                        status=raw.get("status", "SUBSCRIBED"),
                    )
                    await self._cache_set(sid, sub)
                    return sub
            except httpx.HTTPError as e:
                logger.warning("registry lookup failed for %s: %s — trying static fallback", sid, e)

        # Static fallback
        static = self._static.get(sid)
        if static is not None:
            await self._cache_set(sid, static)
            return static

        raise SubscriberNotFound(sid)


    async def invalidate(self, subscriber_id: str) -> None:
        """Drop a cached subscriber entry (e.g. after registry change)."""
        self._mem_cache.pop(subscriber_id, None)
        if self.redis is not None:
            try:
                await self.redis.delete(self._key(subscriber_id))
            except Exception:
                pass

    async def _cache_get(self, subscriber_id: str) -> Subscriber | None:
        # in-memory first (cheapest)
        ent = self._mem_cache.get(subscriber_id)
        if ent is not None and ent[0] > time.time():
            return ent[1]
        # redis
        if self.redis is not None:
            try:
                raw = await self.redis.get(self._key(subscriber_id))
                if raw is not None:
                    data = json.loads(raw if isinstance(raw, str) else raw.decode())
                    sub = Subscriber(**data)
                    # warm in-memory
                    self._mem_cache[subscriber_id] = (time.time() + 60, sub)
                    return sub
            except Exception:
                pass
        return None

    async def _cache_set(self, subscriber_id: str, sub: Subscriber) -> None:
        self._mem_cache[subscriber_id] = (time.time() + 60, sub)
        if self.redis is not None:
            try:
                await self.redis.setex(
                    self._key(subscriber_id),
                    self.ttl,
                    json.dumps(sub.__dict__),
                )
            except Exception:
                pass
