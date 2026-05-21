"""Bot-token authorization for the Beckn REST surface (Task B3a).

A separate authorization dependency from Firebase-based ``get_current_profile``
(deps.py). The B3 MCP bot is NOT a Firebase customer; it presents a static
bearer token configured via the ``BOT_API_TOKEN`` env var on Vercel.

Endpoints that require bot auth use ``Depends(require_bot)``. Existing
Firebase-auth routes (``routers/orders.py``, ``routers/payments.py``, etc.)
are unaffected — they continue to use ``get_current_profile``.

Security model
--------------
- Single shared secret per environment. Rotate by updating ``BOT_API_TOKEN``
  on Vercel and on the bot's VM at the same time.
- 401 (not 403) on missing/wrong/empty — this is an authentication failure,
  not an authorization one (no concept of "this token has insufficient scope"
  in the bot surface today).
- The token MUST be sent as ``Authorization: Bearer <token>``. Other schemes
  (``Token``, ``X-Bot-Token``, etc.) are rejected — keep the surface narrow.
- If ``BOT_API_TOKEN`` is unset on the server, EVERY request is rejected
  with 401 (closed-by-default). The deploy must explicitly set the env var.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException, status


def _expected_token() -> str | None:
    """Return the configured bot token, or ``None`` if unset/blank.

    Read at every call so that a Vercel env-var rotation takes effect
    without a redeploy (matches the ``BECKN_ORDER_FLOW`` pattern).
    """
    raw = os.environ.get("BOT_API_TOKEN", "")
    raw = raw.strip()
    return raw or None


def require_bot(authorization: str | None = Header(None)) -> None:
    """Guard: 401 unless ``Authorization: Bearer <BOT_API_TOKEN>`` matches.

    Use as a FastAPI dependency on routes that should be reachable ONLY
    by the B3 jd-sell MCP bot (NOT by Firebase-authenticated storefront
    customers, NOT by the admin cockpit).
    """
    expected = _expected_token()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Bot auth is not configured on this server "
                "(BOT_API_TOKEN env var is unset)."
            ),
        )

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header (expected: Bearer <token>)",
        )

    presented = authorization.split(" ", 1)[1].strip()
    if not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bot token",
        )

    # Constant-time comparison to defeat byte-by-byte timing side channels.
    # hmac.compare_digest is only fully constant-time when the two inputs are
    # equal-length byte strings; for differing lengths it still returns False
    # but in non-constant time. That residual leak is acceptable here (it
    # reveals only the configured token's length, not its contents).
    presented_b = presented.encode("utf-8")
    expected_b = expected.encode("utf-8")
    if not hmac.compare_digest(presented_b, expected_b):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bot token",
        )
