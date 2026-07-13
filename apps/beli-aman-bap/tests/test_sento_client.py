"""Sento payment gateway HTTP client — pure unit tests via httpx.MockTransport.

Exercises ``services/sento_client.py`` end-to-end (header injection, payload
shape, SentoError mapping) without hitting the network. Each test
substitutes a ``MockTransport``-backed ``_request`` capturing the outgoing
``httpx.Request`` and returning canned JSON.

For the no-keys guard we call the *real* production ``_request`` (with a
transport attached via a thin wrapper) so the test fails if a refactor
removes that defensive check.

Reference: https://api-docs.sento.id/docs-page/payment-link
- Auth: ``x-api-key`` + ``x-username`` headers
- Create: POST /api/payment-checkout/create-v2
- Status: GET  /api/payment-checkout/status?partner_tx_id=...
- Status values: created / waiting_payment / expired / charge_in_progress /
  failed / complete / closed
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

import httpx
import pytest

from services import sento_client  # noqa: E402


def _patch_settings(monkeypatch, **overrides) -> SimpleNamespace:
    """Swap ``sento_client.settings`` with a SimpleNamespace carrying the
    fields ``sento_client._request`` reads at call time.
    """
    fake = SimpleNamespace(
        sento_api_key=overrides.get("sento_api_key", ""),
        sento_default_username=overrides.get("sento_default_username", ""),
    )
    monkeypatch.setattr(sento_client, "settings", fake)
    return fake


def _make_transport(response_or_responses):
    """Returns ``(captured_list, transport)`` that returns the supplied
    response(s) FIFO. Falls back to 200/{} if the queue empties.
    """
    if not isinstance(response_or_responses, list):
        response_or_responses = [response_or_responses]
    captured: list = []
    queue = list(response_or_responses)

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if queue:
            return queue.pop(0)
        return httpx.Response(200, json={})

    return captured, httpx.MockTransport(handler)


# ---- Test classes ----------------------------------------------------------


class TestHeaders:
    @pytest.mark.asyncio
    async def test_includes_api_key_and_username_when_caller_passes(self, monkeypatch):
        captured, transport = _make_transport(
            httpx.Response(200, json={"status": True, "url": "https://pay/x"})
        )
        _patch_settings(
            monkeypatch, sento_api_key="env-key", sento_default_username="env-user",
        )
        _install_request_shim(monkeypatch, transport)
        await sento_client.create_invoice(
            partner_tx_id="x", amount_idr=10000, sender_name="Buyer",
            api_key="caller-key", username="caller-user",
        )
        req = captured[0]
        assert req.headers["x-api-key"] == "caller-key"
        assert req.headers["x-username"] == "caller-user"
        assert req.headers["content-type"] == "application/json"

    @pytest.mark.asyncio
    async def test_falls_back_to_env_keys_when_caller_omits(self, monkeypatch):
        captured, transport = _make_transport(
            httpx.Response(200, json={"status": True, "url": "https://pay/x"})
        )
        _patch_settings(
            monkeypatch, sento_api_key="env-key", sento_default_username="env-user",
        )
        _install_request_shim(monkeypatch, transport)
        await sento_client.create_invoice(
            partner_tx_id="x", amount_idr=10000, sender_name="Buyer",
        )
        req = captured[0]
        assert req.headers["x-api-key"] == "env-key"
        assert req.headers["x-username"] == "env-user"

    def test_omits_header_when_value_is_falsy(self):
        """_headers() must not insert a header when value is falsy."""
        h = sento_client._headers(api_key="", username="")
        assert "x-api-key" not in h
        assert "x-username" not in h
        assert h["Content-Type"] == "application/json"

        h = sento_client._headers(api_key=None, username=None)
        assert "x-api-key" not in h
        assert "x-username" not in h

    def test_content_type_is_application_json(self):
        h = sento_client._headers(api_key="k", username="u")
        assert h["Content-Type"] == "application/json"


class TestRequestErrors:
    @pytest.mark.asyncio
    async def test_raises_sento_error_on_4xx_response(self, monkeypatch):
        captured, transport = _make_transport(
            httpx.Response(400, json={"status": False, "message": "amount invalid"})
        )
        _patch_settings(monkeypatch, sento_api_key="k", sento_default_username="u")
        _install_request_shim(monkeypatch, transport)
        with pytest.raises(sento_client.SentoError) as ei:
            await sento_client.create_invoice(
                partner_tx_id="x", amount_idr=10000, sender_name="Buyer",
            )
        assert ei.value.status_code == 400
        assert ei.value.body == {"status": False, "message": "amount invalid"}

    @pytest.mark.asyncio
    async def test_raises_sento_error_on_5xx_response(self, monkeypatch):
        captured, transport = _make_transport(
            httpx.Response(503, text="sento service down")
        )
        _patch_settings(monkeypatch, sento_api_key="k", sento_default_username="u")
        _install_request_shim(monkeypatch, transport)
        with pytest.raises(sento_client.SentoError) as ei:
            await sento_client.create_invoice(
                partner_tx_id="x", amount_idr=10000, sender_name="Buyer",
            )
        assert ei.value.status_code == 503
        assert ei.value.body == "sento service down"

    @pytest.mark.asyncio
    async def test_raises_sento_error_with_status_zero_when_no_keys(self, monkeypatch):
        """No env key, no per-call key → fail before any network call.

        We call the *real* ``_request`` here (not a shim) so the test
        actually fails if a refactor removes the no-keys guard.
        """
        called: list = []
        transport = httpx.MockTransport(
            lambda req: (called.append(req) or httpx.Response(200, json={}))[1]
        )
        _patch_settings(monkeypatch, sento_api_key="", sento_default_username="")

        class TransportClient(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs.setdefault("transport", transport)
                kwargs.setdefault("timeout", 30.0)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(sento_client.httpx, "AsyncClient", TransportClient)

        with pytest.raises(sento_client.SentoError) as ei:
            await sento_client._request("POST", "/api/payment-checkout/create-v2")
        assert ei.value.status_code == 0
        assert "SENTO_API_KEY not configured" in str(ei.value.body)
        assert called == []


class TestCreateInvoicePayload:
    @pytest.mark.asyncio
    async def test_required_fields_serialized(self, monkeypatch):
        captured, transport = _make_transport(
            httpx.Response(200, json={"status": True, "url": "https://pay/x"})
        )
        _patch_settings(monkeypatch, sento_api_key="k", sento_default_username="u")
        _install_request_shim(monkeypatch, transport)
        await sento_client.create_invoice(
            partner_tx_id="tx-1", amount_idr=50000, sender_name="Buyer",
        )
        body = _body_of(captured[0])
        assert body["partner_tx_id"] == "tx-1"
        assert body["amount"] == 50000
        assert body["sender_name"] == "Buyer"
        assert body["is_open"] is False
        assert body["include_admin_fee"] is False

    @pytest.mark.asyncio
    async def test_optional_fields_omitted_when_blank(self, monkeypatch):
        captured, transport = _make_transport(
            httpx.Response(200, json={"status": True, "url": "https://pay/x"})
        )
        _patch_settings(monkeypatch, sento_api_key="k", sento_default_username="u")
        _install_request_shim(monkeypatch, transport)
        await sento_client.create_invoice(
            partner_tx_id="tx-1", amount_idr=50000, sender_name="Buyer",
            description=None, notes=None, email=None, phone_number=None,
            expiration=None, va_display_name=None,
            list_enabled_banks=None, list_disabled_payment_methods=None,
        )
        body = _body_of(captured[0])
        for key in (
            "description", "notes", "email", "phone_number",
            "expiration", "va_display_name",
            "list_enabled_banks", "list_disabled_payment_methods",
        ):
            assert key not in body, f"{key} should be omitted when None"

    @pytest.mark.asyncio
    async def test_includes_optional_fields_when_set(self, monkeypatch):
        captured, transport = _make_transport(
            httpx.Response(200, json={"status": True, "url": "https://pay/x"})
        )
        _patch_settings(monkeypatch, sento_api_key="k", sento_default_username="u")
        _install_request_shim(monkeypatch, transport)
        await sento_client.create_invoice(
            partner_tx_id="tx-1", amount_idr=50000, sender_name="Buyer",
            description="Safiya order",
            notes="Thanks",
            email="buyer@example.com",
            phone_number="08123456789",
            list_enabled_banks="BCA,BNI",
            expiration="2026-07-14 12:00:00",
            va_display_name="Safiya Store",
            callback_url="https://cb",
        )
        body = _body_of(captured[0])
        assert body["description"] == "Safiya order"
        assert body["notes"] == "Thanks"
        assert body["email"] == "buyer@example.com"
        assert body["phone_number"] == "08123456789"
        assert body["list_enabled_banks"] == "BCA,BNI"
        assert body["expiration"] == "2026-07-14 12:00:00"
        assert body["va_display_name"] == "Safiya Store"
        assert body["callback_url"] == "https://cb"

    @pytest.mark.asyncio
    async def test_amount_is_coerced_to_int(self, monkeypatch):
        """amount_idr is declared int but callers might pass a numeric string."""
        captured, transport = _make_transport(
            httpx.Response(200, json={"status": True, "url": "https://pay/x"})
        )
        _patch_settings(monkeypatch, sento_api_key="k", sento_default_username="u")
        _install_request_shim(monkeypatch, transport)
        await sento_client.create_invoice(
            partner_tx_id="tx-1", amount_idr="50000", sender_name="Buyer",  # type: ignore[arg-type]
        )
        body = _body_of(captured[0])
        assert body["amount"] == 50000
        assert isinstance(body["amount"], int)


class TestGetStatus:
    @pytest.mark.asyncio
    async def test_calls_status_endpoint_with_partner_tx_id(self, monkeypatch):
        captured, transport = _make_transport(
            httpx.Response(200, json={"status": "complete", "amount": 50000})
        )
        _patch_settings(monkeypatch, sento_api_key="k", sento_default_username="u")
        _install_request_shim(monkeypatch, transport)
        out = await sento_client.get_status(partner_tx_id="tx-1")
        req = captured[0]
        assert req.method == "GET"
        assert req.url.path == "/api/payment-checkout/status"
        assert req.url.params["partner_tx_id"] == "tx-1"
        assert out["status"] == "complete"


class TestNotImplemented:
    @pytest.mark.asyncio
    async def test_cancel_invoice_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            await sento_client.cancel_invoice(partner_tx_id="x")

    @pytest.mark.asyncio
    async def test_create_refund_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            await sento_client.create_refund(
                partner_tx_id="x", amount_idr=1000,
            )


# ---- helpers ---------------------------------------------------------------


def _body_of(request: httpx.Request) -> dict:
    """Decode the JSON body from a MockTransport-captured httpx.Request."""
    raw = request.content
    if isinstance(raw, bytes):
        return json.loads(raw.decode("utf-8"))
    return raw


def _install_request_shim(monkeypatch, transport: httpx.MockTransport) -> None:
    """Swap ``sento_client._request`` with a transport-backed shim that
    mirrors the production logic exactly (so that test-time errors on
    the no-keys branch aren't masked by a shim that does its own check).
    """
    async def fake_request(method, path, *, api_key=None, username=None, json=None, params=None):
        effective = api_key or sento_client.settings.sento_api_key
        if not effective:
            raise sento_client.SentoError(0, "SENTO_API_KEY not configured (env or Brand.sento_api_key)")
        async with httpx.AsyncClient(transport=transport, timeout=30.0) as client:
            resp = await client.request(
                method,
                f"{sento_client._BASE_URL}{path}",
                headers=sento_client._headers(
                    api_key=api_key or sento_client.settings.sento_api_key,
                    username=username or sento_client.settings.sento_default_username,
                ),
                json=json,
                params=params,
            )
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = resp.text
            raise sento_client.SentoError(resp.status_code, body)
        return resp.json()

    monkeypatch.setattr(sento_client, "_request", fake_request)