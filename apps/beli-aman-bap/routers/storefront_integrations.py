"""Per-tenant marketing/analytics integrations (GA, Facebook Pixel, CAPI).

- ``GET /api/v1/storefronts/{slug}/integrations`` — public; consumed by the
  storefront layout to inject GA / Pixel `<script>` tags. Returns only the
  public IDs (ga_measurement_id, fb_pixel_id). NEVER returns the CAPI
  access token.
- ``GET /api/v1/storefronts/{slug}/integrations/admin`` — admin-authed.
  Returns the public IDs PLUS whether the CAPI token is configured (boolean
  only, never the value itself).
- ``PUT /api/v1/storefronts/{slug}/integrations`` — admin-authed. Accepts
  public IDs and (optionally) the CAPI token. Token field is write-only:
  passing ``null`` leaves it unchanged, passing ``""`` clears it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_profile
from models.profile import BeliAmanProfile
from models.store_membership import StoreMembership
from models.storefront_integration import StorefrontIntegration

router = APIRouter(prefix="/api/v1/storefronts", tags=["storefront-integrations"])


def _serialize_public(row: StorefrontIntegration | None, slug: str) -> dict:
    """Public view — safe to expose to anonymous storefront layout.

    Returns ONLY values the browser legitimately needs to render the Pixel
    snippet. Server-side secrets (CAPI token) are never included.
    """
    if row is None:
        return {
            "tenant_slug": slug,
            "ga_measurement_id": None,
            "fb_pixel_id": None,
        }
    return {
        "tenant_slug": row.tenant_slug,
        "ga_measurement_id": row.ga_measurement_id,
        "fb_pixel_id": row.fb_pixel_id,
    }


def _serialize_admin(row: StorefrontIntegration | None, slug: str) -> dict:
    """Admin view — adds CAPI status flags without revealing secret values."""
    base = _serialize_public(row, slug)
    base["fb_capi_access_token_set"] = bool(row and row.fb_capi_access_token)
    base["fb_capi_test_event_code"] = row.fb_capi_test_event_code if row else None
    return base


async def _require_admin_for_slug(
    slug: str, profile: BeliAmanProfile, db: AsyncSession
) -> None:
    if profile.is_super_admin:
        return
    membership = (
        await db.execute(
            select(StoreMembership)
            .where(StoreMembership.profile_id == profile.id)
            .where(StoreMembership.store_slug == slug)
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(403, f"Not a member of store '{slug}'")


@router.get("/{slug}/integrations")
async def get_integrations(slug: str, db: AsyncSession = Depends(get_db)) -> dict:
    row = (
        await db.execute(
            select(StorefrontIntegration).where(StorefrontIntegration.tenant_slug == slug)
        )
    ).scalar_one_or_none()
    return _serialize_public(row, slug)


@router.get("/{slug}/integrations/admin")
async def get_integrations_admin(
    slug: str,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require_admin_for_slug(slug, profile, db)
    row = (
        await db.execute(
            select(StorefrontIntegration).where(StorefrontIntegration.tenant_slug == slug)
        )
    ).scalar_one_or_none()
    return _serialize_admin(row, slug)


class IntegrationsIn(BaseModel):
    ga_measurement_id: str | None = None
    fb_pixel_id: str | None = None
    # Write-only. Semantics:
    #   - field omitted entirely → no change to stored token
    #   - "" (empty string) → clear the stored token
    #   - "<value>" → set new token
    # We can't distinguish "missing" from "null" in pydantic v1-style BaseModel,
    # so the router uses model_fields_set to tell them apart.
    fb_capi_access_token: str | None = None
    fb_capi_test_event_code: str | None = None


def _normalize(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    return v or None


@router.put("/{slug}/integrations")
async def put_integrations(
    slug: str,
    body: IntegrationsIn,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require_admin_for_slug(slug, profile, db)

    ga = _normalize(body.ga_measurement_id)
    pixel = _normalize(body.fb_pixel_id)
    fields_set = body.model_fields_set

    row = (
        await db.execute(
            select(StorefrontIntegration).where(StorefrontIntegration.tenant_slug == slug)
        )
    ).scalar_one_or_none()

    if row is None:
        row = StorefrontIntegration(
            tenant_slug=slug,
            ga_measurement_id=ga,
            fb_pixel_id=pixel,
        )
        db.add(row)
    else:
        row.ga_measurement_id = ga
        row.fb_pixel_id = pixel

    # Token update — only touch if the field was explicitly sent. This
    # preserves the existing token when the admin UI does a partial save
    # of just the public IDs.
    if "fb_capi_access_token" in fields_set:
        token_raw = body.fb_capi_access_token
        if token_raw is None or token_raw.strip() == "":
            row.fb_capi_access_token = None
        else:
            row.fb_capi_access_token = token_raw.strip()
    if "fb_capi_test_event_code" in fields_set:
        row.fb_capi_test_event_code = _normalize(body.fb_capi_test_event_code)

    await db.flush()
    return _serialize_admin(row, slug)
