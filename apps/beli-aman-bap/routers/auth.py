"""POST /api/v1/auth/exchange — verify a Firebase ID token, materialize the profile."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from deps import get_current_profile
from models.profile import BeliAmanProfile

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/exchange")
async def exchange_token(profile: BeliAmanProfile = Depends(get_current_profile)) -> dict:
    """Validate the Bearer token; return the profile JSON.

    The frontend calls this right after Firebase sign-in to materialize the
    user server-side and get back the canonical profile shape.
    """
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
