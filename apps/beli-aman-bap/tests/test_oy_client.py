"""OY Indonesia HTTP client — pure unit tests via httpx.MockTransport.

Exercises ``services/oy_client.py`` end-to-end (header injection, payload
shape, OYError mapping, default-rail fallback) without hitting the
network. Each test substitutes a ``MockTransport``-backed ``_request``
capturing the outgoing ``httpx.Request`` and returning canned JSON.

For the no-keys guard we call the *real* production ``_request`` (with a
transport attached via a thin wrapper) so the test fails if a refactor
removes that defensive check.
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

from services import oy_client  # noqa: E402


def _patch_settings(monkeypatch, **overrides) -> SimpleNamespace:
    """Swap ``oy_client.settings`` with a SimpleNamespace carrying the
    fields ``oy_client._request`` reads at call time.
    """
    fake = SimpleNamespace(
        oy_api_key=overrides.get("oy_api_key", ""),
        oy_default_username=overrides.get("oy_default_username", ""),
    )
    monkeypatch.setattr(oy_client, "settings", fake)
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
        captured, transport = _make_transport(httpx.Response(200, json={"trx_id": "T"}))
        _patch_settings(
            monkeypatch, oy_api_key="env-key", oy_default_username="env-user",
        )
        _install_request_shim(monkeypatch, transport)
        await oy_client.create_invoice(
            external_id="x", amount_idr=1000, description="d",
            api_key="caller-key", username="caller-user",
        )
        req = captured[0]
        assert req.headers["x-api-key"] == "caller-key"
        assert req.headers["x-oy-username"] == "caller-user"
        assert req.headers["content-type"] == "application/json"

    @pytest.mark.asyncio
    async def test_falls_back_to_env_keys_when_caller_omits(self, monkeypatch):
        captured, transport = _make_transport(httpx.Response(200, json={"trx_id": "T"}))
        _patch_settings(
            monkeypatch, oy_api_key="env-key", oy_default_username="env-user",
        )
        _install_request_shim(monkeypatch, transport)
        await oy_client.create_invoice(external_id="x", amount_idr=1000, description="d")
        req = captured[0]
        assert req.headers["x-api-key"] == "env-key"
        assert req.headers["x-oy-username"] == "env-user"

    def test_omits_header_when_value_is_falsy(self):
        """_headers() must not insert a header when value is falsy."""
        h = oy_client._headers(api_key="", username="")
        assert "x-api-key" not in h
        assert "x-oy-username" not in h
        assert h["Content-Type"] == "application/json"

        h = oy_client._headers(api_key=None, username=None)
        assert "x-api-key" not in h
        assert "x-oy-username" not in h

    def test_content_type_is_application_json(self):
        h = oy_client._headers(api_key="k", username="u")
        assert h["Content-Type"] == "application/json"


class TestRequestErrors:
    @pytest.mark.asyncio
    async def test_raises_oy_error_on_4xx_response(self, monkeypatch):
        captured, transport = _make_transport(
            httpx.Response(400, json={"error": "bad_request", "message": "amount invalid"})
        )
        _patch_settings(monkeypatch, oy_api_key="k", oy_default_username="u")
        _install_request_shim(monkeypatch, transport)
        with pytest.raises(oy_client.OYError) as ei:
            await oy_client.create_invoice(external_id="x", amount_idr=1000, description="d")
        assert ei.value.status_code == 400
        assert ei.value.body == {"error": "bad_request", "message": "amount invalid"}

    @pytest.mark.asyncio
    async def test_raises_oy_error_on_5xx_response(self, monkeypatch):
        captured, transport = _make_transport(httpx.Response(503, text="oy service down"))
        _patch_settings(monkeypatch, oy_api_key="k", oy_default_username="u")
        _install_request_shim(monkeypatch, transport)
        with pytest.raises(oy_client.OYError) as ei:
            await oy_client.create_invoice(external_id="x", amount_idr=1000, description="d")
        assert ei.value.status_code == 503
        assert ei.value.body == "oy service down"

    @pytest.mark.asyncio
    async def test_raises_oy_error_with_status_zero_when_no_keys(self, monkeypatch):
        """No env key, no per-call key → fail before any network call.

        We call the *real* ``_request`` here (not a shim) so the test
        actually fails if a refactor removes the no-keys guard.
        """
        called: list = []
        transport = httpx.MockTransport(
            lambda req: (called.append(req) or httpx.Response(200, json={}))[1]
        )
        _patch_settings(monkeypatch, oy_api_key="", oy_default_username="")

        # Substitute the inner httpx.AsyncClient by patching httpx.AsyncClient
        # at module level within oy_client only. Easier path: replace
        # _request with one that delegates to a transport but only AFTER
        # running the no-keys guard. Since we WANT production's guard to run,
        # we patch httpx.AsyncClient to our transport-backed class.
        class TransportClient(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs.setdefault("transport", transport)
                kwargs.setdefault("timeout", 30.0)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(oy_client.httpx, "AsyncClient", TransportClient)

        with pytest.raises(oy_client.OYError) as ei:
            await oy_client._request("POST", "/payment/create")
        assert ei.value.status_code == 0
        assert "OY_API_KEY not configured" in str(ei.value.body)
        assert called == []


class TestCreateInvoicePayload:
    @pytest.mark.asyncio
    async def test_default_payment_methods_is_qris(self, monkeypatch):
        captured, transport = _make_transport(httpx.Response(200, json={"trx_id": "T"}))
        _patch_settings(monkeypatch, oy_api_key="k", oy_default_username="u")
        _install_request_shim(monkeypatch, transport)
        await oy_client.create_invoice(external_id="x", amount_idr=1000, description="d")
        body = _body_of(captured[0])
        assert body["payment_methods"] == [{"name": "QRIS"}]
        assert body["currency"] == "IDR"
        assert body["amount"] == 1000
        assert body["external_id"] == "x"
        assert body["description"] == "d"

    @pytest.mark.asyncio
    async def test_uses_caller_payment_methods_when_provided(self, monkeypatch):
        captured, transport = _make_transport(httpx.Response(200, json={"trx_id": "T"}))
        _patch_settings(monkeypatch, oy_api_key="k", oy_default_username="u")
        _install_request_shim(monkeypatch, transport)
        rails = [
            {"name": "VA", "code": "BCA"},
            {"name": "EWALLET", "code": "OVO"},
        ]
        await oy_client.create_invoice(
            external_id="x", amount_idr=1000, description="d",
            payment_methods=rails,
        )
        body = _body_of(captured[0])
        assert body["payment_methods"] == rails

    @pytest.mark.asyncio
    async def test_includes_payer_email_when_set(self, monkeypatch):
        captured, transport = _make_transport(httpx.Response(200, json={"trx_id": "T"}))
        _patch_settings(monkeypatch, oy_api_key="k", oy_default_username="u")
        _install_request_shim(monkeypatch, transport)
        await oy_client.create_invoice(
            external_id="x", amount_idr=1000, description="d",
            payer_email="buyer@example.com",
        )
        body = _body_of(captured[0])
        assert body["payer_email"] == "buyer@example.com"

    @pytest.mark.asyncio
    async def test_includes_customer_name_when_payer_name_set(self, monkeypatch):
        captured, transport = _make_transport(httpx.Response(200, json={"trx_id": "T"}))
        _patch_settings(monkeypatch, oy_api_key="k", oy_default_username="u")
        _install_request_shim(monkeypatch, transport)
        await oy_client.create_invoice(
            external_id="x", amount_idr=1000, description="d",
            payer_name="Buyer",
        )
        body = _body_of(captured[0])
        assert body["customer_name"] == "Buyer"

    @pytest.mark.asyncio
    async def test_omits_optional_fields_when_blank(self, monkeypatch):
        captured, transport = _make_transport(httpx.Response(200, json={"trx_id": "T"}))
        _patch_settings(monkeypatch, oy_api_key="k", oy_default_username="u")
        _install_request_shim(monkeypatch, transport)
        await oy_client.create_invoice(
            external_id="x", amount_idr=1000, description="d",
            payer_email=None, payer_name=None,
            callback_url=None, success_redirect_url=None, failure_redirect_url=None,
        )
        body = _body_of(captured[0])
        assert "payer_email" not in body
        assert "customer_name" not in body
        assert "callback_url" not in body
        assert "success_redirect_url" not in body
        assert "failure_redirect_url" not in body

    @pytest.mark.asyncio
    async def test_amount_is_coerced_to_int(self, monkeypatch):
        """amount_idr is declared int but callers might pass a numeric string."""
        captured, transport = _make_transport(httpx.Response(200, json={"trx_id": "T"}))
        _patch_settings(monkeypatch, oy_api_key="k", oy_default_username="u")
        _install_request_shim(monkeypatch, transport)
        await oy_client.create_invoice(external_id="x", amount_idr="5000", description="d")  # type: ignore[arg-type]
        body = _body_of(captured[0])
        assert body["amount"] == 5000
        assert isinstance(body["amount"], int)

    @pytest.mark.asyncio
    async def test_includes_all_redirects_when_set(self, monkeypatch):
        captured, transport = _make_transport(httpx.Response(200, json={"trx_id": "T"}))
        _patch_settings(monkeypatch, oy_api_key="k", oy_default_username="u")
        _install_request_shim(monkeypatch, transport)
        await oy_client.create_invoice(
            external_id="x", amount_idr=1000, description="d",
            callback_url="https://cb",
            success_redirect_url="https://ok",
            failure_redirect_url="https://fail",
        )
        body = _body_of(captured[0])
        assert body["callback_url"] == "https://cb"
        assert body["success_redirect_url"] == "https://ok"
        assert body["failure_redirect_url"] == "https://fail"


class TestNotImplemented:
    @pytest.mark.asyncio
    async def test_get_invoice_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            await oy_client.get_invoice(invoice_id="x")

    @pytest.mark.asyncio
    async def test_create_refund_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            await oy_client.create_refund(
                external_id="x", invoice_id="y", amount_idr=1000,
            )


# ---- helpers ---------------------------------------------------------------


def _body_of(request: httpx.Request) -> dict:
    """Decode the JSON body from a MockTransport-captured httpx.Request."""
    raw = request.content
    if isinstance(raw, bytes):
        return json.loads(raw.decode("utf-8"))
    return raw


def _install_request_shim(monkeypatch, transport: httpx.MockTransport) -> None:
    """Swap ``oy_client._request`` with a transport-backed shim that
    mirrors the production logic exactly (so that test-time errors on
    the no-keys branch aren't masked by a shim that does its own check).
    """
    async def fake_request(method, path, *, api_key=None, username=None, json=None):
        effective = api_key or oy_client.settings.oy_api_key
        if not effective:
            raise oy_client.OYError(0, "OY_API_KEY not configured (env or Brand.oy_api_key)")
        async with httpx.AsyncClient(transport=transport, timeout=30.0) as client:
            resp = await client.request(
                method,
                f"{oy_client._BASE_URL}{path}",
                headers=oy_client._headers(
                    api_key=api_key or oy_client.settings.oy_api_key,
                    username=username or oy_client.settings.oy_default_username,
                ),
                json=json,
            )
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = resp.text
            raise oy_client.OYError(resp.status_code, body)
        return resp.json()

    monkeypatch.setattr(oy_client, "_request", fake_request)
