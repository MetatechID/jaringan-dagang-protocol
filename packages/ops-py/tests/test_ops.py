import gzip
import json
import logging

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from jaringan_dagang_ops import asgi, capture, context, exchanges, logs, spans
from jaringan_dagang_ops.http import audited


@pytest.fixture
def sinks():
    got = {"spans": [], "exchanges": []}

    async def write_span(rec):
        got["spans"].append(rec)

    async def write_exchange(rec):
        got["exchanges"].append(rec)

    spans.configure(service="test", sink=write_span)
    exchanges.configure(recorder=write_exchange)
    yield got
    spans.configure(service="unknown", sink=None)
    exchanges.configure(recorder=None)


# --- context -------------------------------------------------------------------

def test_traceparent_roundtrip_and_rejects_invalid():
    tid, sid = context.new_trace_id(), context.new_span_id()
    assert context.parse_traceparent(context.format_traceparent(tid, sid)) == (tid, sid)
    for bad in (None, "", "garbage", "00-" + "0" * 32 + "-" + "1" * 16 + "-01",
                "00-" + "a" * 32 + "-" + "0" * 16 + "-01", "01-" + "a" * 32 + "-" + "b" * 16 + "-01x"):
        assert context.parse_traceparent(bad) is None


def test_start_adopts_remote_parent_and_child_links():
    tp = context.format_traceparent("a" * 32, "b" * 16)
    ctx = context.start(tp)
    assert ctx.trace_id == "a" * 32 and ctx.parent_span_id == "b" * 16 and ctx.span_id != "b" * 16
    with context.use(ctx):
        kid = context.child()
    assert kid.trace_id == ctx.trace_id and kid.parent_span_id == ctx.span_id


def test_refs_scope_is_restored():
    context.reset_refs()
    with context.refs_scope(order_id="o1"):
        context.bind_refs(brand_id="b1", cart_id=None)
        assert context.refs() == {"order_id": "o1", "brand_id": "b1"}
    assert context.refs() == {}


# --- capture -------------------------------------------------------------------

def test_redaction_masks_secrets_and_account_tails_only_when_present():
    c = capture.Capture()
    c.feed_all(json.dumps({"amount": 1000, "bank_account_number": "1234567890",
                           "nested": {"accessToken": "xyz", "note": "keep"}}).encode())
    ev = capture.evidence(c)
    assert ev["redacted"] is True
    assert ev["json"] == {"amount": 1000, "bank_account_number": "******7890",
                          "nested": {"accessToken": "[redacted]", "note": "keep"}}
    assert b"xyz" not in ev["raw"] and b"1234567890" not in ev["raw"]
    plain = capture.Capture()
    plain.feed_all(b'{"amount": 1}')
    ev2 = capture.evidence(plain)
    assert ev2["redacted"] is False and ev2["raw"] == b'{"amount": 1}'


# --- outbound transport --------------------------------------------------------

def _transport(handler):
    return httpx.MockTransport(handler)


async def test_outbound_ok_records_exchange_and_returns_same_response(sinks):
    def handler(request):
        assert "traceparent" not in request.headers  # partners never get our trace
        return httpx.Response(201, json={"id": "inv_1", "status": "PENDING"})

    with context.refs_scope(order_id="ord-1"):
        async with httpx.AsyncClient(transport=audited("xendit", "invoice_create",
                                                       inner=_transport(handler))) as c:
            resp = await c.post("https://api.example.test/v2/invoices?key=secret", json={"amount": 5})
    assert resp.status_code == 201 and resp.json() == {"id": "inv_1", "status": "PENDING"}
    [ex] = sinks["exchanges"]
    assert ex["direction"] == "outbound" and ex["integration"] == "xendit"
    assert ex["error_class"] == "ok" and ex["status"] == 201
    assert ex["endpoint"] == "https://api.example.test"  # origin only, no query secrets
    assert ex["order_id"] == "ord-1"
    assert ex["request_body_json"] == {"amount": 5}
    assert ex["response_body_json"]["id"] == "inv_1"
    [sp] = sinks["spans"]
    assert sp["kind"] == "client" and sp["trace_id"] == ex["trace_id"] and sp["span_id"] == ex["span_id"]


@pytest.mark.parametrize("status,klass", [(400, "rejected"), (409, "rejected"), (429, "retryable"),
                                          (503, "retryable")])
async def test_outbound_status_classes_and_partner_code(sinks, status, klass):
    def handler(request):
        return httpx.Response(status, json={"responseCode": "4012401", "responseMessage": "nope"})

    async with httpx.AsyncClient(transport=audited("dipay", "qris_create", inner=_transport(handler))) as c:
        resp = await c.post("https://dipay.test/qr", json={})
    assert resp.status_code == status
    [ex] = sinks["exchanges"]
    assert ex["error_class"] == klass and ex["partner_code"] == "4012401"


async def test_outbound_read_timeout_is_ambiguous_for_post_and_reraised(sinks):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    async with httpx.AsyncClient(transport=audited("sento", "remit", inner=_transport(handler))) as c:
        with pytest.raises(httpx.ReadTimeout):
            await c.post("https://sento.test/remit", json={"amount": 1})
    [ex] = sinks["exchanges"]
    assert ex["error_class"] == "ambiguous" and ex["error"] == "ReadTimeout"
    assert sinks["spans"][0]["status"] == "error"


async def test_outbound_timeout_on_get_is_retryable_and_connect_error_too(sinks):
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=audited("oy", "status", inner=_transport(slow))) as c:
        with pytest.raises(httpx.ReadTimeout):
            await c.get("https://oy.test/status")
    async with httpx.AsyncClient(transport=audited("oy", "create", inner=_transport(down))) as c:
        with pytest.raises(httpx.ConnectError):
            await c.post("https://oy.test/create", json={})
    assert [e["error_class"] for e in sinks["exchanges"]] == ["retryable", "retryable"]


async def test_propagate_adds_traceparent_for_internal_calls(sinks):
    seen = {}

    def handler(request):
        seen["tp"] = request.headers.get("traceparent")
        return httpx.Response(200, json={})

    root = context.start()
    with context.use(root):
        async with httpx.AsyncClient(transport=audited("seller", "escrow_status", propagate=True,
                                                       inner=_transport(handler))) as c:
            await c.patch("https://seller.test/api/internal/escrow-orders/x", json={})
    trace_id, parent = context.parse_traceparent(seen["tp"])
    assert trace_id == root.trace_id and parent == sinks["exchanges"][0]["span_id"]


async def test_gzip_response_is_decoded_in_evidence_and_for_caller(sinks):
    body = json.dumps({"ok": True}).encode()

    def handler(request):
        return httpx.Response(200, content=gzip.compress(body), headers={"content-encoding": "gzip",
                                                                          "content-type": "application/json"})

    async with httpx.AsyncClient(transport=audited("biteship", "rates", inner=_transport(handler))) as c:
        resp = await c.get("https://biteship.test/rates")
    assert resp.json() == {"ok": True}
    assert sinks["exchanges"][0]["response_body_json"] == {"ok": True}


async def test_recorder_failure_never_breaks_the_call():
    async def boom(rec):
        raise RuntimeError("db down")

    exchanges.configure(recorder=boom)
    spans.configure(service="t", sink=boom)
    try:
        async with httpx.AsyncClient(transport=audited("x", "y", inner=_transport(
                lambda r: httpx.Response(200, text="fine")))) as c:
            assert (await c.get("https://x.test/")).text == "fine"
    finally:
        exchanges.configure(recorder=None)
        spans.configure(service="unknown", sink=None)


# --- inbound middleware --------------------------------------------------------

def _app():
    app = FastAPI()

    @app.post("/webhooks/pay")
    async def pay(request: Request):
        body = await request.json()
        if request.headers.get("x-callback-token") != "ok":
            return JSONResponse({"detail": "bad token"}, status_code=401)
        context.bind_refs(order_id=body.get("order_id"))
        if body.get("dup"):
            asgi.attach(request, error_class="duplicate", provider_ref="inv-9")
        return {"received": True}

    @app.get("/plain")
    async def plain():
        return {"trace": context.current_trace_id()}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaput")

    def routes(path, method):
        return ("payco", "invoice") if (method, path) == ("POST", "/webhooks/pay") else None

    return asgi.TraceMiddleware(asgi.ExchangeMiddleware(app, route_map=routes))


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                             base_url="http://t")


async def test_trace_minted_exposed_and_root_span_written(sinks):
    async with await _client(_app()) as c:
        r = await c.get("/plain")
    assert r.headers["x-trace-id"] == r.json()["trace"]
    [sp] = sinks["spans"]
    assert sp["kind"] == "server" and sp["trace_id"] == r.json()["trace"]
    assert sp["attributes"]["http.route"] == "/plain" and sp["status"] == "ok"
    assert sinks["exchanges"] == []  # not a partner route


async def test_trace_adopted_from_internal_caller(sinks):
    tp = context.format_traceparent("c" * 32, "d" * 16)
    async with await _client(_app()) as c:
        r = await c.get("/plain", headers={"traceparent": tp})
    assert r.headers["x-trace-id"] == "c" * 32
    assert sinks["spans"][0]["parent_span_id"] == "d" * 16


async def test_inbound_exchange_classes_and_handler_keys(sinks):
    async with await _client(_app()) as c:
        ok = await c.post("/webhooks/pay", json={"order_id": "o-7"}, headers={"x-callback-token": "ok"})
        dup = await c.post("/webhooks/pay", json={"order_id": "o-8", "dup": True},
                           headers={"x-callback-token": "ok"})
        bad = await c.post("/webhooks/pay", json={"order_id": "o-9"}, headers={"x-callback-token": "no"})
    assert (ok.status_code, dup.status_code, bad.status_code) == (200, 200, 401)
    first, second, third = sinks["exchanges"]
    assert first["error_class"] == "ok" and first["order_id"] == "o-7" and first["integration"] == "payco"
    assert first["trace_id"] == ok.headers["x-trace-id"]
    assert first["request_body_json"] == {"order_id": "o-7"}
    assert second["error_class"] == "duplicate" and second["provider_ref"] == "inv-9"
    assert third["error_class"] == "auth_failed"


async def test_unhandled_error_marks_root_span(sinks):
    async with await _client(_app()) as c:
        r = await c.get("/boom")
    assert r.status_code == 500
    assert sinks["spans"][0]["status"] == "error"


def test_json_logs_carry_trace_and_refs(capsys):
    handler = logging.StreamHandler()
    handler.setFormatter(logs.JsonFormatter("svc"))
    log = logging.getLogger("jd-ops-test")
    log.handlers, log.propagate = [handler], False
    with context.use(context.start()) as ctx, context.refs_scope(order_id="o-1"):
        log.warning("hello %s", "world", extra={"attempt": 2})
    line = json.loads(capsys.readouterr().err.strip())
    assert line["msg"] == "hello world" and line["trace_id"] == ctx.trace_id
    assert line["order_id"] == "o-1" and line["attempt"] == 2 and line["service"] == "svc"


class _StreamingTransport(httpx.AsyncBaseTransport):
    """Returns an unread streamed response, like the real network transport."""

    def __init__(self, body: bytes, headers: dict):
        self.body, self.headers = body, headers

    async def handle_async_request(self, request):
        return httpx.Response(200, headers=self.headers, stream=httpx.ByteStream(self.body))


async def test_streamed_gzip_response_keeps_raw_for_caller_and_decodes_evidence(sinks):
    body = json.dumps({"rates": [1, 2]}).encode()
    inner = _StreamingTransport(gzip.compress(body), {"content-encoding": "gzip",
                                                      "content-type": "application/json"})
    async with httpx.AsyncClient(transport=audited("jubelio", "rates", inner=inner)) as c:
        resp = await c.get("https://jubelio.test/rates")
    assert resp.json() == {"rates": [1, 2]}
    assert sinks["exchanges"][0]["response_body_json"] == {"rates": [1, 2]}


async def test_instrument_wraps_existing_transport_and_skips_fakes(sinks):
    from jaringan_dagang_ops.http import instrument

    class TransportClient(httpx.AsyncClient):  # how app tests swap in a mock
        def __init__(self, **kw):
            kw.setdefault("transport", _transport(lambda r: httpx.Response(200, json={"ok": True})))
            super().__init__(**kw)

    async with TransportClient(timeout=5.0) as c:
        assert instrument(c, "jubelio", "rates", refs={"cart_id": "c-1"}) is c
        instrument(c, "jubelio", "rates", refs={"cart_id": "c-1"})  # idempotent
        assert (await c.get("https://partner.example.test/rates")).json() == {"ok": True}
    [ex] = sinks["exchanges"]
    assert ex["integration"] == "jubelio" and ex["cart_id"] == "c-1" and ex["error_class"] == "ok"
    fake = object()
    assert instrument(fake, "x", "y") is fake


def test_path_operation_groups_ids():
    assert exchanges.path_operation("post", "/v2/invoices") == "POST /v2/invoices"
    assert exchanges.path_operation("GET", "/v2/invoices/inv_123?x=1") == "GET /v2/invoices/{id}"
    assert exchanges.path_operation("POST", "/v1.0/qr/qr-mpm-generate") == "POST /v1.0/qr/qr-mpm-generate"


def test_redaction_catches_compound_secret_names():
    red, changed = capture.redact_json({"partner_api_key": "pk_live_x", "callback_secret": "s",
                                        "x_callback_token": "t", "token_type": "Bearer", "notes": "ok"})
    assert changed
    assert red == {"partner_api_key": "[redacted]", "callback_secret": "[redacted]",
                   "x_callback_token": "[redacted]", "token_type": "Bearer", "notes": "ok"}
