"""Operability primitives shared by the Jaringan Dagang services.

- ``context``: W3C trace ids and business keys in contextvars.
- ``spans``: OTel-shaped spans written through an app-provided sink.
- ``exchanges`` / ``http`` / ``asgi``: one evidence row per partner HTTP
  exchange (inbound and outbound) with an ours-vs-partner error class.
- ``capture``: bounded body capture with targeted redaction.
- ``logs``: JSON log lines carrying trace ids.

The package holds no configuration or credentials; apps wire their own
storage with ``spans.configure`` and ``exchanges.configure``.
"""
from . import asgi, capture, context, exchanges, http, logs, spans
from .asgi import ExchangeMiddleware, TraceMiddleware, attach
from .context import bind_refs, current_trace_id, outbound_traceparent, refs_scope
from .http import AuditedTransport, audited, instrument
from .spans import span

__all__ = [
    "asgi", "capture", "context", "exchanges", "http", "logs", "spans",
    "ExchangeMiddleware", "TraceMiddleware", "attach",
    "bind_refs", "current_trace_id", "outbound_traceparent", "refs_scope",
    "AuditedTransport", "audited", "instrument", "span",
]

__version__ = "0.1.0"
