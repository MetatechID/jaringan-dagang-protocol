"""Storefront analytics — receive events, return funnel metrics per version.

All endpoints are public (no auth). The client tracker is a thin
fire-and-forget POSTer; we keep this fast and never fail loudly.

Funnel definition (per version_sha within a time window):
  sessions          = COUNT DISTINCT session_id with any event
  product_viewers   = sessions that fired product_view
  carters           = sessions that fired add_to_cart
  checkouts         = sessions that fired checkout_start
  Conversion rates are computed pairwise: ATC% = carters / sessions,
  CO% = checkouts / sessions. Final-order completion lives in the
  orders router and joins by session_id once we wire it; for v1 we
  treat checkout_start as the bottom of the funnel.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.storefront_event import StorefrontEvent

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


# ---------- Schemas ----------


class EventIn(BaseModel):
    name: str
    tenant_slug: str
    version_sha: str
    session_id: str
    ts: int = Field(..., description="Client-side ms-since-epoch when the event happened")
    path: str
    props: dict[str, Any] | None = None


class TrackIn(BaseModel):
    events: list[EventIn]


# ---------- Ingest ----------


@router.post("/track", status_code=204)
async def track(body: TrackIn, db: AsyncSession = Depends(get_db)) -> None:
    """Persist a batch of client events. Returns 204 even on partial errors —
    the storefront must never block on analytics."""
    if not body.events:
        return None
    now = datetime.now(timezone.utc)
    rows: list[StorefrontEvent] = []
    for e in body.events:
        # Trim runaway strings before insert.
        rows.append(
            StorefrontEvent(
                tenant_slug=e.tenant_slug[:64],
                session_id=e.session_id[:64],
                version_sha=e.version_sha[:64],
                event_name=e.name[:64],
                path=e.path[:512],
                client_ts_ms=int(e.ts),
                occurred_at=datetime.fromtimestamp(e.ts / 1000.0, tz=timezone.utc),
                received_at=now,
                props=(e.props or None),
            )
        )
    db.add_all(rows)
    try:
        await db.flush()
    except Exception:
        # Never fail the client. Log shape is up to the caller.
        await db.rollback()
    return None


# ---------- Funnel query ----------


@router.get("/funnel")
async def funnel(
    tenant_slug: str = Query(...),
    days: int = Query(default=7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Per-version funnel for the last N days. Cheap enough to render on
    every admin /versions page-load."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    # One row per (version_sha, event_name) with distinct-session count.
    stmt = (
        select(
            StorefrontEvent.version_sha,
            StorefrontEvent.event_name,
            func.count(func.distinct(StorefrontEvent.session_id)).label("sessions"),
            func.count(StorefrontEvent.id).label("event_count"),
            func.min(StorefrontEvent.occurred_at).label("first_seen"),
            func.max(StorefrontEvent.occurred_at).label("last_seen"),
        )
        .where(StorefrontEvent.tenant_slug == tenant_slug)
        .where(StorefrontEvent.occurred_at >= since)
        .group_by(StorefrontEvent.version_sha, StorefrontEvent.event_name)
    )
    result = await db.execute(stmt)

    per_version: dict[str, dict[str, Any]] = {}
    for row in result:
        v = row.version_sha
        bucket = per_version.setdefault(v, {
            "version_sha": v,
            "first_seen": row.first_seen,
            "last_seen": row.last_seen,
            "counts": {},
            "event_counts": {},
        })
        bucket["counts"][row.event_name] = int(row.sessions)
        bucket["event_counts"][row.event_name] = int(row.event_count)
        if row.first_seen and (not bucket["first_seen"] or row.first_seen < bucket["first_seen"]):
            bucket["first_seen"] = row.first_seen
        if row.last_seen and (not bucket["last_seen"] or row.last_seen > bucket["last_seen"]):
            bucket["last_seen"] = row.last_seen

    versions: list[dict[str, Any]] = []
    for v, b in per_version.items():
        sessions = b["counts"].get("page_view", 0)
        atc = b["counts"].get("add_to_cart", 0)
        co = b["counts"].get("checkout_start", 0)
        pdv = b["counts"].get("product_view", 0)
        versions.append({
            "version_sha": v,
            "first_seen": b["first_seen"].isoformat() if b["first_seen"] else None,
            "last_seen": b["last_seen"].isoformat() if b["last_seen"] else None,
            "sessions": sessions,
            "product_viewers": pdv,
            "carters": atc,
            "checkouts": co,
            "atc_rate": (atc / sessions) if sessions else 0.0,
            "checkout_rate": (co / sessions) if sessions else 0.0,
            "atc_to_checkout_rate": (co / atc) if atc else 0.0,
            "event_counts": b["event_counts"],
        })
    versions.sort(key=lambda r: r["last_seen"] or "", reverse=True)

    return {
        "tenant_slug": tenant_slug,
        "since": since.isoformat(),
        "versions": versions,
    }
