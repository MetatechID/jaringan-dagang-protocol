"""Passwordless-login OTP store — sha256-hashed codes with TTL + attempt cap.

Python port of karya1's ``apps/app/server/utils/otpStore.ts``. The store is
channel-aware so the same contact value (e.g. an email) never collides with
a phone-number namespace.

Defaults:
  - TTL: 7 minutes
  - Resend rate-limit: 60 seconds (returns ``rate_limited`` without issuing).
  - Max verify attempts per code: 5.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.otp_code import OtpCode

Channel = Literal["wa", "email"]

TTL = timedelta(minutes=7)
RESEND_INTERVAL = timedelta(seconds=60)
MAX_ATTEMPTS = 5


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def generate_code() -> str:
    """6-digit numeric code, zero-padded. Cryptographic randomness."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _as_utc(dt: datetime) -> datetime:
    """Return ``dt`` as a UTC-aware datetime.

    SQLite's ``DateTime(timezone=True)`` round-trips as naive datetimes
    (no tzdata in the engine), so we coerce on read. Postgres always
    returns aware values and this is a no-op there.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


@dataclass
class IssueOk:
    code: str
    rate_limited: bool = False


@dataclass
class IssueRateLimited:
    code: str = ""
    rate_limited: bool = True


IssueResult = IssueOk | IssueRateLimited


async def issue(db: AsyncSession, channel: Channel, contact: str) -> IssueResult:
    """Issue (or refresh) a code for ``(channel, contact)``.

    Returns ``IssueRateLimited`` if a code was issued for this pair less than
    60 seconds ago (the prior code stays valid). Otherwise upserts a new code
    and returns it (plaintext) for the caller to deliver via WA / email.
    """
    now = datetime.now(timezone.utc)

    existing = (
        await db.execute(
            select(OtpCode).where(
                and_(OtpCode.channel == channel, OtpCode.contact == contact)
            )
        )
    ).scalar_one_or_none()

    if existing and (now - _as_utc(existing.issued_at)) < RESEND_INTERVAL:
        return IssueRateLimited()

    code = generate_code()
    if existing:
        existing.code_hash = hash_code(code)
        existing.attempts = 0
        existing.issued_at = now
        existing.expires_at = now + TTL
    else:
        db.add(
            OtpCode(
                channel=channel,
                contact=contact,
                code_hash=hash_code(code),
                attempts=0,
                issued_at=now,
                expires_at=now + TTL,
            )
        )
    await db.flush()
    return IssueOk(code=code)


@dataclass
class VerifyOk:
    ok: Literal[True] = True


@dataclass
class VerifyFail:
    ok: Literal[False] = False
    reason: str = "mismatch"  # not_found | expired | too_many_attempts | mismatch


VerifyResult = VerifyOk | VerifyFail


async def verify(db: AsyncSession, channel: Channel, contact: str, code: str) -> VerifyResult:
    """Single-use verify. Successful match deletes the row."""
    row = (
        await db.execute(
            select(OtpCode).where(
                and_(OtpCode.channel == channel, OtpCode.contact == contact)
            )
        )
    ).scalar_one_or_none()

    if row is None:
        return VerifyFail(reason="not_found")

    now = datetime.now(timezone.utc)
    if now > _as_utc(row.expires_at):
        await db.execute(delete(OtpCode).where(OtpCode.id == row.id))
        await db.flush()
        return VerifyFail(reason="expired")

    if row.attempts >= MAX_ATTEMPTS:
        return VerifyFail(reason="too_many_attempts")

    row.attempts += 1
    await db.flush()

    if not hmac.compare_digest(hash_code(code), row.code_hash):
        return VerifyFail(reason="mismatch")

    await db.execute(delete(OtpCode).where(OtpCode.id == row.id))
    await db.flush()
    return VerifyOk()
