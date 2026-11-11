"""Trace context: W3C ``traceparent`` ids plus business keys, held in contextvars.

A commerce flow spans many traces (checkout now, payment webhook later,
release days later), so every record carries two things: the ``trace_id`` of
its causal chain and the business keys it touches (``order_id``, ...). Both
live here so any code can read them without threading arguments.
"""
from __future__ import annotations

import contextvars
import re
import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from types import MappingProxyType

_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16

# Business keys we index everywhere. Anything else goes into span attributes.
REF_KEYS = ("order_id", "cart_id", "partner_order_id", "brand_id", "store_id", "provider_ref", "job_id")


def new_trace_id() -> str:
    while True:
        tid = secrets.token_hex(16)
        if tid != _ZERO_TRACE:
            return tid


def new_span_id() -> str:
    while True:
        sid = secrets.token_hex(8)
        if sid != _ZERO_SPAN:
            return sid


def parse_traceparent(value: str | None) -> tuple[str, str] | None:
    """``(trace_id, parent_span_id)`` from a valid version-00 header, else None."""
    if not value:
        return None
    m = _TRACEPARENT.match(value.strip().lower())
    if not m or m.group(1) == _ZERO_TRACE or m.group(2) == _ZERO_SPAN:
        return None
    return m.group(1), m.group(2)


def format_traceparent(trace_id: str, span_id: str) -> str:
    return f"00-{trace_id}-{span_id}-01"


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    request_id: str | None = None


_ctx: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar("jd_ops_trace", default=None)
_refs: contextvars.ContextVar[Mapping[str, str]] = contextvars.ContextVar(
    "jd_ops_refs", default=MappingProxyType({}))  # never mutated: bind_refs copies


def current() -> TraceContext | None:
    return _ctx.get()


def current_trace_id() -> str | None:
    ctx = _ctx.get()
    return ctx.trace_id if ctx else None


def current_span_id() -> str | None:
    ctx = _ctx.get()
    return ctx.span_id if ctx else None


def outbound_traceparent() -> str | None:
    """Header value for a call to one of our own services (None outside a trace)."""
    ctx = _ctx.get()
    return format_traceparent(ctx.trace_id, ctx.span_id) if ctx else None


@contextmanager
def use(ctx: TraceContext) -> Iterator[TraceContext]:
    token = _ctx.set(ctx)
    try:
        yield ctx
    finally:
        _ctx.reset(token)


def start(traceparent: str | None = None, *, request_id: str | None = None,
          trace_id: str | None = None) -> TraceContext:
    """A new root (or remote-child) context. Does not install it; see ``use``."""
    parsed = parse_traceparent(traceparent)
    if parsed:
        return TraceContext(trace_id=parsed[0], span_id=new_span_id(), parent_span_id=parsed[1],
                            request_id=request_id)
    return TraceContext(trace_id=trace_id or new_trace_id(), span_id=new_span_id(), request_id=request_id)


def child() -> TraceContext:
    """A child of the current span, or a fresh root when there is none."""
    ctx = _ctx.get()
    if ctx is None:
        return start()
    return replace(ctx, span_id=new_span_id(), parent_span_id=ctx.span_id)


def refs() -> dict[str, str]:
    return dict(_refs.get())


def bind_refs(**values: object) -> None:
    """Attach business keys to the rest of the current task (None values are ignored)."""
    merged = dict(_refs.get())
    for key, value in values.items():
        if value is not None and value != "":
            merged[key] = str(value)
    _refs.set(merged)


@contextmanager
def refs_scope(**values: object) -> Iterator[None]:
    token = _refs.set(dict(_refs.get()))
    try:
        bind_refs(**values)
        yield
    finally:
        _refs.reset(token)


def reset_refs() -> None:
    _refs.set({})
