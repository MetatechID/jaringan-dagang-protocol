"""Outbound partner calls: an httpx transport that records every exchange.

Call sites keep their own ``httpx.AsyncClient(...)`` and only add a transport::

    async with httpx.AsyncClient(timeout=15.0,
                                 transport=audited("jubelio", "rates", refs={"order_id": oid})) as c:
        resp = await c.post(url, json=payload)

Behaviour is unchanged for the caller: the same response comes back and the
same exceptions propagate. On the side, one exchange row (exact bytes sent,
bounded response, status, latency, ``error_class``, ``partner_code``, trace
and business keys) and one client span are written. ``propagate=True`` adds a
W3C ``traceparent`` header; use it only for our own services, never partners.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx

from . import context, exchanges, spans
from .capture import Capture, evidence, safe_endpoint

# Bound at import: tests often monkeypatch ``httpx.AsyncClient`` module-wide.
_ASYNC_CLIENT = httpx.AsyncClient


class AuditedTransport(httpx.AsyncBaseTransport):
    def __init__(self, integration: str, operation: str, *, inner: httpx.AsyncBaseTransport | None = None,
                 propagate: bool = False, idempotent: bool | None = None, refs: dict | None = None,
                 endpoint: str | None = None):
        self.integration, self.operation = integration, operation
        self.inner = inner or httpx.AsyncHTTPTransport()
        self.propagate, self.idempotent = propagate, idempotent
        self.refs = dict(refs or {})
        self.endpoint = endpoint

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        method = request.method.upper()
        idempotent = exchanges.is_idempotent(method, self.idempotent)
        endpoint = self.endpoint or safe_endpoint(str(request.url))
        async with spans.span(f"http.client {self.integration}.{self.operation}", kind="client",
                              attributes={"http.method": method, "endpoint": endpoint},
                              refs=self.refs) as sp:
            if self.propagate:
                request.headers["traceparent"] = context.format_traceparent(sp.ctx.trace_id, sp.ctx.span_id)
            rec = exchanges.base_record(direction="outbound", integration=self.integration,
                                        operation=self.operation, method=method, endpoint=endpoint,
                                        refs=self.refs)
            rec.update(started_at=datetime.now(UTC), status=None, outcome="no_response", error=None)
            sent_body = Capture()
            received = Capture()
            response_headers: dict = {}
            sent = False
            t0 = time.monotonic()
            try:
                try:
                    sent_body.feed_all(await request.aread())
                except Exception:
                    # Streaming request body we can't buffer: record that it wasn't captured.
                    pass
                sent = True  # from here on the partner may have received the request
                response = await self.inner.handle_async_request(request)
                out_headers = response.headers
                try:
                    if response.is_stream_consumed:
                        # Already read (e.g. httpx.MockTransport): content is decoded,
                        # so drop the encoding headers or the client decodes twice.
                        raw = decoded = response.content
                        out_headers = httpx.Headers([(k, v) for k, v in response.headers.multi_items()
                                                     if k.lower() not in ("content-encoding", "content-length")])
                    else:
                        # Raw wire bytes: aread() would content-decode, and the
                        # client decodes again when the caller reads the body.
                        raw = b"".join([chunk async for chunk in response.aiter_raw()])
                        decoded = _decoded(response, raw)
                finally:
                    await response.aclose()
                response_headers = dict(response.headers)
                received.feed_all(decoded)
                status = response.status_code
                error_class = exchanges.classify_status(status)
                rec.update(status=status, outcome="response" if status < 400 else "http_error",
                           error_class=error_class)
                sp.set(**{"http.status_code": status, "error_class": error_class})
                if status >= 500:
                    sp.fail(f"HTTP {status}")
                extensions = {k: v for k, v in response.extensions.items() if k != "network_stream"}
                return httpx.Response(status, headers=out_headers, content=raw,
                                      request=request, extensions=extensions)
            except BaseException as exc:
                error_class = exchanges.classify_exception(exc, sent=sent, idempotent=idempotent) \
                    if isinstance(exc, Exception) else exchanges.LOCAL_ERROR
                rec.update(outcome="transport_error" if isinstance(exc, httpx.HTTPError) else "local_error",
                           error=type(exc).__name__[:64], error_class=error_class)
                sp.set(error_class=error_class)
                raise
            finally:
                rec.update(completed_at=datetime.now(UTC),
                           duration_ms=int((time.monotonic() - t0) * 1000))
                req_ev = evidence(sent_body, dict(request.headers))
                res_ev = evidence(received, response_headers)
                rec.update(request=req_ev["snapshot"], response=res_ev["snapshot"],
                           request_body_raw=req_ev["raw"], response_body_raw=res_ev["raw"],
                           request_body_json=req_ev["json"], response_body_json=res_ev["json"],
                           redacted=req_ev["redacted"] or res_ev["redacted"],
                           partner_code=exchanges.partner_code(res_ev["json"]))
                if rec.get("partner_code"):
                    sp.set(partner_code=rec["partner_code"])
                await exchanges.write(rec)

    async def aclose(self) -> None:
        await self.inner.aclose()


def _decoded(response: httpx.Response, raw: bytes) -> bytes:
    """Content-decoded copy of ``raw`` for the evidence row (gzip etc.)."""
    if not response.headers.get("content-encoding"):
        return raw
    try:
        return httpx.Response(response.status_code, headers=response.headers, content=raw).read()
    except Exception:
        return raw


def audited(integration: str, operation: str, **kwargs) -> AuditedTransport:
    """Shorthand for ``AuditedTransport(integration, operation, **kwargs)``."""
    return AuditedTransport(integration, operation, **kwargs)


def instrument(client, integration: str, operation: str, **kwargs):
    """Record every request ``client`` makes, keeping the transport it already has.

    For call sites that build their own ``httpx.AsyncClient(...)`` (and tests
    that swap in a ``MockTransport``-backed subclass)::

        async with httpx.AsyncClient(timeout=30.0) as client:
            instrument(client, "xendit", "invoice_create", refs={"order_id": oid})
            resp = await client.post(url, json=body)

    Wraps the client's default transport and any mounts (env proxies). Relies
    on httpx's ``_transport``/``_mounts`` attributes (stable since 0.18); any
    other object, e.g. a test fake, is returned untouched.
    """
    if not isinstance(client, _ASYNC_CLIENT):
        return client
    inner = getattr(client, "_transport", None)
    if isinstance(inner, AuditedTransport):
        inner.integration, inner.operation = integration, operation
        inner.refs = dict(kwargs.get("refs") or {})
        return client
    if inner is not None:
        client._transport = AuditedTransport(integration, operation, inner=inner, **kwargs)
    mounts = getattr(client, "_mounts", None)
    if isinstance(mounts, dict):
        for pattern, transport in list(mounts.items()):
            if transport is not None and not isinstance(transport, AuditedTransport):
                mounts[pattern] = AuditedTransport(integration, operation, inner=transport, **kwargs)
    return client
