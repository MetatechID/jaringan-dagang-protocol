"""Buyer password hashing and sign-in throttling (stdlib only).

Hashes are stored as ``scrypt$n$r$p$salt$hash``; never plaintext. Accounts are
created by an operator (``scripts/set-buyer-password.py``); there is no
self-service sign-up or reset.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

# scrypt cost: n=2**14, r=8, p=1 → ~16 MiB, well inside OpenSSL's default maxmem.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_LEN = 2**14, 8, 1, 32

MIN_PASSWORD_LENGTH = 8
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 15 * 60


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_LEN)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, n, r, p, salt, want = (stored or "").split("$")
        if algo != "scrypt":
            return False
        want_b = _b64d(want)
        dk = hashlib.scrypt(password.encode(), salt=_b64d(salt), n=int(n), r=int(r), p=int(p), dklen=len(want_b))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, want_b)


# Verified against when the account has no password, so a missing account
# costs the same time as a wrong password.
DUMMY_HASH = hash_password(secrets.token_hex(8))


class LoginLimiter:
    """In-memory: MAX_FAILURES failed attempts per email per window (one process)."""

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, key: str, now: float) -> deque[float]:
        q = self._failures[key]
        while q and now - q[0] > FAILURE_WINDOW_SECONDS:
            q.popleft()
        return q

    def blocked(self, key: str, now: float | None = None) -> bool:
        return len(self._recent(key, now or time.monotonic())) >= MAX_FAILURES

    def fail(self, key: str, now: float | None = None) -> None:
        now = now or time.monotonic()
        self._recent(key, now).append(now)

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)

    def reset(self) -> None:
        self._failures.clear()


login_limiter = LoginLimiter()
