"""Beckn BAP endpoints — receives on_* callbacks from BPPs.

All routes here are signature-verified + idempotent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jaringan_dagang_beckn import RegistryClient, SubscriberNotFound, verify_request  # noqa: E402
from routers.beckn_ownership import VerifiedContext, authorize_resources

from config import settings  # noqa: E402
from database import async_session  # noqa: E402
from models.beckn_logs import BecknInboundLog  # noqa: E402

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/beckn", tags=["beckn"])


@router.get("/_inbound")
async def _inbound(limit: int = 20):
    """Recent inbound Beckn messages received by the BAP (for diagnostics)."""
    from sqlalchemy import select, desc
    async with async_session() as db:
        rows = (await db.execute(
            select(BecknInboundLog).order_by(desc(BecknInboundLog.received_at)).limit(limit)
        )).scalars().all()
    return {"rows": [{
        "action": r.action,
        "bpp_id": r.bpp_id, "bap_id": r.bap_id,
        "received_at": r.received_at.isoformat() if r.received_at else None,
        "message_id": r.message_id,
        "response_status": r.response_status,
    } for r in rows]}


@router.get("/_mirror")
async def _mirror():
    """Mirror state per store (for diagnostics)."""
    from models.mirror import MirrorStore, MirrorProduct
    from sqlalchemy import select, func
    async with async_session() as db:
        rows = (await db.execute(select(MirrorStore))).scalars().all()
        out = []
        for s in rows:
            cnt = (await db.execute(
                select(func.count(MirrorProduct.id)).where(MirrorProduct.store_id == s.id)
            )).scalar()
            out.append({
                "slug": s.slug, "bpp_id": s.bpp_id, "name": s.name,
                "product_count": cnt,
                "last_pushed_at": s.last_pushed_at.isoformat() if s.last_pushed_at else None,
            })
    return {"stores": out}


@router.get("/_debug")
async def _debug_registry():
    """Diagnostic: report what the buyer thinks the registry/static state is."""
    import os
    from pathlib import Path
    r = _get_registry()
    here = Path(__file__).resolve()
    discovered = []
    for parent in [here.parent, *here.parents][:6]:
        for cand in (parent / "dev" / "static-subscribers.json",
                     parent / "dev-static-subscribers.json",
                     parent / "static-subscribers.json"):
            discovered.append({"path": str(cand), "exists": cand.is_file()})
    return {
        "registry_url": r.registry_url,
        "static_subscriber_count": len(r._static),
        "static_subscriber_ids": list(r._static.keys()),
        "env_BECKN_STATIC_SUBSCRIBERS_set": bool(os.environ.get("BECKN_STATIC_SUBSCRIBERS")),
        "env_BECKN_STATIC_SUBSCRIBERS_PATH": os.environ.get("BECKN_STATIC_SUBSCRIBERS_PATH", ""),
        "this_file": str(here),
        "discovery_candidates": discovered[:12],
    }

REQUIRE_SIGNATURE = os.environ.get("BECKN_REQUIRE_SIGNATURE", "true").lower() != "false"

_registry: RegistryClient | None = None


def _get_registry() -> RegistryClient:
    global _registry
    if _registry is None:
        _registry = RegistryClient(registry_url=settings.registry_url)
    return _registry


_KEY_ID_RE = re.compile(r'keyId="([^"]+)"')


def _extract_subscriber_id(auth: str) -> str | None:
    m = _KEY_ID_RE.search(auth)
    if not m:
        return None
    return m.group(1).split("|")[0]


async def _verify(request: Request, body: bytes) -> str:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        # Unsigned callbacks have no principal and cannot authorize mutations.
        raise HTTPException(401, "Missing Authorization header")
    sid = _extract_subscriber_id(auth)
    if not sid:
        raise HTTPException(401, "Malformed Authorization header")
    try:
        sub = await _get_registry().lookup(sid)
    except SubscriberNotFound:
        raise HTTPException(401, f"Unknown subscriber: {sid}")
    except Exception:
        logger.exception("Registry lookup failed for %s", sid)
        raise HTTPException(503, "Registry lookup failed")
    if not verify_request(body, auth, sub.signing_public_key_b64):
        raise HTTPException(401, "Invalid Beckn signature")
    return sid


async def _idempotent(db: AsyncSession, message_id: str) -> dict | None:
    if not message_id:
        return None
    row = (
        await db.execute(
            select(BecknInboundLog).where(BecknInboundLog.message_id == message_id)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return row.response_body or {"message": {"ack": {"status": "ACK"}}}


def _ack() -> dict:
    return {"message": {"ack": {"status": "ACK"}}}


# ------------------------------------------------------------------
# Endpoints (handlers wired in later phases — for now they ACK + log)
# ------------------------------------------------------------------


_BECKN_ACTIONS = (
    "on_search",
    "on_select",
    "on_init",
    "on_confirm",
    "on_status",
    "on_track",
    "on_cancel",
    "on_update",
    "on_rating",
    "on_support",
    # ONDC IGM (Task A5) — refund-request scope.
    "on_issue",
    "on_issue_status",
    # ONDC RSP (Task A6) — settlement-record scope.
    "on_settle",
)


def _make_endpoint(action: str):
    async def handler(request: Request):
        raw = await request.body()
        signer = await _verify(request, raw)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError()
            context = body["context"]
            message = body.get("message", {})
            if not isinstance(context, dict) or not isinstance(message, dict):
                raise ValueError()
            for field in ("message_id", "transaction_id"):
                if not isinstance(context.get(field), str) or not context[field].strip():
                    raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise HTTPException(400, "Invalid Beckn envelope")
        if context.get("bpp_id") != signer or context.get("bap_id") != settings.subscriber_id:
            raise HTTPException(403, "Beckn participant identity mismatch")
        if context.get("action") != action:
            raise HTTPException(400, "Beckn action mismatch")
        ctx = VerifiedContext(context, signer)
        # Keep the existing unique message_id column schema while namespacing
        # retries by their verified identity and protocol correlation fields.
        message_id = hashlib.sha256(json.dumps([
            signer, action, ctx["transaction_id"], ctx["message_id"],
        ], separators=(",", ":")).encode()).hexdigest()

        async with async_session() as db:
            try:
                # Authorization precedes dedupe so an old ACK cannot bypass a
                # changed owner/resource boundary.
                await authorize_resources(action, ctx, message, db)
                cached = await _idempotent(db, message_id)
                if cached is not None:
                    return cached
                response = _ack()
                from routers import beckn_handlers
                real = getattr(beckn_handlers, f"handle_{action}", None)
                if real is not None:
                    handler_response = await real(ctx, message, db)
                    if handler_response is not None:
                        response = handler_response
                db.add(BecknInboundLog(
                    message_id=message_id,
                    transaction_id=ctx["transaction_id"], action=action,
                    bpp_id=signer, bap_id=ctx["bap_id"], response_status=200,
                    response_body=response, received_at=datetime.now(timezone.utc),
                ))
                # Effects and success record commit together. Failed work is
                # never acknowledged or recorded as a completed retry.
                await db.commit()
            except HTTPException:
                await db.rollback()
                raise
            except Exception:
                await db.rollback()
                logger.exception("buyer %s callback failed", action)
                raise HTTPException(500, "Beckn callback processing failed")
        return response

    handler.__name__ = f"beckn_{action}"
    return handler


for _action in _BECKN_ACTIONS:
    router.add_api_route(
        f"/{_action}",
        _make_endpoint(_action),
        methods=["POST"],
        summary=f"Beckn {_action} callback",
    )
