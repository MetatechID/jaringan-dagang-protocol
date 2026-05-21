"""Outbound Beckn message sender for the buyer (BAP).

Signs and POSTs Beckn requests to BPP URLs. Looks up BPP URLs via the
network registry (RegistryClient). Logs every attempt to BecknOutboundLog.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

# Make the beckn-protocol package importable
_proto_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "packages", "beckn-protocol")
)
if _proto_path not in sys.path:
    sys.path.insert(0, _proto_path)

from beckn_protocol import (  # noqa: E402
    BecknSigner,
    RegistryClient,
    SubscriberNotFound,
    resolve_ondc_domain,
)
from nacl.signing import SigningKey  # noqa: E402

from config import settings  # noqa: E402
from database import async_session  # noqa: E402
from models.beckn_logs import BecknOutboundLog  # noqa: E402

logger = logging.getLogger(__name__)

_RETRY_DELAYS = [0.0, 1.0, 4.0, 16.0]


def build_ondc_context(
    *,
    action: str,
    bpp_id: str,
    bpp_uri: str,
    transaction_id: str | None = None,
    message_id: str | None = None,
) -> dict[str, Any]:
    """Build a Beckn ``context`` dict with the per-BPP ONDC domain code.

    The Beckn ``domain`` is resolved per target BPP via
    :func:`beckn_protocol.resolve_ondc_domain`: Safiya
    (``safiyafood.jaringan-dagang.id``) -> ``"ONDC:RET11"``, anything
    unknown / missing falls back to the resolver's store-level retail
    default (``"ONDC:RET"``). Settings still provides ``core_version``,
    ``bap_id``, ``bap_uri``, ``city``, ``country`` -- and the legacy
    ``settings.domain`` (``"retail"``) is intentionally NOT used: the
    resolver's default is the new fallback.

    Used by every outbound BAP envelope site (``services/beckn_orders``,
    ``routers/disputes``, ``workers/catalog_puller``) so all three emit a
    consistent ONDC-localized context.
    """
    return {
        "domain": resolve_ondc_domain(bpp_id).domain_code,
        "country": settings.country_code,
        "city": settings.city_code,
        "action": action,
        "core_version": settings.core_version,
        "bap_id": settings.subscriber_id,
        "bap_uri": settings.subscriber_url,
        "bpp_id": bpp_id,
        "bpp_uri": bpp_uri,
        "transaction_id": transaction_id or str(uuid.uuid4()),
        "message_id": message_id or str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

_registry: RegistryClient | None = None


def _get_registry() -> RegistryClient:
    global _registry
    if _registry is None:
        _registry = RegistryClient(registry_url=settings.registry_url)
    return _registry


def _load_signer() -> BecknSigner | None:
    """Load the BAP's signing key from disk (dev/keys/) or env."""
    key_path = os.environ.get(
        "BECKN_BAP_PRIVATE_KEY_PATH",
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "dev", "keys", "beli-aman.private.pem"),
    )
    key_b64_env = os.environ.get("BECKN_BAP_PRIVATE_KEY")
    try:
        if key_b64_env:
            raw = base64.b64decode(key_b64_env)
            signing_key = SigningKey(raw)
        elif os.path.exists(key_path):
            # PEM file — first try as raw base64 ed25519 seed, else parse PEM
            data = open(key_path, "rb").read()
            try:
                from cryptography.hazmat.primitives.serialization import load_pem_private_key
                from cryptography.hazmat.primitives.asymmetric import ed25519
                priv = load_pem_private_key(data, password=None)
                if isinstance(priv, ed25519.Ed25519PrivateKey):
                    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
                    raw = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
                    signing_key = SigningKey(raw)
                else:
                    return None
            except Exception:
                # try as raw bytes
                signing_key = SigningKey(data[:32])
        else:
            return None
        return BecknSigner(
            signing_key=signing_key,
            subscriber_id=settings.subscriber_id,
            unique_key_id=settings.unique_key_id,
        )
    except Exception:
        logger.exception("failed to load BAP signing key")
        return None


_signer_singleton: BecknSigner | None = None
_signer_loaded = False


def get_signer() -> BecknSigner | None:
    global _signer_singleton, _signer_loaded
    if not _signer_loaded:
        _signer_singleton = _load_signer()
        _signer_loaded = True
    return _signer_singleton


async def resolve_bpp_url(bpp_id: str) -> str | None:
    """Look up a BPP's beckn URL via the registry."""
    try:
        sub = await _get_registry().lookup(bpp_id)
        return sub.subscriber_url
    except SubscriberNotFound:
        logger.warning("BPP %s not in registry", bpp_id)
        return None


async def send_beckn_request(
    *,
    bpp_id: str,
    action: str,
    body: dict[str, Any],
    target_url: str | None = None,
) -> bool:
    """Send a signed Beckn request to a BPP.

    Args:
        bpp_id: BPP subscriber_id. Looked up via registry if target_url not given.
        action: Beckn action (e.g. "search", "select", "init", "confirm", "update").
        body: Full Beckn request body. context.action/bap_id/bap_uri should be set.
        target_url: If given, used directly; otherwise looked up via registry.
    """
    if target_url is None:
        url = await resolve_bpp_url(bpp_id)
        if url is None:
            logger.warning("cannot send %s — no URL for %s", action, bpp_id)
            return False
        target_url = f"{url.rstrip('/')}/{action}"

    signer = get_signer()
    body_bytes = json.dumps(body, separators=(",", ":")).encode()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if signer is not None:
        headers["Authorization"] = signer.sign(body_bytes)
    else:
        logger.warning("BAP has no signer configured; sending %s unsigned", action)

    ctx = body.get("context") or {}
    msg_id = ctx.get("message_id") or str(uuid.uuid4())
    txn_id = ctx.get("transaction_id")

    last_status: int | None = None
    for attempt, delay in enumerate(_RETRY_DELAYS, start=1):
        if delay:
            await asyncio.sleep(delay)
        log = BecknOutboundLog(
            message_id=msg_id,
            transaction_id=txn_id,
            action=action,
            target_url=target_url,
            attempt=attempt,
            request_body=body,
        )
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(target_url, content=body_bytes, headers=headers)
                log.response_status = resp.status_code
                try:
                    log.response_body = resp.json()
                except Exception:
                    log.response_body = {"text": resp.text[:500]}
        except Exception as e:
            log.error = repr(e)[:1000]
            async with async_session() as db:
                db.add(log)
                await db.commit()
            continue

        async with async_session() as db:
            db.add(log)
            await db.commit()

        last_status = resp.status_code
        if 200 <= resp.status_code < 300:
            return True
        if resp.status_code < 500:
            return False
    logger.warning("Beckn %s -> %s failed after retries (last=%s)", action, target_url, last_status)
    return False
