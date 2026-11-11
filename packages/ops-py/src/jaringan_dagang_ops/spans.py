"""OTel-shaped spans written through an app-provided sink.

Same model as the bot's ``apps/bot/bridge/obs.py`` (``bot_events``): a span is
one dict with ``trace_id``, ``span_id``, ``parent_span_id``, ``name``,
``kind``, ``service``, ``start_time``, ``end_time``, ``status``, ``error``,
business keys and ``attributes``. The package never talks to a database; the
app calls ``configure(service=..., sink=...)`` with an async callable that
persists one record. Sink failures are logged and swallowed: observability
must never break the request it measures.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from . import context

_LOG = logging.getLogger(__name__)

Sink = Callable[[dict], Awaitable[None]]

_state: dict[str, Any] = {"service": "unknown", "sink": None, "timeout": 0.75}


def configure(*, service: str, sink: Sink | None, timeout: float = 0.75) -> None:
    _state.update(service=service, sink=sink, timeout=timeout)


def service_name() -> str:
    return _state["service"]


async def emit(record: dict) -> None:
    sink = _state["sink"]
    if sink is None:
        return
    try:
        async with asyncio.timeout(_state["timeout"]):
            await sink(record)
    except Exception:
        _LOG.warning("span persistence failed name=%s", record.get("name"))


def record(*, ctx: context.TraceContext, name: str, kind: str, start_time: datetime,
           end_time: datetime | None, status: str = "ok", error: str | None = None,
           attributes: dict | None = None, refs: dict | None = None) -> dict:
    keys = context.refs()
    keys.update({k: v for k, v in (refs or {}).items() if v is not None})
    rec = {
        "trace_id": ctx.trace_id, "span_id": ctx.span_id, "parent_span_id": ctx.parent_span_id,
        "name": name, "kind": kind, "service": _state["service"],
        "start_time": start_time, "end_time": end_time,
        "status": status, "error": (error or None) and str(error)[:1000],
        "attributes": dict(attributes or {}),
    }
    for key in context.REF_KEYS:
        rec[key] = keys.get(key)
    if ctx.request_id:
        rec["attributes"].setdefault("request_id", ctx.request_id)
    return rec


class Span:
    def __init__(self, name: str, kind: str, attributes: dict | None, refs: dict | None):
        self.name, self.kind = name, kind
        self.attributes = dict(attributes or {})
        self.refs = dict(refs or {})
        self.status, self.error = "ok", None
        self.ctx = context.child()

    def set(self, **attributes: Any) -> Span:
        self.attributes.update(attributes)
        return self

    def set_refs(self, **values: Any) -> Span:
        self.refs.update({k: str(v) for k, v in values.items() if v is not None})
        return self

    def fail(self, error: str | BaseException) -> Span:
        self.status = "error"
        self.error = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
        return self


@asynccontextmanager
async def span(name: str, *, kind: str = "internal", attributes: dict | None = None,
               refs: dict | None = None) -> AsyncIterator[Span]:
    """Child of the current span (or a new root). Exceptions mark it ``error`` and re-raise."""
    s = Span(name, kind, attributes, refs)
    started = datetime.now(UTC)
    t0 = time.monotonic()
    with context.use(s.ctx):
        try:
            yield s
        except BaseException as exc:  # noqa: BLE001 — recorded, then re-raised
            if s.status == "ok":
                s.fail(exc)
            raise
        finally:
            s.attributes.setdefault("duration_ms", int((time.monotonic() - t0) * 1000))
            await emit(record(ctx=s.ctx, name=name, kind=kind, start_time=started,
                              end_time=datetime.now(UTC), status=s.status, error=s.error,
                              attributes=s.attributes, refs=s.refs))
