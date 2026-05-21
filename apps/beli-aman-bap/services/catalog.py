"""Catalog reader — Beckn mirror tables (mirror_*) with JSON fallback.

Previously read JSON fixtures from apps/beli-aman-bap/catalog/*.json. After
Phase 2 of the Beckn migration, the seller's Postgres is the source of truth;
the buyer's local read-only mirror is populated by Beckn /on_search push +
/search pull and queried here.

Source selection (Task A4) — env ``CATALOG_SOURCE``:

  - ``json`` (default, safe): JSON fixtures only. Mirror is unused.
  - ``mirror``: mirror_* only. JSON fallback is disabled — empty mirror means
    empty result (the canonical post-cutover behaviour).
  - ``mirror-with-fallback`` (a.k.a. ``mirror-with-json-fallback``): try
    mirror first, fall through to JSON if the mirror has no products for the
    requested brand. Operational sweet-spot during rollout: if push/pull
    hasn't reached this brand yet, the storefront still renders.

The flag is read at every call (no module-level cache) so an env flip takes
effect on the next request without process restart, matching the existing
``BECKN_REQUIRE_SIGNATURE`` convention in ``routers/beckn.py``.

Response shape is preserved for backwards-compat with the storefront UI:
    {
        "products": [
            {
                "name": ..., "description": ..., "sku": ..., "image": ...,
                "gallery": [...], "tagline": ..., "badges": [...], "category": ...,
                "option_axes": [...],
                "variants": [
                    {"sku": ..., "label": ..., "price_idr": ..., "stock": ...,
                     "weight_grams": ..., "gallery": [...], "image": ...,
                     "compare_at_price_idr": ...},
                    ...
                ],
            },
            ...
        ]
    }
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database import async_session
from models.mirror import MirrorProduct, MirrorSKU, MirrorStore

logger = logging.getLogger(__name__)

# Legacy JSON catalog directory — kept for ``json`` and ``mirror-with-fallback``
# modes. Once Phase 2 cutover is complete + ``CATALOG_SOURCE=mirror``, this
# directory can be deleted from the deploy artifact.
_FALLBACK_CATALOG_DIR = Path(__file__).resolve().parent.parent / "catalog"


# Valid env values (lower-case canonical; ``mirror-with-json-fallback`` is
# accepted as a synonym to match the spec § 5.3 docs).
_VALID_SOURCES = {"json", "mirror", "mirror-with-fallback", "mirror-with-json-fallback"}


def _catalog_source() -> str:
    """Resolve the active ``CATALOG_SOURCE`` env value at call time.

    Defaults to ``json`` for safety: a fresh deploy doesn't suddenly start
    reading from an empty mirror just because the table exists.
    """
    raw = (os.environ.get("CATALOG_SOURCE") or "json").strip().lower()
    if raw not in _VALID_SOURCES:
        logger.warning(
            "CATALOG_SOURCE=%r is not one of %s — defaulting to 'json'",
            raw, sorted(_VALID_SOURCES),
        )
        return "json"
    if raw == "mirror-with-json-fallback":
        return "mirror-with-fallback"
    return raw


def _serialize_product(p: MirrorProduct) -> dict[str, Any]:
    """Map a MirrorProduct (+ skus + images) to the legacy JSON shape."""
    parent_imgs = sorted(p.images or [], key=lambda i: i.position)
    primary_img = next((i.url for i in parent_imgs if i.is_primary), None)
    if primary_img is None and parent_imgs:
        primary_img = parent_imgs[0].url

    variants = []
    for s in sorted(p.skus or [], key=lambda s: s.sku_code):
        sku_imgs = sorted(s.images or [], key=lambda i: i.position)
        sku_primary = next((i.url for i in sku_imgs if i.is_primary), None)
        if sku_primary is None and sku_imgs:
            sku_primary = sku_imgs[0].url
        variants.append({
            "sku": s.sku_code,
            "label": s.variant_value or s.variant_name or "Default",
            "price_idr": int(s.price) if s.price is not None else 0,
            "compare_at_price_idr": int(s.original_price) if s.original_price else None,
            "stock": s.stock,
            "weight_grams": s.weight_grams,
            "gallery": [i.url for i in sku_imgs],
            "image": sku_primary,
        })

    attrs = p.attributes if isinstance(p.attributes, dict) else {}
    return {
        "name": p.name,
        "description": p.description or "",
        "sku": p.sku,
        "slug": (p.sku or "").lower().replace("_", "-"),
        "image": primary_img,
        "gallery": [i.url for i in parent_imgs],
        "tagline": attrs.get("tagline"),
        "badges": attrs.get("badges", []),
        "category": attrs.get("category"),
        "option_axes": attrs.get("option_axes", []),
        "variants": variants,
    }


async def _list_products_from_mirror(brand_slug: str) -> list[dict[str, Any]]:
    async with async_session() as db:
        result = await db.execute(
            select(MirrorStore)
            .where(MirrorStore.slug == brand_slug)
            .options(
                selectinload(MirrorStore.products).selectinload(MirrorProduct.skus).selectinload(MirrorSKU.images),
                selectinload(MirrorStore.products).selectinload(MirrorProduct.images),
            )
        )
        store = result.scalar_one_or_none()
        if store is None or not store.products:
            return []
        return [_serialize_product(p) for p in store.products]


@lru_cache(maxsize=8)
def _load_fallback(brand_slug: str) -> dict[str, Any]:
    path = _FALLBACK_CATALOG_DIR / f"{brand_slug}.json"
    if not path.exists():
        return {"products": []}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


async def list_products(brand_slug: str) -> list[dict[str, Any]]:
    """Return the toko's products from whichever source CATALOG_SOURCE selects.

    See :func:`_catalog_source` for the resolution rules. In ``mirror`` mode
    the result can be empty if no push/pull has populated the mirror — that
    is the desired post-cutover behaviour (the storefront then renders an
    empty-catalog state, no stale JSON gets in the way).
    """
    src = _catalog_source()
    if src == "json":
        return _load_fallback(brand_slug).get("products", [])
    products = await _list_products_from_mirror(brand_slug)
    if products:
        return products
    if src == "mirror":
        # Strict mode: no JSON fallback. Empty mirror -> empty result.
        return []
    # mirror-with-fallback
    fb = _load_fallback(brand_slug).get("products", [])
    if fb:
        logger.warning(
            "catalog mirror empty for %s — falling back to JSON (%d products). "
            "Trigger a Beckn /search to populate the mirror.",
            brand_slug, len(fb),
        )
    return fb


async def get_product(brand_slug: str, product_slug: str) -> dict[str, Any] | None:
    for p in await list_products(brand_slug):
        if p.get("slug") == product_slug or p.get("sku") == product_slug:
            return p
    return None
