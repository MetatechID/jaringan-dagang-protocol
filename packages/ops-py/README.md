# jaringan-dagang-ops

Operability primitives shared by the Jaringan Dagang services (BAP, seller,
bot). No configuration or credentials live here; each app wires its own
storage.

| Module | What it does |
|---|---|
| `context` | W3C `traceparent` parse/format, trace and span ids, business keys (`order_id`, `cart_id`, …) in contextvars. |
| `spans` | `async with span("name"):` OTel-shaped span records written through `spans.configure(service=..., sink=...)`. |
| `asgi` | `TraceMiddleware` (adopt or mint the trace, `X-Trace-ID`, root span) and `ExchangeMiddleware` (inbound partner exchange evidence, driven by a route map). |
| `http` | `instrument(client, integration, operation)` wraps an existing `httpx.AsyncClient`'s transport (`AuditedTransport`) so each outbound exchange and a client span are recorded. Responses and exceptions are unchanged for the caller. `propagate=True` adds `traceparent`, for our own services only. |
| `exchanges` | Record building and the error classes: `ok`, `retryable`, `ambiguous`, `rejected`, `local_error` (outbound); `ok`, `duplicate`, `auth_failed`, `invalid_payload`, `handler_error` (inbound). |
| `capture` | Bounded body capture (`EXCHANGE_BODY_LIMIT`, default 64 KiB) and targeted redaction of secrets and account numbers. |
| `logs` | `configure_logging(service)`: JSON log lines with `trace_id`, `span_id` and business keys. |

```python
from jaringan_dagang_ops import TraceMiddleware, ExchangeMiddleware, instrument, spans, exchanges

spans.configure(service="bap", sink=write_span)          # async def write_span(record: dict)
exchanges.configure(recorder=write_exchange)              # async def write_exchange(record: dict)
app = TraceMiddleware(ExchangeMiddleware(app, route_map=routes))

async with httpx.AsyncClient(timeout=15) as client:
    instrument(client, "biteship", "rates", refs={"order_id": order_id})
    resp = await client.post(url, json=payload)
```

Install next to the app: `pip install ./packages/ops-py`. Tests: `pytest packages/ops-py/tests`.
