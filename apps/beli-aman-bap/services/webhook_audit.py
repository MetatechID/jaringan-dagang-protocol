"""Bounded exact HTTP body evidence with safe metadata and independent writes."""
from __future__ import annotations

import asyncio
import logging
import json
import math
from decimal import Decimal
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

_LOG = logging.getLogger(__name__)


def capture_limit() -> int:
    try:
        return max(0, min(int(os.getenv("WEBHOOK_AUDIT_BODY_LIMIT", "65536")), 1048576))
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


def attach(request, *, brand=None, order=None, reference=None):
    """Only use already authenticated/resolved business objects."""
    audit = request.scope.get("webhook_audit") if request is not None else None
    if audit is None:
        return
    if brand is not None:
        audit["brand_id"] = str(brand.id)
    if order is not None:
        audit.update(brand_id=str(order.brand_id), partner_order_id=str(order.id),
                     external_order_id=order.external_order_id, dipay_reference=order.invoice_id)
    if reference is not None:
        audit["dipay_reference"] = reference[:255]


async def write_audit(record: dict):
    """Never use the handler transaction; log no exception text or payloads."""
    try:
        from database import async_session
        from models.webhook_audit import WebhookAudit
        async with asyncio.timeout(0.75):
            async with async_session() as session:
                from sqlalchemy import JSON
                values = dict(record)
                for side in ("request", "response"):
                    if values.get(side, {}).get("json_parse_status") == "parsed_null":
                        values[f"{side}_body_json"] = JSON.NULL
                session.add(WebhookAudit(**values))
                await session.commit()
    except Exception:
        _LOG.warning("HTTP audit persistence failed reason=write_failed")


def inbound_operation(path: str, method: str):
    if method == "POST" and path in {"/v1.0/qr/qr-mpm-notify", "/webhooks/dipay/qris", "/webhooks/dipay/remit"}:
        return "dipay", "remit" if path.endswith("remit") else "qris_notify"
    if method == "POST" and path == "/api/v1/partner/orders":
        return "partner", "register_order"
    prefix = "/api/v1/partner/orders/"
    if method == "GET" and path.startswith(prefix) and path[len(prefix):] and "/" not in path[len(prefix):]:
        return "partner", "order_status"
    return None


class AuditMiddleware:
    """Outer ASGI wrapper: see ServerErrorMiddleware's actual serialized 500.

    Tee only consumed chunks. Do not drain requests. Persist after dependencies
    and background tasks clean up, but timestamp the final emitted response.
    """
    def __init__(self, app):
        self.app = app

    def __getattr__(self, name):
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        selected = inbound_operation(scope.get("path", ""), scope.get("method", "")) if scope["type"] == "http" else None
        if selected is None:
            return await self.app(scope, receive, send)
        started = time.monotonic()
        record = dict(started_at=datetime.now(timezone.utc), completed_at=None,
                      duration_ms=None, direction="inbound", integration=selected[0],
                      operation=selected[1], method=scope["method"],
                      endpoint="/api/v1/partner/orders/{order_id}" if selected[1] == "order_status" else scope["path"],
                      status=None, outcome="no_response", error=None)
        scope["webhook_audit"] = record
        incoming, outgoing = Capture(), Capture()
        request_headers = dict((k.decode("latin1"), v.decode("latin1")) for k, v in scope.get("headers", []))
        response_headers = {}

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
                    record.update(completed_at=datetime.now(timezone.utc),
                                  duration_ms=int((time.monotonic() - started) * 1000),
                                  outcome="http_error" if (record["status"] or 0) >= 400 else "response")
        try:
            await self.app(scope, tee_receive, tee_send)
        except Exception:
            record["error"] = "processing_error"
            raise
        finally:
            record["request"] = incoming.snapshot(request_headers)
            record["response"] = outgoing.snapshot(response_headers)
            record["request_body_raw"] = incoming.raw_body()
            record["response_body_raw"] = outgoing.raw_body()
            record["request_body_json"] = incoming.parsed_json()[0]
            record["response_body_json"] = outgoing.parsed_json()[0]
            await write_audit(record)
