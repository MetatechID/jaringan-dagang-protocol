import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from services.webhook_audit import AuditMiddleware, Capture, inbound_operation, safe_endpoint


def test_capture_preserves_raw_bytes_and_safe_metadata():
    for raw in (b'{ "buyer": {"email":"x@example.com"}, "unknown":"secret" }',
                b'\xff\x00not json', b'{malformed', b'<html>error</html>'):
        cap = Capture()
        cap.feed(raw)
        cap.complete = True
        assert cap.raw_body() == raw
        meta = cap.snapshot({"Content-Type": "application/json", "Authorization": "Bearer secret"})
        assert meta["headers"] == {"content-type": "application/json"}
        assert not meta["redacted"] and meta["complete"]
        assert "body" not in meta


def test_capture_exact_prefix_and_unconsumed():
    cap = Capture(limit=3)
    cap.feed(b"abcdef")
    cap.complete = True
    assert cap.raw_body() == b"abc"
    assert cap.snapshot()["truncated"]
    assert cap.snapshot()["length"] == 6
    partial = Capture(limit=3)
    partial.feed(b"ab")
    assert partial.raw_body() == b"ab"
    assert partial.snapshot()["length"] is None
    assert not partial.snapshot()["complete"]
    assert Capture().raw_body() is None
    assert Capture().snapshot()["not_consumed"]


def test_json_projection_scalars_duplicate_keys_and_unsafe_values():
    for raw, expected, status in (
        (b'null', None, 'parsed_null'), (b'true', True, 'parsed'),
        (b'42', 42, 'parsed'), (b'1.5', 1.5, 'parsed'),
        (b'[true,null,1]', [True, None, 1], 'parsed'),
        (b'"plain"', 'plain', 'parsed'),
        (b'{"x":1,"x":2}', {'x': 2}, 'parsed'),
    ):
        cap = Capture()
        cap.feed(raw)
        cap.complete = True
        assert cap.parsed_json() == (expected, status)
        assert cap.raw_body() == raw
    unsafe = [b'NaN', b'Infinity', b'-Infinity', b'1e9999', b'1e-9999',
              b'"\\u0000"', b'"\\ud800"', b'{"\\ud800":0}',
              b'{"x":"\\u0000","x":1}', b'\xff', b'{bad',
              b'[' * 70 + b'0' + b']' * 70]
    for raw in unsafe:
        cap = Capture()
        cap.feed(raw)
        cap.complete = True
        assert cap.parsed_json() == (None, 'invalid_or_unrepresentable_json')
        assert cap.raw_body() == raw
    partial = Capture()
    partial.feed(b'{}')
    assert partial.parsed_json() == (None, 'incomplete')
    bounded = Capture(limit=1)
    bounded.feed(b'{}')
    bounded.complete = True
    assert bounded.parsed_json() == (None, 'truncated')
    assert Capture().parsed_json() == (None, 'not_consumed')
    empty = Capture()
    empty.feed(b'')
    empty.complete = True
    assert empty.parsed_json() == (None, 'empty')
    assert partial.snapshot()['json_parse_status'] == 'incomplete'
    assert bounded.snapshot()['json_parse_status'] == 'truncated'


def test_selected_routes_and_sanitized_endpoint():
    assert inbound_operation("/webhooks/dipay/qris", "POST") == ("dipay", "qris_notify")
    assert inbound_operation("/health", "GET") is None
    assert safe_endpoint("https://example.test/secret/path?key=x") == "https://example.test"


def test_middleware_records_actual_response_and_does_not_drain(monkeypatch):
    records = []
    import services.webhook_audit as audit
    async def save(row):
        records.append(row)
    monkeypatch.setattr(audit, "write_audit", save)

    async def app(scope, receive, send):
        # Simulate an early authentication rejection without reading body.
        await send({"type": "http.response.start", "status": 401, "headers": []})
        await send({"type": "http.response.body", "body": b'{"detail":"invalid"}'})

    messages = []
    async def send(message):
        messages.append(message)
    async def receive():
        raise AssertionError("middleware must not drain early-rejected body")

    scope = {"type": "http", "method": "POST", "path": "/api/v1/partner/orders",
             "headers": [(b"authorization", b"Bearer secret")]}
    asyncio.run(AuditMiddleware(app)(scope, receive, send))
    assert messages[-1]["body"] == b'{"detail":"invalid"}'
    row = records[0]
    assert row["status"] == 401 and row["outcome"] == "http_error"
    assert row["request"]["not_consumed"]
    assert row["response_body_raw"] == b'{"detail":"invalid"}'
    assert row["response_body_json"] == {"detail": "invalid"}
    assert row["request_body_json"] is None
    assert row["request"]["json_parse_status"] == "not_consumed"


def test_asgi_preserves_malformed_binary_request_and_response(monkeypatch):
    import services.webhook_audit as audit
    rows = []
    request_bytes = b'\xff\x00{broken json with secret'
    response_bytes = b'<html>unrecognized error</html>\xfe'
    async def save(row):
        rows.append(row)
    monkeypatch.setattr(audit, "write_audit", save)
    async def app(scope, receive, send):
        assert (await receive())["body"] == request_bytes
        await send({"type": "http.response.start", "status": 400, "headers": []})
        await send({"type": "http.response.body", "body": response_bytes})
    async def receive():
        return {"type": "http.request", "body": request_bytes}
    async def send(message):
        pass
    asyncio.run(AuditMiddleware(app)({"type": "http", "method": "POST", "path": "/webhooks/dipay/qris"}, receive, send))
    assert rows[0]["request_body_raw"] == request_bytes
    assert rows[0]["response_body_raw"] == response_bytes
    assert rows[0]["request"]["complete"] and rows[0]["response"]["complete"]
    assert rows[0]["request_body_json"] is None and rows[0]["response_body_json"] is None
    assert rows[0]["request"]["json_parse_status"] == "invalid_or_unrepresentable_json"
    assert rows[0]["response"]["json_parse_status"] == "invalid_or_unrepresentable_json"


def test_exact_safe_bytes_and_empty_response():
    cap = Capture()
    raw = b'{ "responseCode": "2005200", "ok": true }'
    cap.feed(raw)
    cap.complete = True
    assert cap.raw_body() == raw
    assert not cap.snapshot()["redacted"]
    empty = Capture()
    empty.feed(b"")
    empty.complete = True
    assert empty.raw_body() == b""
    assert Capture().raw_body() is None


def test_outer_wrapper_sees_500_and_times_final_chunk_not_background(monkeypatch):
    from fastapi import FastAPI
    from starlette.background import BackgroundTask
    from starlette.responses import JSONResponse
    from starlette.testclient import TestClient
    import services.webhook_audit as audit
    rows = []
    async def save(row):
        rows.append(row)
    monkeypatch.setattr(audit, "write_audit", save)
    app = FastAPI()
    @app.post("/webhooks/dipay/qris")
    async def callback():
        return JSONResponse({"responseCode": "2005200"}, background=BackgroundTask(asyncio.sleep, 0.08))
    @app.post("/webhooks/dipay/remit")
    async def fail():
        raise RuntimeError("sensitive exception text")
    with TestClient(AuditMiddleware(app), raise_server_exceptions=False) as client:
        assert client.post("/webhooks/dipay/qris").status_code == 200
        assert client.post("/webhooks/dipay/remit").status_code == 500
    assert rows[0]["duration_ms"] < 70
    assert rows[0]["response_body_raw"] == b'{"responseCode":"2005200"}'
    assert rows[1]["status"] == 500
    assert rows[1]["response"]["complete"]
    assert rows[1]["error"] == "processing_error"


def test_outbound_attempts_preserve_status_on_broken_body_and_bound_capture(monkeypatch):
    import httpx
    from services import partner_webhook as sender
    rows = []
    async def save(row):
        rows.append(row)
    monkeypatch.setattr(sender, "write_audit", save)
    monkeypatch.setattr(sender, "_RETRY_DELAYS", (0, 0))
    monkeypatch.setenv("WEBHOOK_AUDIT_BODY_LIMIT", "32")
    class Stream(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            yield b"{" + b"x" * 100
            raise RuntimeError("must not be reached for oversized capture")
        async def aclose(self):
            self.closed = True
    streams = []
    class Transport:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def handle_async_request(self, request):
            stream = Stream()
            streams.append(stream)
            return httpx.Response(503 if len(streams) < 3 else 204, stream=stream)
    monkeypatch.setattr(sender, "CallbackTransport", Transport)
    assert asyncio.run(sender.send_payment_callback(url="https://example.test/token?secret=x", secret="s", payload={}))
    assert len(rows) == 3 and all(s.closed for s in streams)
    assert len({r["delivery_id"] for r in rows}) == 1
    assert [r["attempt"] for r in rows] == [1, 2, 3]
    assert rows[-1]["status"] == 204
    assert rows[-1]["error"] == "response_capture_incomplete"
    assert all(r["response"]["truncated"] for r in rows)
    assert all(r["response_body_json"] is None for r in rows)
    assert all(r["response"]["json_parse_status"] == "truncated" for r in rows)
    assert all(r["response_body_raw"] == b"{" + b"x" * 31 for r in rows)
    assert all(r["endpoint"] == "https://example.test" for r in rows)


def test_writer_failure_is_best_effort(monkeypatch, caplog):
    import database
    from services.webhook_audit import write_audit
    def unavailable():
        raise RuntimeError("password=do-not-log")
    monkeypatch.setattr(database, "async_session", unavailable)
    asyncio.run(write_audit({}))
    assert "write_failed" in caplog.text
    assert "do-not-log" not in caplog.text


def test_registration_dipay_paid_callback_and_duplicate_join(monkeypatch):
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from auth.partner_auth import require_partner
    from database import get_db
    from routers import partner, webhooks_dipay as dipay
    from services import webhook_audit as audit, partner_webhook as sender
    from config import settings

    rows, tasks = [], []
    brand = SimpleNamespace(id="brand-test", payment_provider="dipay",
                            partner_callback_url="https://partner.example/hook", partner_callback_secret="secret")
    order = SimpleNamespace(id="order-test", brand_id=brand.id, external_order_id="EXT-1",
                            amount_idr=100000, invoice_id=None, invoice_provider=None,
                            payment_url=None, expires_at=None, status="pending", paid_at=None)
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock(), refresh=AsyncMock())
    async def get_session():
        yield db
    async def authenticated(request: Request):
        audit.attach(request, brand=brand)
        return brand
    async def create(*args, **kwargs):
        return order
    async def invoice(*args, **kwargs):
        order.invoice_id = "q-join"
        order.invoice_provider = "dipay"
        order.payment_url = "https://pay.example/pay/order-test"
        order.expires_at = datetime.now(timezone.utc)
    async def resolved(*args):
        return brand, None, None, order
    async def save(row):
        rows.append(row)
    async def notify(order_id):
        await sender.notify_partner_paid(order, brand)
    original_create_task = asyncio.create_task
    def track(coro):
        task = original_create_task(coro)
        tasks.append(task)
        return task
    class Transport:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def handle_async_request(self, request):
            assert sender.build_signature_header("secret", request.content,
                   timestamp=int(request.headers["x-oito-signature"].split("t=")[1].split(",")[0])) == request.headers["x-oito-signature"]
            return httpx.Response(200, stream=Body())
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"error":"webhook not configured"}'
        async def aclose(self):
            pass
    monkeypatch.setattr(audit, "write_audit", save)
    monkeypatch.setattr(sender, "write_audit", save)
    monkeypatch.setattr(sender, "CallbackTransport", Transport)
    monkeypatch.setattr(partner, "create_partner_order", create)
    monkeypatch.setattr(partner, "_create_invoice", invoice)
    monkeypatch.setattr(dipay.dipay_client, "require_callback_public_key", lambda: None)
    monkeypatch.setattr(dipay, "_verify_inbound_signature", AsyncMock())
    monkeypatch.setattr(dipay, "_resolve_targets", resolved)
    monkeypatch.setattr(dipay.dipay_client, "resolve_config", lambda b: {})
    monkeypatch.setattr(dipay.dipay_client, "query_qris", AsyncMock(return_value={
        "originalPartnerReferenceNo": "q-join", "latestTransactionStatus": "00",
        "amount": {"value": "100000.00", "currency": "IDR"}}))
    monkeypatch.setattr(dipay.partner_webhook, "notify_partner_paid_bg", notify)
    monkeypatch.setattr(dipay.asyncio, "create_task", track)
    app = FastAPI()
    app.include_router(partner.router)
    app.include_router(dipay.router)
    app.dependency_overrides[get_db] = get_session
    app.dependency_overrides[require_partner] = authenticated
    with TestClient(AuditMiddleware(app)) as client:
        response = client.post("/api/v1/partner/orders", json={
            "external_order_id": "EXT-1", "amount_idr": 100000, "description": "test",
            "buyer": {"name": "Sensitive buyer", "email": "buyer@example.test", "phone": "123456"}})
        assert response.status_code == 200, response.text
        for _ in range(2):
            response = client.post("/webhooks/dipay/qris", json={"originalPartnerReferenceNo": "q-join"})
            assert response.status_code == 200
        # Let scheduled callbacks complete before TestClient cancels its loop.
        async def wait_callbacks():
            await asyncio.gather(*[t for t in tasks if t.get_coro().__name__ == "notify"])
        client.portal.call(wait_callbacks)
    assert order.status == "paid" and order.paid_at is not None
    selected = [r for r in rows if r["operation"] in {"register_order", "qris_notify", "payment.paid"}]
    assert len(selected) == 5  # Duplicate notifications preserve existing callback semantics.
    assert {r["partner_order_id"] for r in selected} == {"order-test"}
    assert {r["external_order_id"] for r in selected} == {"EXT-1"}
    assert {r["dipay_reference"] for r in selected} == {"q-join"}
    outbound = [r for r in selected if r["direction"] == "outbound"]
    assert all(json.loads(r["response_body_raw"]) == {"error": "webhook not configured"} for r in outbound)
    registration = next(r for r in rows if r["operation"] == "register_order")
    assert json.loads(registration["request_body_raw"])["buyer"]["email"] == "buyer@example.test"
    assert all(r["request_body_raw"] is not None for r in rows)
    assert registration["request_body_json"]["buyer"]["email"] == "buyer@example.test"
    assert all(r["response_body_json"] == {"error": "webhook not configured"} for r in outbound)
    assert all(r["request_body_json"] == json.loads(r["request_body_raw"]) for r in outbound)


