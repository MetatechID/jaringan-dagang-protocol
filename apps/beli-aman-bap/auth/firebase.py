"""Firebase Admin initialization + ID-token verification.

Lazy-initialized so Vercel cold-starts don't pay the ~300ms init cost on
every invocation that doesn't actually need to verify a token.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Any

import firebase_admin
from firebase_admin import auth as fb_auth, credentials

from config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_firebase_app() -> firebase_admin.App:
    """Initialize firebase_admin once and reuse the App across calls."""
    cred_json = settings.firebase_service_account_json or os.environ.get(
        "FIREBASE_SERVICE_ACCOUNT_JSON", ""
    )
    if not cred_json:
        raise RuntimeError(
            "FIREBASE_SERVICE_ACCOUNT_JSON env var is empty. Paste the entire "
            "service-account JSON file contents into this var (see infra/beli-aman/README.md)."
        )

    try:
        cred_dict = json.loads(cred_json)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            "FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON. Paste the raw "
            "file contents (no surrounding quotes, no escaping)."
        ) from e

    cred = credentials.Certificate(cred_dict)
    app_name = "beli-aman"
    try:
        existing = firebase_admin.get_app(app_name)
        return existing
    except ValueError:
        return firebase_admin.initialize_app(cred, name=app_name)


def verify_id_token(id_token: str) -> dict[str, Any]:
    """Verify a Firebase ID token and return its decoded claims.

    Raises a ValueError on any verification failure.
    """
    app = get_firebase_app()
    try:
        return fb_auth.verify_id_token(id_token, app=app, check_revoked=False)
    except Exception as e:
        logger.warning("Firebase ID token verification failed: %s", e)
        raise ValueError(f"Invalid Firebase ID token: {e}") from e
