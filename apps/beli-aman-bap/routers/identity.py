"""Beli Aman = the network Identity Provider.

Endpoints other apps (seller dashboard, buyer Vibe admin) call to resolve
"who is this person + what stores can they manage":

  GET    /api/v1/me/stores                       my store memberships
  GET    /api/v1/stores/{store_id}/members        list members of a store
  POST   /api/v1/stores/{store_id}/members        invite by email (owner only)
  DELETE /api/v1/stores/{store_id}/members/{id}   revoke (owner only)
  POST   /api/v1/identity/seed-membership         admin backfill (X-Admin-Token)

`store_id` is the seller catalog's Store.id (a loose reference; Beli Aman
owns the permission mapping, not the catalog).
"""

from __future__ import annotations

import logging
import traceback
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import get_db
from deps import SUPER_ADMIN_EMAILS, get_current_profile, require_admin_token
from models.profile import BeliAmanProfile
from models.store_membership import StoreMembership, StoreRole

logger = logging.getLogger(__name__)

router = APIRouter(tags=["identity"])


def _serialize_member(m: StoreMembership, p: BeliAmanProfile | None) -> dict[str, Any]:
    return {
        "membership_id": m.id,
        "store_id": m.store_id,
        "email": m.invited_email,
        "role": m.role.value if isinstance(m.role, StoreRole) else m.role,
        "pending": p is None,
        "accepted_at": m.accepted_at.isoformat() if m.accepted_at else None,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "profile": None
        if p is None
        else {
            "id": p.id,
            "email": p.email,
            "display_name": p.display_name,
            "photo_url": p.photo_url,
        },
    }


async def _can_manage(
    db: AsyncSession, profile: BeliAmanProfile, store_id: str, owner_only: bool = False
) -> bool:
    if profile.is_super_admin:
        return True
    row = (
        await db.execute(
            select(StoreMembership)
            .where(StoreMembership.profile_id == profile.id)
            .where(StoreMembership.store_id == store_id)
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    if owner_only:
        return row.role == StoreRole.OWNER
    return True


@router.get("/api/v1/me/stores")
async def my_stores(
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Store ids + roles this person can manage. Super admins get a wildcard."""
    try:
        if profile.is_super_admin:
            return {
                "data": [],
                "is_super_admin": True,
                "note": "super admin — all stores",
            }
        rows = (
            await db.execute(
                select(StoreMembership).where(
                    StoreMembership.profile_id == profile.id
                )
            )
        ).scalars().all()
        return {
            "data": [
                {
                    "store_id": m.store_id,
                    "role": m.role.value if isinstance(m.role, StoreRole) else m.role,
                    "membership_id": m.id,
                }
                for m in rows
            ],
            "is_super_admin": False,
        }
    except Exception as e:
        logger.exception("/api/v1/me/stores failed")
        raise HTTPException(500, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")


@router.get("/api/v1/me/can-admin")
async def can_admin(
    slug: str,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Used by the buyer Vibe admin (safiya.beliaman.com/admin). Returns
    whether the signed-in person may edit the storefront for `slug`."""
    if profile.is_super_admin:
        return {"can_admin": True, "role": "super_admin", "slug": slug}
    row = (
        await db.execute(
            select(StoreMembership)
            .where(StoreMembership.profile_id == profile.id)
            .where(StoreMembership.store_slug == slug)
        )
    ).scalar_one_or_none()
    if row is None:
        return {"can_admin": False, "role": None, "slug": slug}
    return {
        "can_admin": True,
        "role": row.role.value if isinstance(row.role, StoreRole) else row.role,
        "slug": slug,
    }


class InviteIn(BaseModel):
    email: str
    role: StoreRole = StoreRole.STAFF


@router.get("/api/v1/stores/{store_id}/members")
async def list_members(
    store_id: str,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not await _can_manage(db, profile, store_id):
        raise HTTPException(403, "no access to this store")
    rows = (
        await db.execute(
            select(StoreMembership, BeliAmanProfile)
            .outerjoin(BeliAmanProfile, BeliAmanProfile.id == StoreMembership.profile_id)
            .where(StoreMembership.store_id == store_id)
            .order_by(StoreMembership.created_at)
        )
    ).all()
    return {"data": [_serialize_member(m, p) for (m, p) in rows]}


@router.post("/api/v1/stores/{store_id}/members", status_code=201)
async def invite_member(
    store_id: str,
    body: InviteIn,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not await _can_manage(db, profile, store_id, owner_only=True):
        raise HTTPException(403, "store owner only")
    email_lc = body.email.lower()
    existing = (
        await db.execute(
            select(StoreMembership)
            .where(StoreMembership.store_id == store_id)
            .where(StoreMembership.invited_email == email_lc)
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.role = body.role
        await db.commit()
        return {"data": _serialize_member(existing, None), "note": "updated"}
    target = (
        await db.execute(select(BeliAmanProfile).where(BeliAmanProfile.email == email_lc))
    ).scalar_one_or_none()
    m = StoreMembership(
        profile_id=target.id if target else None,
        invited_email=email_lc,
        store_id=store_id,
        role=body.role,
        invited_by_email=profile.email,
        accepted_at=datetime.now(timezone.utc) if target else None,
    )
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return {"data": _serialize_member(m, target)}


@router.delete("/api/v1/stores/{store_id}/members/{membership_id}", status_code=204)
async def revoke_member(
    store_id: str,
    membership_id: str,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
):
    if not await _can_manage(db, profile, store_id, owner_only=True):
        raise HTTPException(403, "store owner only")
    m = (
        await db.execute(
            select(StoreMembership)
            .where(StoreMembership.id == membership_id)
            .where(StoreMembership.store_id == store_id)
        )
    ).scalar_one_or_none()
    if m is None:
        raise HTTPException(404, "membership not found")
    if m.role == StoreRole.OWNER:
        owners = (
            await db.execute(
                select(StoreMembership)
                .where(StoreMembership.store_id == store_id)
                .where(StoreMembership.role == StoreRole.OWNER)
            )
        ).scalars().all()
        if len(owners) <= 1:
            raise HTTPException(400, "cannot remove the last owner")
    await db.delete(m)
    await db.commit()
    return None


class SeedIn(BaseModel):
    email: str
    store_id: str
    store_slug: str | None = None
    role: StoreRole = StoreRole.OWNER


@router.get("/api/v1/identity/overview")
async def identity_overview(
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Observability: the whole identity + ACL state in one call.

    Who has signed in (profiles), who is super admin, every store membership
    and whether it's still a pending invite. Admin-token gated.
    """
    profiles = (
        await db.execute(select(BeliAmanProfile).order_by(BeliAmanProfile.created_at))
    ).scalars().all()
    memberships = (
        await db.execute(
            select(StoreMembership, BeliAmanProfile)
            .outerjoin(BeliAmanProfile, BeliAmanProfile.id == StoreMembership.profile_id)
            .order_by(StoreMembership.created_at)
        )
    ).all()
    return {
        "identity_provider": "beli-aman-bap",
        "firebase_project": "beli-aman-prod",
        "counts": {
            "profiles": len(profiles),
            "super_admins": sum(1 for p in profiles if p.is_super_admin),
            "memberships": len(memberships),
            "pending_invites": sum(1 for (m, _p) in memberships if m.profile_id is None),
        },
        "profiles": [
            {
                "email": p.email,
                "display_name": p.display_name,
                "is_super_admin": p.is_super_admin,
                "last_seen_at": p.last_seen_at.isoformat() if p.last_seen_at else None,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in profiles
        ],
        "memberships": [
            {
                "email": m.invited_email,
                "store_id": m.store_id,
                "store_slug": m.store_slug,
                "role": m.role.value if isinstance(m.role, StoreRole) else m.role,
                "status": "active" if m.profile_id else "pending-invite",
                "linked_profile": (p.email if p else None),
                "accepted_at": m.accepted_at.isoformat() if m.accepted_at else None,
            }
            for (m, p) in memberships
        ],
    }


@router.post("/api/v1/identity/seed-membership")
async def seed_membership(
    body: SeedIn,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        return await _seed_impl(body, db)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1800:]}")


@router.post("/api/v1/identity/ensure-tables")
async def ensure_tables(_: None = Depends(require_admin_token)) -> dict[str, Any]:
    """Create the store_memberships table + add profiles.is_super_admin if
    they don't exist yet (Base.metadata.create_all is idempotent)."""
    from database import engine
    from models import Base  # registers all models
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # is_super_admin may need a manual ADD COLUMN if profiles predates it
        from sqlalchemy import text
        async with engine.begin() as conn:
            await conn.execute(text(
                "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS is_super_admin "
                "BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            await conn.execute(text(
                "ALTER TABLE store_memberships ADD COLUMN IF NOT EXISTS "
                "store_slug VARCHAR(100)"
            ))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")


async def _seed_impl(body: "SeedIn", db: AsyncSession) -> dict[str, Any]:
    """Admin backfill: grant a person access to a store before they sign in."""
    email_lc = body.email.lower()
    existing = (
        await db.execute(
            select(StoreMembership)
            .where(StoreMembership.store_id == body.store_id)
            .where(StoreMembership.invited_email == email_lc)
        )
    ).scalar_one_or_none()
    target = (
        await db.execute(select(BeliAmanProfile).where(BeliAmanProfile.email == email_lc))
    ).scalar_one_or_none()
    if existing is not None:
        existing.role = body.role
        if body.store_slug:
            existing.store_slug = body.store_slug
        if target is not None and existing.profile_id is None:
            existing.profile_id = target.id
            existing.accepted_at = datetime.now(timezone.utc)
        await db.commit()
        return {"data": {"id": existing.id, "email": email_lc, "role": body.role.value, "pending": target is None}}
    m = StoreMembership(
        profile_id=target.id if target else None,
        invited_email=email_lc,
        store_id=body.store_id,
        store_slug=body.store_slug,
        role=body.role,
        accepted_at=datetime.now(timezone.utc) if target else None,
    )
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return {"data": {"id": m.id, "email": email_lc, "role": body.role.value, "pending": target is None}}
