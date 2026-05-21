"""FastAPI dependencies: DB session, current profile, admin guard.

A profile may be created via three sign-in methods (Google SSO, WA OTP, email
OTP). Each Firebase ID token resolves to exactly one ``BeliAmanProfile``:

  - Google SSO → token ``sub`` is the Google subject identifier. We look up
    by ``google_sub``; on miss, fall back to ``email`` (auto-merges into an
    existing OTP-only profile when the same person later signs in with
    Google). On both misses, we create a new profile.

  - WA / email OTP → after verify, the BAP mints a Firebase **custom token**
    with ``uid = profile.id``. The resulting ID token therefore carries
    ``sub = profile.id`` and ``firebase.sign_in_provider == "custom"``. We
    detect that branch and look up the profile by id directly.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.firebase import verify_id_token
from config import settings
from database import get_db
from models.profile import BeliAmanProfile
from models.store_membership import StoreMembership

# Network-wide super admins. These emails bypass every StoreMembership check
# and see every store on both the seller dashboard and buyer Vibe admin.
SUPER_ADMIN_EMAILS = {
    "hallucinogenplus@gmail.com",
    "lwastuargo@gmail.com",
}


async def _claim_pending_invites(db: AsyncSession, profile: BeliAmanProfile) -> None:
    """Auto-claim any pending StoreMembership invites that match this profile's email."""
    email_lc = (profile.email or "").lower()
    if not email_lc:
        return
    pending = (
        await db.execute(
            select(StoreMembership)
            .where(StoreMembership.invited_email == email_lc)
            .where(StoreMembership.profile_id.is_(None))
        )
    ).scalars().all()
    for inv in pending:
        inv.profile_id = profile.id
        inv.accepted_at = datetime.now(timezone.utc)


async def _get_or_create_profile(
    db: AsyncSession,
    *,
    google_sub: str,
    email: str,
    display_name: str | None,
    photo_url: str | None,
) -> BeliAmanProfile:
    """Google-SSO path. Look up by google_sub → email → create."""
    email_lc = (email or "").lower()
    is_super = email_lc in SUPER_ADMIN_EMAILS

    # 1) Match on google_sub (most specific).
    profile = (
        await db.execute(
            select(BeliAmanProfile).where(BeliAmanProfile.google_sub == google_sub)
        )
    ).scalar_one_or_none()

    # 2) Fall back to verified email — auto-merges an existing OTP-only profile.
    if profile is None and email_lc:
        profile = (
            await db.execute(
                select(BeliAmanProfile).where(BeliAmanProfile.email == email_lc)
            )
        ).scalar_one_or_none()
        if profile is not None and profile.google_sub is None:
            profile.google_sub = google_sub

    if profile is None:
        profile = BeliAmanProfile(
            google_sub=google_sub,
            email=email_lc or None,
            display_name=display_name,
            photo_url=photo_url,
            last_seen_at=datetime.now(timezone.utc),
            is_super_admin=is_super,
        )
        db.add(profile)
        await db.flush()
    else:
        profile.last_seen_at = datetime.now(timezone.utc)
        if email_lc and profile.email != email_lc:
            profile.email = email_lc
        if display_name and profile.display_name != display_name:
            profile.display_name = display_name
        if photo_url and profile.photo_url != photo_url:
            profile.photo_url = photo_url
        if is_super and not profile.is_super_admin:
            profile.is_super_admin = True

    await _claim_pending_invites(db, profile)
    return profile


async def _get_or_create_profile_by_contact(
    db: AsyncSession,
    *,
    channel: str,
    contact: str,
) -> BeliAmanProfile:
    """OTP path. ``channel`` is "wa" or "email", ``contact`` is E.164 phone or lowercased email.

    Auto-merge: if a profile already has this contact (e.g. a Google sign-in
    earlier set the same email, or a prior OTP sign-in set the phone), attach
    to it. Otherwise create a fresh profile.
    """
    contact_lc = contact.lower() if channel == "email" else contact

    if channel == "wa":
        existing = (
            await db.execute(
                select(BeliAmanProfile).where(BeliAmanProfile.phone_e164 == contact_lc)
            )
        ).scalar_one_or_none()
    elif channel == "email":
        existing = (
            await db.execute(
                select(BeliAmanProfile).where(BeliAmanProfile.email == contact_lc)
            )
        ).scalar_one_or_none()
    else:
        raise ValueError(f"unsupported channel: {channel!r}")

    is_super = (
        channel == "email" and contact_lc in SUPER_ADMIN_EMAILS
    )

    if existing is None:
        profile = BeliAmanProfile(
            email=contact_lc if channel == "email" else None,
            phone_e164=contact_lc if channel == "wa" else None,
            last_seen_at=datetime.now(timezone.utc),
            is_super_admin=is_super,
        )
        db.add(profile)
        await db.flush()
    else:
        profile = existing
        profile.last_seen_at = datetime.now(timezone.utc)
        if is_super and not profile.is_super_admin:
            profile.is_super_admin = True

    await _claim_pending_invites(db, profile)
    return profile


async def _get_profile_by_id(db: AsyncSession, profile_id: str) -> BeliAmanProfile | None:
    return (
        await db.execute(
            select(BeliAmanProfile).where(BeliAmanProfile.id == profile_id)
        )
    ).scalar_one_or_none()


async def get_current_profile(
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
) -> BeliAmanProfile:
    """Verify a Firebase ID token from the Authorization header and resolve to a profile."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header (expected: Bearer <id_token>)",
        )

    id_token = authorization.split(" ", 1)[1].strip()
    try:
        decoded = verify_id_token(id_token)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e)
        ) from e

    # Custom-token sign-ins (post-OTP) carry ``sub = profile.id`` because the
    # BAP set it that way in ``mint_custom_token`` at OTP-verify time.
    provider = (decoded.get("firebase") or {}).get("sign_in_provider")
    if provider == "custom":
        profile = await _get_profile_by_id(db, decoded["sub"])
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Profile not found for custom-token uid",
            )
        profile.last_seen_at = datetime.now(timezone.utc)
        return profile

    return await _get_or_create_profile(
        db,
        google_sub=decoded["sub"],
        email=decoded.get("email", ""),
        display_name=decoded.get("name"),
        photo_url=decoded.get("picture"),
    )


def require_admin_token(x_admin_token: str | None = Header(None)) -> None:
    """Guard for admin / internal-mock endpoints. 403 if header doesn't match."""
    if not x_admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing X-Admin-Token header",
        )
