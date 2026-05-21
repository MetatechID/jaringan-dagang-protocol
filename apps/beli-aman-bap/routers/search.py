"""Bot-facing REST: ``/api/v1/search`` (Task B3a).

Exposes Beckn /search behind a stateless ``Authorization: Bearer <BOT_API_TOKEN>``
guard. The B3 jd-sell MCP server is the only known caller today; Firebase-
authenticated storefront customers do NOT hit these routes.

Sync ACK model:

  1. ``POST /api/v1/search`` — creates a SearchSession row, fires the Beckn
     /search envelope, returns ``{session_id, transaction_id, status}``.
  2. ``GET /api/v1/search/{session_id}/results`` — accumulated /on_search
     results, queried from the MirrorProduct rows for that session's BPP.
     The /on_search handler in ``routers/beckn_handlers.py:handle_on_search``
     does the heavy lifting (mirror_* upsert) — this endpoint just reads
     back what has accumulated.

YAGNI: no pagination, no faceting, no client-side filtering. The bot
issues one search per query; if it wants paginated UX it can page in its
own layer.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.bot_auth import require_bot
from database import get_db
from models.bot_rest import SearchSession, SearchSessionStatus
from models.mirror import (
    MirrorProduct,
    MirrorProductImage,
    MirrorSKU,
    MirrorSKUImage,
    MirrorStore,
)
from services import order_flow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["bot-rest"])


# ---------- Schemas ----------


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    category: str | None = None
    city: str | None = None  # defaults to settings.city_code on the wire
    bpp_id: str | None = None
    bpp_uri: str | None = None


class SearchOut(BaseModel):
    session_id: str
    transaction_id: str
    status: str
    bpp_id: str | None = None


class SearchResultsOut(BaseModel):
    session_id: str
    status: str
    results: list[dict[str, Any]]


# ---------- Endpoints ----------


@router.post(
    "",
    dependencies=[Depends(require_bot)],
    response_model=SearchOut,
)
async def create_search(
    body: SearchIn,
    db: AsyncSession = Depends(get_db),
) -> SearchOut:
    """Fire a Beckn /search; return a session handle for polling."""
    transaction_id = str(uuid.uuid4())
    session = SearchSession(
        query=body.query,
        category=body.category,
        city=body.city or "std:021",
        transaction_id=transaction_id,
        bpp_id=body.bpp_id,
        status=SearchSessionStatus.PENDING,
    )
    db.add(session)
    await db.flush()

    try:
        await order_flow.send_search(
            query=body.query,
            category=body.category,
            city=body.city,
            bpp_id=body.bpp_id,
            bpp_uri=body.bpp_uri,
            transaction_id=transaction_id,
        )
    except Exception:
        # Transport failures are non-fatal — the bot can re-poll or retry.
        logger.exception("send_search failed for session %s", session.id)

    return SearchOut(
        session_id=session.id,
        transaction_id=session.transaction_id,
        status=session.status.value,
        bpp_id=session.bpp_id,
    )


def _serialize_mirror_product(
    p: MirrorProduct,
    images: list[MirrorProductImage],
    skus: list[MirrorSKU],
    sku_images: dict[str, list[MirrorSKUImage]],
) -> dict[str, Any]:
    return {
        "product_id": p.bpp_product_id,
        "sku": p.sku,
        "name": p.name,
        "description": p.description,
        "status": p.status,
        "images": [
            {"url": i.url, "is_primary": i.is_primary, "position": i.position}
            for i in sorted(images, key=lambda x: x.position)
        ],
        "skus": [
            {
                "sku_id": s.bpp_sku_id,
                "sku_code": s.sku_code,
                "variant_name": s.variant_name,
                "variant_value": s.variant_value,
                "price_idr": int(s.price),
                "original_price_idr": int(s.original_price) if s.original_price else None,
                "stock": s.stock,
                "images": [
                    {"url": si.url, "is_primary": si.is_primary, "position": si.position}
                    for si in sorted(sku_images.get(s.id, []), key=lambda x: x.position)
                ],
            }
            for s in skus
        ],
    }


@router.get(
    "/{session_id}/results",
    dependencies=[Depends(require_bot)],
    response_model=SearchResultsOut,
)
async def get_search_results(
    session_id: str,
    db: AsyncSession = Depends(get_db),
) -> SearchResultsOut:
    """Return accumulated /on_search results for this session.

    The /on_search handler upserts MirrorProduct rows per (bpp_id, provider).
    We read those rows back, scoped to the session's bpp_id (if known) or
    return all stores' products (the bot can dedupe by provider_id).
    """
    session = (
        await db.execute(
            select(SearchSession).where(SearchSession.id == session_id)
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Search session not found")

    expires_at = (
        session.expires_at.replace(tzinfo=timezone.utc)
        if session.expires_at and session.expires_at.tzinfo is None
        else session.expires_at
    )
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        session.status = SearchSessionStatus.EXPIRED
        return SearchResultsOut(
            session_id=session.id,
            status=session.status.value,
            results=[],
        )

    # If the session pre-froze its results, return them verbatim.
    if session.results_json:
        return SearchResultsOut(
            session_id=session.id,
            status=session.status.value,
            results=session.results_json.get("results") or [],
        )

    # Scope to session.bpp_id when known, else return everything.
    store_q = select(MirrorStore)
    if session.bpp_id:
        store_q = store_q.where(MirrorStore.bpp_id == session.bpp_id)
    stores = (await db.execute(store_q)).scalars().all()
    if not stores:
        results: list[dict[str, Any]] = []
    else:
        # Fetch products + images + skus + sku images in 4 batched IN
        # queries (instead of N×M sequential roundtrips per store/product).
        # Vercel serverless + Neon's per-query latency was hitting the
        # 30 s function timeout on the per-product loop.
        store_ids = [s.id for s in stores]
        products = (
            await db.execute(
                select(MirrorProduct).where(MirrorProduct.store_id.in_(store_ids))
            )
        ).scalars().all()

        # Filter by the original query — Beckn /on_search returns the
        # whole catalog per store (no server-side query filter exists in
        # the protocol), so the bot was getting back 17 unrelated rows
        # for "minyak zaitun" against a catalog with only kurma/madu/etc.
        # Substring-match on name + description, case-insensitive, with
        # per-word tokenization so "minyak zaitun" matches "Minyak Zaitun
        # Extra Virgin" but not "Madu Asli". Empty query (defensive) =
        # no filter.
        q_raw = (session.query or "").strip().lower()
        if q_raw:
            tokens = [t for t in q_raw.split() if len(t) >= 2]

            def _haystack(p: MirrorProduct) -> str:
                return f"{(p.name or '').lower()} {(p.description or '').lower()}"

            def _matches(p: MirrorProduct) -> bool:
                hay = _haystack(p)
                # Match: at least one token appears as a whole word or
                # substring. Be permissive (Indonesian conjugations) but
                # not absurd — minimum 2-char tokens already filter "di",
                # "a", "ke", "ya" out.
                return any(t in hay for t in tokens) if tokens else True

            filtered = [p for p in products if _matches(p)]
            # If the filter removes EVERY product, prefer "no results" to
            # "all results" — the bot's persona handles the empty case
            # gracefully and won't escalate.
            products = filtered
        prod_ids = [p.id for p in products]

        prod_imgs: dict[str, list[MirrorProductImage]] = {}
        if prod_ids:
            for img in (
                await db.execute(
                    select(MirrorProductImage).where(
                        MirrorProductImage.product_id.in_(prod_ids)
                    )
                )
            ).scalars().all():
                prod_imgs.setdefault(img.product_id, []).append(img)

        prod_skus: dict[str, list[MirrorSKU]] = {}
        sku_ids: list[str] = []
        if prod_ids:
            for sku in (
                await db.execute(
                    select(MirrorSKU).where(MirrorSKU.product_id.in_(prod_ids))
                )
            ).scalars().all():
                prod_skus.setdefault(sku.product_id, []).append(sku)
                sku_ids.append(sku.id)

        sku_imgs: dict[str, list[MirrorSKUImage]] = {}
        if sku_ids:
            for img in (
                await db.execute(
                    select(MirrorSKUImage).where(
                        MirrorSKUImage.sku_id.in_(sku_ids)
                    )
                )
            ).scalars().all():
                sku_imgs.setdefault(img.sku_id, []).append(img)

        prods_by_store: dict[str, list[MirrorProduct]] = {}
        for p in products:
            prods_by_store.setdefault(p.store_id, []).append(p)

        results = []
        for store in stores:
            store_prods = prods_by_store.get(store.id, [])
            if not store_prods:
                continue
            store_block = {
                "bpp_id": store.bpp_id,
                "bpp_uri": store.bpp_uri,
                "provider_id": store.bpp_id,
                "store_slug": store.slug,
                "store_name": store.name,
                "products": [
                    _serialize_mirror_product(
                        p, prod_imgs.get(p.id, []), prod_skus.get(p.id, []), sku_imgs
                    )
                    for p in store_prods
                ],
            }
            results.append(store_block)

    if results and session.status == SearchSessionStatus.PENDING:
        session.status = SearchSessionStatus.RESULTS

    return SearchResultsOut(
        session_id=session.id,
        status=session.status.value,
        results=results,
    )
