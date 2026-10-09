"""Partner API authorization for external merchants (e.g. Consumerland).

Same authorization model as ``auth/bot_auth.py`` (Bearer token, 401
closed-by-default) but the token resolves to a Brand row instead of a
single shared env var — each partner brand carries its own
``partner_api_key`` column in ``brands``.

Endpoints that require partner auth use ``Depends(require_partner)``.

Security model
--------------
- One Bearer key per partner Brand; rotate by updating the brand row.
- 401 (not 403) on missing/wrong — authentication failure, no scopes.
- Key comparison walks the brand table with ``hmac.compare_digest`` per
  row so a wrong key never matches byte-by-byte.
- If no brand has ``partner_api_key`` set, EVERY request is rejected with
  401 (closed-by-default).
"""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Header, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.brand import Brand


async def require_partner(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Brand:
    """Guard: 401 unless ``Authorization: Bearer <partner_api_key>`` matches a
    Brand row. Returns the matched Brand on success."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header (expected: Bearer <key>)",
        )
    presented = authorization.split(" ", 1)[1].strip()
    if not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid partner key",
        )

    result = await db.execute(select(Brand).where(Brand.partner_api_key.is_not(None)))
    for brand in result.scalars():
        if hmac.compare_digest(
            presented.encode("utf-8"), brand.partner_api_key.encode("utf-8")
        ):
            return brand

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid partner key",
    )
