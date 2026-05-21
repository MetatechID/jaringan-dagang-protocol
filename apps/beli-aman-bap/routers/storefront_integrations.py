"""Per-tenant marketing/analytics integrations (GA, Facebook Pixel).

- ``GET /api/v1/storefronts/{slug}/integrations`` — public; consumed by the
  storefront layout to inject GA / Pixel `<script>` tags.
- ``PUT /api/v1/storefronts/{slug}/integrations`` — requires the caller to
  be a super-admin or a StoreMembership member for `slug`. Edited from the
  buyer-side Vibe admin modal.
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


def _serialize(row: StorefrontIntegration | None, slug: str) -> dict:
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


@router.get("/{slug}/integrations")
async def get_integrations(slug: str, db: AsyncSession = Depends(get_db)) -> dict:
    row = (
        await db.execute(
            select(StorefrontIntegration).where(StorefrontIntegration.tenant_slug == slug)
        )
    ).scalar_one_or_none()
    return _serialize(row, slug)


class IntegrationsIn(BaseModel):
    ga_measurement_id: str | None = None
    fb_pixel_id: str | None = None


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
    if not profile.is_super_admin:
        membership = (
            await db.execute(
                select(StoreMembership)
                .where(StoreMembership.profile_id == profile.id)
                .where(StoreMembership.store_slug == slug)
            )
        ).scalar_one_or_none()
        if membership is None:
            raise HTTPException(403, f"Not a member of store '{slug}'")

    ga = _normalize(body.ga_measurement_id)
    pixel = _normalize(body.fb_pixel_id)

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

    await db.flush()
    return _serialize(row, slug)
