"""Auth routes — Firebase token exchange + passwordless OTP login.

Three sign-in surfaces converge on the same Firebase ID token shape:

  POST /api/v1/auth/exchange       — Google SSO (called after Firebase
                                     ``signInWithPopup``); materializes the
                                     profile and returns it.
  POST /api/v1/auth/otp/request    — Issue a 6-digit OTP, deliver via WA or
                                     email. Generic 200 either way.
  POST /api/v1/auth/otp/verify     — Verify the code, auto-merge / create the
                                     profile, return a Firebase custom token
                                     that the SDK signs in with.
  POST /api/v1/auth/password/login — Email + password for operator-created
                                     accounts; returns a Firebase custom token
                                     like /otp/verify.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import passwords
from auth.firebase import mint_custom_token
from database import get_db
from deps import _get_or_create_profile_by_contact, get_current_profile
from models.profile import BeliAmanProfile
from services import otp_store
from services.email_sender import send_otp as send_email_otp
from services.wa_sender import send_otp as send_wa_otp

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


_E164 = re.compile(r"^\+\d{8,15}$")
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _normalize(channel: str, contact: str) -> str | None:
    contact = (contact or "").strip()
    if channel == "wa":
        # Accept "62812…" / "0812…" / "+62812…"; we coerce to E.164.
        digits = re.sub(r"\D", "", contact)
        if digits.startswith("0"):
            digits = "62" + digits[1:]
        if not digits.startswith("62") and len(digits) >= 8:
            digits = "62" + digits
        e164 = "+" + digits
        return e164 if _E164.match(e164) else None
    if channel == "email":
        e = contact.lower()
        return e if _EMAIL.match(e) else None
    return None


# ------------------------ /exchange ------------------------------------------


@router.post("/exchange")
async def exchange_token(profile: BeliAmanProfile = Depends(get_current_profile)) -> dict:
    """Validate the Bearer token; return the profile JSON."""
    return {
        "profile": {
            "id": profile.id,
            "google_sub": profile.google_sub,
            "email": profile.email,
            "display_name": profile.display_name,
            "photo_url": profile.photo_url,
            "phone_e164": profile.phone_e164,
            "created_at": profile.created_at.isoformat(),
        }
    }


# ------------------------ /otp/request ---------------------------------------


class OtpRequestIn(BaseModel):
    channel: str = Field(..., pattern="^(wa|email)$")
    contact: str


# Always return this body, no matter what — prevents user enumeration.
_GENERIC_OK = {"ok": True, "message": "If this contact is reachable, a code has been sent."}


@router.post("/otp/request")
async def otp_request(body: OtpRequestIn, db: AsyncSession = Depends(get_db)) -> dict:
    """Issue a fresh 6-digit code and dispatch it via the appropriate channel."""
    normalized = _normalize(body.channel, body.contact)
    if normalized is None:
        # Same generic response — don't leak the channel/format check either.
        return _GENERIC_OK

    result = await otp_store.issue(db, body.channel, normalized)
    await db.commit()
    if result.rate_limited:
        logger.info("[otp] rate-limited: channel=%s contact=%s", body.channel, normalized)
        return _GENERIC_OK

    if body.channel == "wa":
        sent = await send_wa_otp(normalized, result.code)
    else:
        sent = await send_email_otp(normalized, result.code)
    if not sent:
        logger.error("[otp] delivery failed: channel=%s contact=%s", body.channel, normalized)

    return _GENERIC_OK


# ------------------------ /otp/verify ----------------------------------------


class OtpVerifyIn(BaseModel):
    channel: str = Field(..., pattern="^(wa|email)$")
    contact: str
    code: str = Field(..., pattern=r"^\d{6}$")


@router.post("/otp/verify")
async def otp_verify(body: OtpVerifyIn, db: AsyncSession = Depends(get_db)) -> dict:
    """Verify the code → mint a Firebase custom token bound to the profile uid."""
    normalized = _normalize(body.channel, body.contact)
    if normalized is None:
        raise HTTPException(status_code=400, detail="invalid_contact")

    result = await otp_store.verify(db, body.channel, normalized, body.code)
    if not result.ok:
        await db.commit()
        # Map all failure reasons to 400 with a single error code; the SDK
        # shows the same "wrong or expired code" message either way.
        raise HTTPException(status_code=400, detail="invalid_code")

    profile = await _get_or_create_profile_by_contact(
        db, channel=body.channel, contact=normalized
    )
    await db.commit()

    custom_token = mint_custom_token(profile.id)
    return {
        "custom_token": custom_token,
        "profile": {
            "id": profile.id,
            "google_sub": profile.google_sub,
            "email": profile.email,
            "display_name": profile.display_name,
            "photo_url": profile.photo_url,
            "phone_e164": profile.phone_e164,
            "created_at": profile.created_at.isoformat(),
        },
    }


# ------------------------ /password/login ------------------------------------


class PasswordLoginIn(BaseModel):
    email: str = Field(..., max_length=255)
    password: str = Field(..., max_length=256)


_INVALID_CREDENTIALS = "invalid_credentials"


@router.post("/password/login")
async def password_login(body: PasswordLoginIn, db: AsyncSession = Depends(get_db)) -> dict:
    """Email + password → Firebase custom token bound to the profile uid.

    Unknown email, no password set and wrong password all return the same 401
    after the same scrypt work. Five failures per email in 15 minutes → 429.
    """
    email = (body.email or "").strip().lower()
    if passwords.login_limiter.blocked(email):
        raise HTTPException(status_code=429, detail="too_many_attempts")

    profile = None
    if _EMAIL.match(email):
        profile = (
            await db.execute(select(BeliAmanProfile).where(BeliAmanProfile.email == email))
        ).scalar_one_or_none()
    stored = profile.password_hash if profile is not None else None
    if not passwords.verify_password(body.password, stored or passwords.DUMMY_HASH) or not stored:
        passwords.login_limiter.fail(email)
        raise HTTPException(status_code=401, detail=_INVALID_CREDENTIALS)

    passwords.login_limiter.clear(email)
    profile.last_seen_at = datetime.now(timezone.utc)
    await db.commit()
    return {
        "custom_token": mint_custom_token(profile.id),
        "profile": {
            "id": profile.id,
            "google_sub": profile.google_sub,
            "email": profile.email,
            "display_name": profile.display_name,
            "photo_url": profile.photo_url,
            "phone_e164": profile.phone_e164,
            "created_at": profile.created_at.isoformat(),
        },
    }
