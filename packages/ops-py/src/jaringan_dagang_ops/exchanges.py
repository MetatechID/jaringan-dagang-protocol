"""Partner exchange records: one row per HTTP exchange with a partner, either direction.

The app persists rows through ``configure(recorder=...)``; this module builds
the record and classifies the outcome so every service answers "was it us or
the partner?" the same way (see ``docs/specs/2026-11-11-commerce-operability.md``
in the private repo, §5.4).
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from . import context

_LOG = logging.getLogger(__name__)

Recorder = Callable[[dict], Awaitable[None]]

_state: dict[str, Any] = {"recorder": None, "timeout": 0.75}

# Outbound classes
OK = "ok"
RETRYABLE = "retryable"      # partner unavailable: connect error, 408, 429, 5xx, timeout before send
AMBIGUOUS = "ambiguous"      # sent, no answer, operation not idempotent: reconcile, never resend blindly
REJECTED = "rejected"        # partner refused (other 4xx): see partner_code
LOCAL_ERROR = "local_error"  # our code failed before sending or while handling the answer
# Inbound classes
DUPLICATE = "duplicate"
AUTH_FAILED = "auth_failed"
INVALID_PAYLOAD = "invalid_payload"
HANDLER_ERROR = "handler_error"

_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


def configure(*, recorder: Recorder | None, timeout: float = 0.75) -> None:
    _state.update(recorder=recorder, timeout=timeout)


async def write(record: dict) -> None:
    """Persist on the app's own connection; never raises, logs no payloads."""
    recorder = _state["recorder"]
    if recorder is None:
        return
    try:
        async with asyncio.timeout(_state["timeout"]):
            await recorder(record)
    except Exception:
        _LOG.warning("exchange persistence failed integration=%s operation=%s",
                     record.get("integration"), record.get("operation"))


def is_idempotent(method: str, idempotent: bool | None) -> bool:
    return idempotent if idempotent is not None else method.upper() in _IDEMPOTENT_METHODS


def classify_status(status: int | None) -> str:
    if status is None:
        return RETRYABLE
    if 200 <= status < 300:
        return OK
    if status in (408, 429) or status >= 500:
        return RETRYABLE
    return REJECTED


def classify_exception(exc: BaseException, *, sent: bool, idempotent: bool) -> str:
    """Map a transport exception to a class. ``sent`` = the request left our process."""
    import httpx  # local: keep the module importable without httpx for inbound-only users

    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return RETRYABLE
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.WriteError,
                        httpx.RemoteProtocolError, httpx.TimeoutException, httpx.NetworkError)):
        if not sent:
            return RETRYABLE
        return RETRYABLE if idempotent else AMBIGUOUS
    if isinstance(exc, httpx.HTTPError):
        return RETRYABLE if (idempotent or not sent) else AMBIGUOUS
    return LOCAL_ERROR


def classify_inbound(status: int | None, *, raised: bool) -> str:
    if raised or status is None or status >= 500:
        return HANDLER_ERROR
    if status < 300:
        return OK
    if status in (401, 403):
        return AUTH_FAILED
    return INVALID_PAYLOAD


_CODE_KEYS = ("responseCode", "response_code", "error_code", "errorCode", "code", "status_code")


def partner_code(body: Any) -> str | None:
    """The provider's own error/status code from a JSON body (SNAP ``responseCode``,
    Xendit ``error_code``, ...), searched one level deep."""
    candidates = [body]
    if isinstance(body, dict):
        candidates += [v for k, v in body.items() if k in ("error", "errors", "status", "data") and isinstance(v, dict)]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        for key in _CODE_KEYS:
            value = item.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
                return str(value)[:64]
    return None


_VERSION_SEGMENT = re.compile(r"^v\d+(\.\d+)?$")


def path_operation(method: str, path: str) -> str:
    """Operation name for generic request helpers: ``"POST /v2/invoices/{id}"``.

    Segments carrying digits (other than ``v1``/``v1.0``) or longer than 32
    characters become ``{id}``, so one operation groups across orders.
    """
    segments = [s for s in str(path).split("?", 1)[0].split("/") if s]
    norm = ["{id}" if (len(s) > 32 or (any(c.isdigit() for c in s) and not _VERSION_SEGMENT.match(s))) else s
            for s in segments]
    return f"{method.upper()} /{'/'.join(norm)}"[:64]


def base_record(*, direction: str, integration: str, operation: str, method: str,
                endpoint: str, refs: dict | None = None) -> dict:
    """Common fields, including the current trace and business keys."""
    ctx = context.current()
    rec = {"direction": direction, "integration": integration, "operation": operation,
           "method": method, "endpoint": endpoint,
           "trace_id": ctx.trace_id if ctx else None, "span_id": ctx.span_id if ctx else None}
    bound = context.refs()
    for key in context.REF_KEYS:
        if key in bound:
            rec[key] = bound[key]
    # Explicit refs win and may carry app-specific keys (the recorder drops
    # whatever its table has no column for).
    rec.update({k: str(v) for k, v in (refs or {}).items() if v is not None})
    return rec
