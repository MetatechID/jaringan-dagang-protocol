"""BAP wiring for ``jaringan_dagang_ops``.

- Where evidence goes: ``partner_exchanges`` and ``obs_spans``, written on a
  small pool of their own so evidence never competes with business queries
  for the main pool (``database.py`` runs with ``pool_size=1``).
- Which inbound requests are partner calls (``inbound_operation``).
- How handlers bind resolved business objects to the row being recorded
  (``attach``).

``install(app)`` wraps the ASGI app; everything else is imported by callers.
"""
from __future__ import annotations

import json
import logging
import re

from jaringan_dagang_ops import context, exchanges, spans
from jaringan_dagang_ops.asgi import ExchangeMiddleware, TraceMiddleware
from jaringan_dagang_ops.asgi import attach as _attach
from jaringan_dagang_ops.capture import Capture, capture_limit, safe_endpoint, safe_headers  # noqa: F401
from jaringan_dagang_ops.http import audited  # noqa: F401  (re-export for clients)

_LOG = logging.getLogger(__name__)

SERVICE = "bap"
SCOPE_KEY = "partner_exchange"

_sessionmaker = None


def evidence_session():
    """Session on the evidence pool (created on first use)."""
    global _sessionmaker
    if _sessionmaker is None:
        from config import settings
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        url = settings.database_url
        pool = dict(pool_size=2, max_overflow=0, pool_timeout=0.5, pool_pre_ping=True) \
            if url.startswith("postgresql") else {}
        _sessionmaker = async_sessionmaker(create_async_engine(url, **pool), class_=AsyncSession,
                                           expire_on_commit=False)
    return _sessionmaker()


def _fit(table, values: dict) -> dict:
    """Keep only the table's columns; trim strings to the column length."""
    out = {}
    for column in table.columns:
        if column.name not in values:
            continue
        value = values[column.name]
        length = getattr(column.type, "length", None)
        if isinstance(value, str) and length:
            value = value[:length]
        out[column.name] = value
    return out


async def record_exchange(record: dict) -> None:
    """Insert one exchange row. Raises; ``exchanges.write`` times out and swallows."""
    from models.partner_exchange import PartnerExchange
    from sqlalchemy import JSON
    values = _fit(PartnerExchange.__table__, record)
    for side in ("request", "response"):
        if (values.get(side) or {}).get("json_parse_status") == "parsed_null":
            values[f"{side}_body_json"] = JSON.NULL
    values.setdefault("redacted", False)
    async with evidence_session() as session:
        session.add(PartnerExchange(**values))
        await session.commit()


async def record_span(record: dict) -> None:
    from models.obs_span import ObsSpan
    values = _fit(ObsSpan.__table__, record)
    attributes = dict(record.get("attributes") or {})
    for key in context.REF_KEYS:  # keys without a column (store_id) stay findable
        if record.get(key) is not None and key not in values:
            attributes[key] = record[key]
    values["attributes"] = json.loads(json.dumps(attributes, default=str))
    if record.get("end_time") and record.get("start_time"):
        values["duration_ms"] = int((record["end_time"] - record["start_time"]).total_seconds() * 1000)
    async with evidence_session() as session:
        session.add(ObsSpan(**values))
        await session.commit()


# Late-bound so tests can monkeypatch ``record_exchange`` / ``record_span``.
async def _exchange_sink(record):
    await record_exchange(record)


async def _span_sink(record):
    await record_span(record)


def configure() -> None:
    exchanges.configure(recorder=_exchange_sink)
    spans.configure(service=SERVICE, sink=_span_sink)


configure()


async def write_exchange(record: dict) -> None:
    """Best-effort write for exchanges recorded by hand (e.g. partner callbacks)."""
    await exchanges.write(record)


# --- inbound partner routes -------------------------------------------------

_INBOUND = {
    ("POST", "/webhooks/xendit/invoice"): ("xendit", "invoice"),
    ("POST", "/webhooks/xendit/disbursement"): ("xendit", "disbursement"),
    ("POST", "/webhooks/xendit/refund"): ("xendit", "refund"),
    ("POST", "/v1.0/qr/qr-mpm-notify"): ("dipay", "qris_notify"),
    ("POST", "/webhooks/dipay/qris"): ("dipay", "qris_notify"),
    ("POST", "/webhooks/dipay/remit"): ("dipay", "remit"),
    ("POST", "/webhooks/sento/invoice"): ("sento", "invoice"),
    ("POST", "/webhooks/sento/remit"): ("sento", "remit"),
    ("POST", "/webhooks/oy/invoice"): ("oy", "invoice"),
    ("POST", "/webhooks/midtrans"): ("midtrans", "notification"),
    ("POST", "/webhooks/biteship/tracking"): ("biteship", "tracking"),
    ("POST", "/webhooks/jubelio"): ("jubelio", "webhook"),
    ("POST", "/api/v1/partner/orders"): ("partner", "register_order"),
}
_PARTNER_STATUS = re.compile(r"^/api/v1/partner/orders/[^/]+$")


def inbound_operation(path: str, method: str):
    """``(integration, operation[, endpoint_template])`` for partner calls, else None."""
    method = (method or "").upper()
    path = path.rstrip("/") if path != "/" else path
    hit = _INBOUND.get((method, path))
    if hit:
        return hit
    if method == "GET" and _PARTNER_STATUS.match(path):
        return "partner", "order_status", "/api/v1/partner/orders/{order_id}"
    return None


def attach(request, *, brand=None, order=None, reference=None, **keys):
    """Bind already authenticated/resolved business objects to this request's
    exchange row and to the trace context (so spans and outbound calls made
    later in the request carry them too).

    ``order`` is a ``PartnerOrder``; storefront orders and carts go in as
    ``order_id=`` / ``cart_id=``. ``reference`` is the provider's id.
    Evidence only: never raises into the handler.
    """
    try:
        values = dict(keys)
        if brand is not None:
            values["brand_id"] = getattr(brand, "id", None)
        if order is not None:
            values.update(brand_id=getattr(order, "brand_id", None), partner_order_id=getattr(order, "id", None),
                          external_order_id=getattr(order, "external_order_id", None))
            invoice_id = getattr(order, "invoice_id", None)
            if invoice_id:
                values.setdefault("provider_ref", invoice_id)
                values.setdefault("dipay_reference", invoice_id)
        if reference is not None:
            values.update(provider_ref=reference, dipay_reference=reference)
        values = {k: str(v)[:255] for k, v in values.items() if v is not None}
        if request is not None:
            _attach(request, scope_key=SCOPE_KEY, **values)
        context.bind_refs(**{k: v for k, v in values.items() if k in context.REF_KEYS})
    except Exception:  # noqa: BLE001
        _LOG.debug("exchange attach skipped", exc_info=True)


_REF_PREFIXES = (("partner-", "partner_order_id"), ("order-", "order_id"), ("cart-", "cart_id"))
_UUID = re.compile(r"[0-9a-fA-F-]{36}")


def bind_reference(reference, *, request=None, provider_ref=None, brand=None, **keys):
    """Bind our own ``order-<id>`` / ``cart-<id>`` / ``partner-<id>`` external
    id (optionally suffixed, e.g. ``-release``) plus the provider's id. Call it
    after the callback is authenticated or verified."""
    ref = str(reference or "")
    for prefix, key in _REF_PREFIXES:
        if ref.startswith(prefix):
            match = _UUID.match(ref[len(prefix):])
            if match:
                keys.setdefault(key, match.group(0))
            break
    attach(request, brand=brand, provider_ref=provider_ref, **keys)


def install(app):
    """Outermost wrappers, outside Starlette's error middleware so generated
    500s are captured too. Trace first so the exchange row carries the trace."""
    return TraceMiddleware(ExchangeMiddleware(app, route_map=inbound_operation, scope_key=SCOPE_KEY))
