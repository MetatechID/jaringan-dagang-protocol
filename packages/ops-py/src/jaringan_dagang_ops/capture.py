"""Bounded HTTP body evidence with safe metadata and targeted redaction.

Moved from the BAP's ``services/webhook_audit.py`` so every service records
partner exchanges the same way. ``Capture`` keeps the exact consumed prefix of
a body (up to ``EXCHANGE_BODY_LIMIT`` bytes) plus an optional JSONB-safe
projection; ``evidence()`` turns two captures into the columns of an exchange
row, masking secrets and account numbers before anything is persisted.
"""
from __future__ import annotations

import json
import math
import os
from decimal import Decimal
from urllib.parse import urlsplit


def capture_limit() -> int:
    raw = os.getenv("EXCHANGE_BODY_LIMIT") or os.getenv("WEBHOOK_AUDIT_BODY_LIMIT") or "65536"
    try:
        return max(0, min(int(raw), 1048576))
    except ValueError:
        return 65536


_SAFE_HEADERS = frozenset({"content-type", "content-length", "content-encoding"})


def safe_headers(headers) -> dict:
    return {str(k).lower(): str(v)[:256] for k, v in headers.items()
            if str(k).lower() in _SAFE_HEADERS}


def safe_endpoint(url: str) -> str:
    try:
        parsed = urlsplit(url)
        # Callback paths can themselves contain credentials: retain origin only.
        if parsed.netloc:
            return f"{parsed.scheme}://{parsed.hostname or ''}"[:2048]
        return parsed.path[:2048]
    except ValueError:
        return "[invalid endpoint]"


class Capture:
    def __init__(self, limit: int | None = None):
        self.limit = capture_limit() if limit is None else limit
        self.data = bytearray()
        self.length = 0
        self.consumed = False
        self.complete = False

    def feed(self, data: bytes):
        self.consumed = True
        self.length += len(data)
        self.data.extend(data[:max(0, self.limit - len(self.data))])

    def feed_all(self, data: bytes | None):
        """Capture a body that is already fully in memory."""
        self.feed(data or b"")
        self.complete = True

    def raw_body(self) -> bytes | None:
        """Exact consumed prefix; None means no body message was consumed."""
        return bytes(self.data) if self.consumed else None

    def parsed_json(self):
        """Optional JSONB projection; never jeopardize the byte evidence insert."""
        if not self.consumed:
            return None, "not_consumed"
        if self.length > self.limit:
            return None, "truncated"
        if not self.complete:
            return None, "incomplete"
        if not self.length:
            return None, "empty"

        def invalid_constant(value):
            raise ValueError("nonfinite number")

        def finite_float(value):
            number = float(value)
            if not math.isfinite(number) or (number == 0 and Decimal(value) != 0):
                raise ValueError("unsupported number")
            return number

        def validate(value, depth=0):
            if depth > 64:
                raise ValueError("excessive depth")
            if isinstance(value, str):
                if "\u0000" in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
                    raise ValueError("unsupported unicode")
            elif isinstance(value, dict):
                for key, item in value.items():
                    validate(key, depth + 1)
                    validate(item, depth + 1)
            elif isinstance(value, list):
                for item in value:
                    validate(item, depth + 1)

        def checked_object(pairs):
            # Validate even duplicate members that JSONB will discard.
            for key, item in pairs:
                validate(key)
                validate(item)
            return dict(pairs)

        try:
            value = json.loads(bytes(self.data).decode("utf-8"),
                               parse_constant=invalid_constant, parse_float=finite_float,
                               object_pairs_hook=checked_object)
            validate(value)
            # Check the exact serializer shape SQLAlchemy will send to JSONB.
            json.dumps(value, allow_nan=False).encode("utf-8")
            return value, "parsed_null" if value is None else "parsed"
        except (ValueError, UnicodeError, RecursionError, ArithmeticError):
            return None, "invalid_or_unrepresentable_json"

    def snapshot(self, headers=None) -> dict:
        """Metadata only: safe to log; raw evidence is stored in BYTEA columns."""
        _, parse_status = self.parsed_json()
        return {"json_parse_status": parse_status, "headers": safe_headers(headers or {}),
                "length": self.length if self.complete else None,
                "observed_length": self.length, "captured_length": len(self.data),
                "complete": self.complete, "not_consumed": not self.consumed,
                "truncated": self.length > self.limit, "redacted": False,
                "encoding": "raw-bytes", "omission": "not_consumed" if not self.consumed else None}


# --- redaction -----------------------------------------------------------------

def _norm(key: str) -> str:
    return "".join(ch for ch in key.lower() if ch.isalnum())


# Replaced entirely.
_SECRET_KEYS = frozenset({
    "password", "passwd", "secret", "clientsecret", "token", "accesstoken", "refreshtoken",
    "idtoken", "apikey", "apisecret", "otp", "otpcode", "pin", "cvv", "cvc", "cvn",
    "privatekey", "authorization", "sessiontoken", "webhooktoken", "callbacktoken",
})
# Masked down to the last four characters: enough to tell accounts apart in a dispute.
_ACCOUNT_KEYS = frozenset({
    "accountnumber", "accountno", "bankaccount", "bankaccountnumber", "bankaccountno",
    "beneficiaryaccountno", "beneficiaryaccountnumber", "destinationaccountno",
    "cardnumber", "pan", "nik", "ktp", "virtualaccountno", "vanumber",
})
# Compound names (``partner_api_key``, ``callback_secret``) are secrets too.
_SECRET_SUFFIXES = ("secret", "token", "password", "passwd", "apikey", "privatekey")
REDACTED = "[redacted]"


def _is_secret(nk: str) -> bool:
    return nk in _SECRET_KEYS or nk.endswith(_SECRET_SUFFIXES)


def _mask_tail(value) -> str:
    s = str(value)
    return ("*" * max(0, len(s) - 4)) + s[-4:] if len(s) > 4 else "*" * len(s)


def redact_json(value, depth: int = 0):
    """``(redacted_copy, changed)`` for a parsed JSON value."""
    if depth > 64:
        return value, False
    if isinstance(value, dict):
        out, changed = {}, False
        for key, item in value.items():
            nk = _norm(str(key))
            if _is_secret(nk) and item not in (None, ""):
                out[key], changed = REDACTED, True
            elif nk in _ACCOUNT_KEYS and isinstance(item, (str, int)) and item != "":
                out[key], changed = _mask_tail(item), True
            else:
                out[key], sub = redact_json(item, depth + 1)
                changed = changed or sub
        return out, changed
    if isinstance(value, list):
        out, changed = [], False
        for item in value:
            red, sub = redact_json(item, depth + 1)
            out.append(red)
            changed = changed or sub
        return out, changed
    return value, False


def evidence(capture: Capture, headers=None, redact: bool = True) -> dict:
    """``{snapshot, raw, json, redacted}`` ready for an exchange row.

    When the body is complete JSON holding secrets or account numbers, the
    stored raw bytes become the re-serialized redacted JSON (so the exact bytes
    are no longer kept for that body) and ``redacted`` is True.
    """
    snap = capture.snapshot(headers)
    value, status = capture.parsed_json()
    raw = capture.raw_body()
    stored_json = value
    redacted = False
    if redact and status == "parsed":
        red, changed = redact_json(value)
        if changed:
            stored_json, redacted = red, True
            raw = json.dumps(red, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            snap.update(redacted=True, encoding="redacted-json")
    return {"snapshot": snap, "raw": raw, "json": stored_json, "redacted": redacted,
            "parse_status": status}
