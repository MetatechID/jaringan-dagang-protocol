"""ASGI middlewares: request tracing and inbound partner-exchange capture.

Wrap outermost, trace first so the exchange row sees the trace::

    app = TraceMiddleware(ExchangeMiddleware(app, route_map=my_routes))

Both are raw ASGI (no BaseHTTPMiddleware), so contextvars set here are the
ones the endpoint sees, and they also work through Starlette's
``add_middleware``.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from . import context, exchanges, spans
from .capture import Capture, evidence

RouteMap = Callable[[str, str], "tuple[str, str] | tuple[str, str, str] | None"]

_DEFAULT_SKIP = ("/health", "/healthz", "/ready", "/readyz", "/metrics", "/favicon.ico")


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


class TraceMiddleware:
    """Adopt a valid ``traceparent`` (our own callers) or mint a trace; expose
    ``X-Trace-ID``; write one ``http.server`` root span per request."""

    def __init__(self, app, *, skip_paths: Iterable[str] = _DEFAULT_SKIP):
        self.app = app
        self.skip_paths = tuple(skip_paths)

    def __getattr__(self, name):  # let ``app.state`` etc. reach the wrapped app
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        ctx = context.start(_header(scope, b"traceparent"))
        path = scope.get("path", "")
        record_span = scope.get("method") != "OPTIONS" and not path.startswith(self.skip_paths)
        status_holder = {"status": None}
        trace_header = ctx.trace_id.encode()

        async def send_with_trace(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                message["headers"] = [*message.get("headers", []), (b"x-trace-id", trace_header)]
            await send(message)

        started = datetime.now(UTC)
        t0 = time.monotonic()
        error = None
        token_refs = context._refs.set({})
        try:
            with context.use(ctx):
                try:
                    await self.app(scope, receive, send_with_trace)
                except BaseException as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    if record_span:
                        status = status_holder["status"]
                        route = scope.get("route")
                        route_path = getattr(route, "path", None) or path
                        await spans.emit(spans.record(
                            ctx=ctx, name=f"http.server {scope.get('method')} {route_path}", kind="server",
                            start_time=started, end_time=datetime.now(UTC),
                            status="error" if error or (status or 0) >= 500 else "ok",
                            error=error or (f"HTTP {status}" if (status or 0) >= 500 else None),
                            attributes={"http.method": scope.get("method"), "http.route": route_path,
                                        "http.target": path[:512], "http.status_code": status,
                                        "duration_ms": int((time.monotonic() - t0) * 1000)}))
        finally:
            context._refs.reset(token_refs)


class ExchangeMiddleware:
    """Record inbound partner calls selected by ``route_map(path, method)``.

    ``route_map`` returns ``(integration, operation)`` or
    ``(integration, operation, endpoint_template)`` for routes to capture.
    Handlers add business keys or override the class through
    ``scope[scope_key]`` (see ``attach``). Bodies are teed, never drained.
    """

    def __init__(self, app, *, route_map: RouteMap, scope_key: str = "partner_exchange"):
        self.app = app
        self.route_map = route_map
        self.scope_key = scope_key

    def __getattr__(self, name):
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        selected = self.route_map(scope.get("path", ""), scope.get("method", "")) \
            if scope["type"] == "http" else None
        if selected is None:
            return await self.app(scope, receive, send)
        integration, operation = selected[0], selected[1]
        endpoint = selected[2] if len(selected) > 2 else scope["path"]
        t0 = time.monotonic()
        record = exchanges.base_record(direction="inbound", integration=integration, operation=operation,
                                       method=scope["method"], endpoint=endpoint)
        record.update(started_at=datetime.now(UTC), completed_at=None, duration_ms=None,
                      status=None, outcome="no_response", error=None)
        scope[self.scope_key] = record
        incoming, outgoing = Capture(), Capture()
        request_headers = {k.decode("latin1"): v.decode("latin1") for k, v in scope.get("headers", [])}
        response_headers: dict = {}
        raised = False

        async def tee_receive():
            message = await receive()
            if message["type"] == "http.request":
                incoming.feed(message.get("body", b""))
                incoming.complete = not message.get("more_body", False)
            return message

        async def tee_send(message):
            nonlocal response_headers
            await send(message)
            if message["type"] == "http.response.start":
                record["status"] = message["status"]
                response_headers = {k.decode("latin1"): v.decode("latin1") for k, v in message.get("headers", [])}
            elif message["type"] == "http.response.body":
                outgoing.feed(message.get("body", b""))
                if not message.get("more_body", False):
                    outgoing.complete = True
                    record.update(completed_at=datetime.now(UTC),
                                  duration_ms=int((time.monotonic() - t0) * 1000),
                                  outcome="http_error" if (record["status"] or 0) >= 400 else "response")

        try:
            await self.app(scope, tee_receive, tee_send)
        except Exception:
            raised = True
            record["error"] = "processing_error"
            raise
        finally:
            if not record.get("error_class"):
                record["error_class"] = exchanges.classify_inbound(record["status"], raised=raised)
            # Business keys bound by the handler (same task) are visible here.
            for key, value in context.refs().items():
                record.setdefault(key, value)
            req_ev, res_ev = evidence(incoming, request_headers), evidence(outgoing, response_headers)
            record.update(request=req_ev["snapshot"], response=res_ev["snapshot"],
                          request_body_raw=req_ev["raw"], response_body_raw=res_ev["raw"],
                          request_body_json=req_ev["json"], response_body_json=res_ev["json"],
                          redacted=req_ev["redacted"] or res_ev["redacted"])
            await exchanges.write(record)


def attach(scope_or_request, *, scope_key: str = "partner_exchange", error_class: str | None = None,
           **keys: object) -> None:
    """Add business keys (or an inbound class such as ``duplicate``) to the
    exchange being recorded for this request. Only pass already-authenticated,
    resolved values."""
    scope = getattr(scope_or_request, "scope", scope_or_request)
    record = scope.get(scope_key) if isinstance(scope, dict) else None
    if record is None:
        return
    if error_class:
        record["error_class"] = error_class
    for key, value in keys.items():
        if value is not None:
            record[key] = str(value)[:255]
