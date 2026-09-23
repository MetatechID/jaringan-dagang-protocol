"""Dipay SNAP v2.1 client — pure unit tests via httpx.MockTransport.

Exercises ``services/dipay_client.py`` end-to-end (RSA access-token
signing, HMAC-SHA512 request signing, header injection, payload shape,
DipayError mapping, token caching + 401 retry) without hitting the
network. Every test installs a ``MockTransport``-backed ``httpx.AsyncClient``
subclass — the client builds a fresh AsyncClient per call (codebase
convention), so we monkeypatch ``dipay_client.httpx.AsyncClient`` itself.

Reference: https://api-docs.dipay.id/
- Auth:    POST /access-token/b2b (X-CLIENT-KEY + SHA256withRSA X-SIGNATURE)
- QRIS:    POST /qr/qr-mpm-generate, /qr/qr-mpm-query (serviceCode 47)
- Payout:  POST /emoney/transfer-bank, /transfer/status (serviceCode 43)
- partnerReferenceNo ≤ 32 chars (snap_ref enforces the cap)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import uuid
from types import SimpleNamespace

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

import httpx
import pytest

from services import dipay_client  # noqa: E402


def _patch_settings(monkeypatch, **overrides) -> SimpleNamespace:
    fake = SimpleNamespace(
        dipay_client_key=overrides.get("dipay_client_key", "client-key"),
        dipay_client_secret=overrides.get("dipay_client_secret", "client-secret"),
        dipay_private_key_b64=overrides.get("dipay_private_key_b64", ""),
        dipay_private_key_path=overrides.get("dipay_private_key_path", ""),
        dipay_base_url=overrides.get(
            "dipay_base_url", "https://api-b2x-demo.dipay.id/snap/v2.1"
        ),
        dipay_merchant_id=overrides.get("dipay_merchant_id", ""),
        dipay_callback_public_key=overrides.get("dipay_callback_public_key", ""),
        dipay_qris_duration_seconds=overrides.get("dipay_qris_duration_seconds", 1800),
    )
    monkeypatch.setattr(dipay_client, "settings", fake)
    return fake


def _make_transport(queue: dict[str, list]):
    """MockTransport dispatching on URL path. ``queue`` maps path → list of
    canned httpx.Response (popped in order); unmatched paths fall back to a
    per-path default (access-token → tok-1, everything else → 200 OK)."""
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        path = request.url.path
        responses = next(
            (v for k, v in queue.items() if path.endswith(k)), None
        )
        if responses:
            nxt = responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        if path.endswith("/access-token/b2b"):
            return httpx.Response(
                200, json={"accessToken": "tok-1", "expiresIn": "900"}
            )
        return httpx.Response(
            200, json={"responseCode": "2000000", "responseMessage": "Success"}
        )

    return captured, httpx.MockTransport(handler)


def _install_transport(monkeypatch, transport: httpx.MockTransport) -> None:
    class TransportClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("transport", transport)
            kwargs.setdefault("timeout", 30.0)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(dipay_client.httpx, "AsyncClient", TransportClient)


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    """The token cache is module-level state — reset around every test."""
    dipay_client._reset_token_cache()
    yield
    dipay_client._reset_token_cache()


def _body_of(request: httpx.Request) -> dict:
    raw = request.content
    if isinstance(raw, bytes):
        return json.loads(raw.decode("utf-8"))
    return raw


def _config(**overrides) -> dipay_client.DipayConfig:
    current = dipay_client.settings
    return dipay_client.DipayConfig(
        base_url=overrides.get("base_url", current.dipay_base_url).rstrip("/"),
        client_key=overrides.get("client_key", current.dipay_client_key),
        client_secret=overrides.get("client_secret", current.dipay_client_secret),
        private_key_b64=overrides.get(
            "private_key_b64", current.dipay_private_key_b64
        ),
        private_key_path=overrides.get(
            "private_key_path", current.dipay_private_key_path
        ),
        merchant_id=overrides.get("merchant_id", current.dipay_merchant_id),
    )


def _seed_token(token: str, expiry: float) -> None:
    dipay_client._token_cache[_config().token_identity] = (token, expiry)


def _requests_to(captured: list, path: str) -> list:
    # Match on path SUFFIX — the base URL carries a /snap/v2.1 prefix, so
    # httpx's url.path is "/snap/v2.1/access-token/b2b", not "/access-token/b2b".
    return [r for r in captured if r.url.path.endswith(path)]


def _recompute_request_signature(req: httpx.Request, client_secret: str) -> str:
    """Rebuild stringToSign per the Dipay spec and HMAC it — the request's
    X-SIGNATURE must match exactly."""
    token = req.headers["Authorization"].removeprefix("Bearer ")
    body_str = req.content.decode("utf-8") if req.content else "{}"
    string_to_sign = (
        f"{req.method}:{str(req.url)}:{token}:"
        f"{hashlib.sha256(body_str.encode('utf-8')).hexdigest()}:"
        f"{req.headers['X-TIMESTAMP']}"
    )
    digest = hmac.new(
        client_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha512,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _generate_rsa_key_b64() -> tuple[str, object]:
    """Throwaway RSA keypair → (base64 PEM, public key)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(pem).decode(), key.public_key()


# ---- Test classes ----------------------------------------------------------


class TestSnapRef:
    def test_strips_dashes_from_raw_id(self):
        assert dipay_client.snap_ref("q", "ab-cd-ef") == "q-abcdef"

    def test_caps_at_32_chars(self):
        raw = str(uuid.uuid4()) * 4  # far longer than any real id
        for prefix in ("q", "r"):
            ref = dipay_client.snap_ref(prefix, raw)
            assert len(ref) <= 32
            assert ref.startswith(f"{prefix}-")

    def test_truncates_body_to_30_chars(self):
        raw = "a" * 100
        ref = dipay_client.snap_ref("q", raw)
        assert ref == "q-" + "a" * 30
        assert len(ref) == 32

    def test_short_id_passes_through(self):
        assert dipay_client.snap_ref("r", "order-xyz") == "r-orderxyz"


class TestNoCreds:
    @pytest.mark.asyncio
    async def test_raises_dipay_error_zero_when_no_client_key(self, monkeypatch):
        """No client key → fail before any network call."""
        _patch_settings(monkeypatch, dipay_client_key="")
        captured, transport = _make_transport({})
        _install_transport(monkeypatch, transport)

        with pytest.raises(dipay_client.DipayError) as ei:
            await dipay_client.create_qris(
                config=dipay_client.resolve_config(), partner_reference_no="q-x", amount_idr=1000
            )
        assert ei.value.status_code == 0
        assert "Incomplete environment Dipay credentials (missing: dipay_client_key" in str(ei.value.body)
        assert captured == []

    @pytest.mark.asyncio
    async def test_raises_dipay_error_zero_when_no_private_key(self, monkeypatch):
        """Client key set but no RSA key material → token fetch fails fast."""
        _patch_settings(monkeypatch, dipay_private_key_b64="", dipay_private_key_path="")
        captured, transport = _make_transport({})
        _install_transport(monkeypatch, transport)

        with pytest.raises(dipay_client.DipayError) as ei:
            await dipay_client.create_qris(config=_config(), partner_reference_no="q-x", amount_idr=1000)
        assert ei.value.status_code == 0
        assert "private key" in str(ei.value.body).lower()


class TestAccessToken:
    @pytest.mark.asyncio
    async def test_rsa_signature_verifies_against_keypair(self, monkeypatch):
        """X-SIGNATURE on /access-token/b2b must be SHA256withRSA over
        '{client_key}|{x_timestamp}' — verify with the matching public key."""
        key_b64, public_key = _generate_rsa_key_b64()
        _patch_settings(monkeypatch, dipay_private_key_b64=key_b64)
        captured, transport = _make_transport({})
        _install_transport(monkeypatch, transport)

        await dipay_client._get_token(_config(), force=True)

        req = _requests_to(captured, "/access-token/b2b")[0]
        assert req.headers["X-CLIENT-KEY"] == "client-key"
        assert req.headers["Content-Type"] == "application/json"
        signed_message = f"client-key|{req.headers['X-TIMESTAMP']}".encode("utf-8")
        # public_key.verify raises InvalidSignature on mismatch.
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        public_key.verify(
            base64.b64decode(req.headers["X-SIGNATURE"]),
            signed_message,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        body = _body_of(req)
        assert body == {"grantType": "client_credentials"}

    @pytest.mark.asyncio
    async def test_token_cached_across_calls(self, monkeypatch):
        """Second signed call must reuse the cached token — exactly one
        access-token request for two QRIS calls."""
        _patch_settings(monkeypatch, dipay_private_key_b64="x")  # key never loaded
        captured, transport = _make_transport({})
        _install_transport(monkeypatch, transport)

        # Bypass the RSA fetch by pre-seeding the cache, as the real fetch
        # would leave it.
        _seed_token("tok-1", 9_999_999_999.0)

        await dipay_client.create_qris(config=_config(), partner_reference_no="q-a", amount_idr=1000)
        await dipay_client.create_qris(config=_config(), partner_reference_no="q-b", amount_idr=1000)

        assert len(_requests_to(captured, "/access-token/b2b")) == 0
        assert len(_requests_to(captured, "/qr/qr-mpm-generate")) == 2

    @pytest.mark.asyncio
    async def test_expiring_token_triggers_refresh(self, monkeypatch):
        """A token past its refresh margin is re-fetched before use."""
        key_b64, _ = _generate_rsa_key_b64()
        _patch_settings(monkeypatch, dipay_private_key_b64=key_b64)
        captured, transport = _make_transport({})
        _install_transport(monkeypatch, transport)

        _seed_token("tok-stale", 0.0)  # already "expired"

        await dipay_client.create_qris(config=_config(), partner_reference_no="q-a", amount_idr=1000)
        token_reqs = _requests_to(captured, "/access-token/b2b")
        assert len(token_reqs) == 1
        # The signed call must carry the *fresh* token, not the stale one.
        qr_req = _requests_to(captured, "/qr/qr-mpm-generate")[0]
        assert qr_req.headers["Authorization"] == "Bearer tok-1"


class TestSignedRequestHeaders:
    @pytest.mark.asyncio
    async def test_required_headers_present(self, monkeypatch):
        _patch_settings(monkeypatch, dipay_private_key_b64="x")
        captured, transport = _make_transport({})
        _install_transport(monkeypatch, transport)
        _seed_token("tok-1", 9_999_999_999.0)

        await dipay_client.create_qris(config=_config(), partner_reference_no="q-a", amount_idr=1000)

        req = _requests_to(captured, "/qr/qr-mpm-generate")[0]
        assert req.headers["Authorization"] == "Bearer tok-1"
        assert req.headers["X-PARTNER-ID"] == "client-key"
        assert req.headers["CHANNEL-ID"] == "DPAPI"
        assert req.headers["Content-Type"] == "application/json"
        # X-EXTERNAL-ID: 32 lowercase hex chars (uuid4().hex).
        external_id = req.headers["X-EXTERNAL-ID"]
        assert len(external_id) == 32
        assert all(c in "0123456789abcdef" for c in external_id)
        # X-TIMESTAMP: ISO-8601 of length 25 (seconds precision), +07:00 offset (Jakarta).
        ts = req.headers["X-TIMESTAMP"]
        assert ts.endswith("+07:00")
        assert len(ts) == 25
        assert "." not in ts

    @pytest.mark.asyncio
    async def test_hmac_sha512_string_to_sign_recomputation(self, monkeypatch):
        """X-SIGNATURE must equal HMAC-SHA512(clientSecret,
        '{METHOD}:{full_url}:{accessToken}:{sha256(minified body)}:{X-TIMESTAMP}')."""
        _patch_settings(monkeypatch, dipay_private_key_b64="x")
        captured, transport = _make_transport({})
        _install_transport(monkeypatch, transport)
        _seed_token("tok-1", 9_999_999_999.0)

        await dipay_client.create_qris(config=_config(), partner_reference_no="q-a", amount_idr=12345)

        req = _requests_to(captured, "/qr/qr-mpm-generate")[0]
        assert req.headers["X-SIGNATURE"] == _recompute_request_signature(req, "client-secret")

    @pytest.mark.asyncio
    async def test_body_sent_is_the_minified_hashed_body(self, monkeypatch):
        """The bytes on the wire must hash to the same digest inside
        stringToSign (minified, separators=(',',':'))."""
        _patch_settings(monkeypatch, dipay_private_key_b64="x")
        captured, transport = _make_transport({})
        _install_transport(monkeypatch, transport)
        _seed_token("tok-1", 9_999_999_999.0)

        await dipay_client.create_qris(config=_config(), partner_reference_no="q-a", amount_idr=1000)

        req = _requests_to(captured, "/qr/qr-mpm-generate")[0]
        raw = req.content.decode("utf-8")
        # Minified: no spaces after separators.
        assert ": " not in raw and ", " not in raw
        parsed = json.loads(raw)
        minified = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
        assert raw == minified
        assert req.headers["X-SIGNATURE"] == _recompute_request_signature(req, "client-secret")

    @pytest.mark.asyncio
    async def test_empty_body_hashes_empty_json_object(self, monkeypatch):
        """A signed request with no body (json=None) hashes the literal '{}' and
        the wire body must match."""
        _patch_settings(monkeypatch, dipay_private_key_b64="x")
        captured, transport = _make_transport({})
        _install_transport(monkeypatch, transport)
        _seed_token("tok-1", 9_999_999_999.0)

        await dipay_client._signed_request(
            "POST", "/test-empty", config=_config(), access_token="tok-1", json=None
        )

        req = _requests_to(captured, "/test-empty")[0]
        assert req.content.decode("utf-8") == "{}"
        assert req.headers["X-SIGNATURE"] == _recompute_request_signature(req, "client-secret")


class TestRequestErrors:
    @pytest.mark.asyncio
    async def test_raises_dipay_error_on_4xx_response(self, monkeypatch):
        _patch_settings(monkeypatch, dipay_private_key_b64="x")
        captured, transport = _make_transport(
            {"/qr/qr-mpm-generate": [
                httpx.Response(400, json={"responseCode": "4004700", "responseMessage": "Bad Request"})
            ]}
        )
        _install_transport(monkeypatch, transport)
        _seed_token("tok-1", 9_999_999_999.0)

        with pytest.raises(dipay_client.DipayError) as ei:
            await dipay_client.create_qris(config=_config(), partner_reference_no="q-x", amount_idr=1000)
        assert ei.value.status_code == 400
        assert ei.value.body == {"responseCode": "4004700", "responseMessage": "Bad Request"}
        # No retry on non-401: exactly one business call.
        assert len(_requests_to(captured, "/qr/qr-mpm-generate")) == 1

    @pytest.mark.asyncio
    async def test_raises_dipay_error_on_5xx_text_response(self, monkeypatch):
        _patch_settings(monkeypatch, dipay_private_key_b64="x")
        captured, transport = _make_transport(
            {"/qr/qr-mpm-generate": [httpx.Response(503, text="dipay service down")]}
        )
        _install_transport(monkeypatch, transport)
        _seed_token("tok-1", 9_999_999_999.0)

        with pytest.raises(dipay_client.DipayError) as ei:
            await dipay_client.create_qris(config=_config(), partner_reference_no="q-x", amount_idr=1000)
        assert ei.value.status_code == 503
        assert ei.value.body == "dipay service down"

    @pytest.mark.asyncio
    async def test_401_retries_once_with_forced_token_refresh(self, monkeypatch):
        """First business call 401s → force-refresh the token → retry ONCE."""
        key_b64, _ = _generate_rsa_key_b64()
        _patch_settings(monkeypatch, dipay_private_key_b64=key_b64)
        captured, transport = _make_transport(
            {"/qr/qr-mpm-generate": [
                httpx.Response(401, json={"responseCode": "4014300", "responseMessage": "Unauthorized"}),
                httpx.Response(200, json={"responseCode": "2004700", "responseMessage": "Success"}),
            ]}
        )
        _install_transport(monkeypatch, transport)
        _seed_token("tok-stale", 9_999_999_999.0)  # cache says valid → 401 is what triggers refresh

        out = await dipay_client.create_qris(config=_config(), partner_reference_no="q-x", amount_idr=1000)

        assert out["responseCode"] == "2004700"
        # 1 initial token refresh + 2 business calls.
        assert len(_requests_to(captured, "/access-token/b2b")) == 1
        assert len(_requests_to(captured, "/qr/qr-mpm-generate")) == 2
        # Retry carries the new token.
        assert _requests_to(captured, "/qr/qr-mpm-generate")[1].headers["Authorization"] == "Bearer tok-1"


class TestCreateQrisPayload:
    @pytest.mark.asyncio
    async def test_required_fields_and_amount_formatting(self, monkeypatch):
        _patch_settings(monkeypatch, dipay_private_key_b64="x")
        captured, transport = _make_transport({})
        _install_transport(monkeypatch, transport)
        _seed_token("tok-1", 9_999_999_999.0)

        await dipay_client.create_qris(config=_config(merchant_id="M-77"),
            partner_reference_no="q-abc", amount_idr=50000
        )

        req = _requests_to(captured, "/qr/qr-mpm-generate")[0]
        assert req.method == "POST"
        body = _body_of(req)
        assert body["partnerReferenceNo"] == "q-abc"
        assert body["amount"] == {"value": "50000.00", "currency": "IDR"}
        assert body["merchantId"] == "M-77"
        assert "validityPeriod" not in body

    @pytest.mark.asyncio
    async def test_merchant_id_falls_back_to_settings(self, monkeypatch):
        _patch_settings(monkeypatch, dipay_private_key_b64="x", dipay_merchant_id="ENV-MERCH")
        captured, transport = _make_transport({})
        _install_transport(monkeypatch, transport)
        _seed_token("tok-1", 9_999_999_999.0)

        await dipay_client.create_qris(config=_config(), partner_reference_no="q-abc", amount_idr=1000)

        body = _body_of(_requests_to(captured, "/qr/qr-mpm-generate")[0])
        assert body["merchantId"] == "ENV-MERCH"

    @pytest.mark.asyncio
    async def test_optional_validity_period_included_when_set(self, monkeypatch):
        _patch_settings(monkeypatch, dipay_private_key_b64="x")
        captured, transport = _make_transport({})
        _install_transport(monkeypatch, transport)
        _seed_token("tok-1", 9_999_999_999.0)

        await dipay_client.create_qris(config=_config(),
            partner_reference_no="q-abc", amount_idr=1000,
            validity_period="2026-09-21T23:59:59+07:00",
        )

        body = _body_of(_requests_to(captured, "/qr/qr-mpm-generate")[0])
        assert body["validityPeriod"] == "2026-09-21T23:59:59+07:00"


class TestQueryQris:
    @pytest.mark.asyncio
    async def test_query_endpoint_with_service_code_47(self, monkeypatch):
        _patch_settings(monkeypatch, dipay_private_key_b64="x")
        captured, transport = _make_transport({})
        _install_transport(monkeypatch, transport)
        _seed_token("tok-1", 9_999_999_999.0)

        out = await dipay_client.query_qris(config=_config(), partner_reference_no="q-abc")

        req = _requests_to(captured, "/qr/qr-mpm-query")[0]
        body = _body_of(req)
        assert body == {"originalPartnerReferenceNo": "q-abc", "serviceCode": "47"}
        assert out["responseCode"] == "2000000"


class TestDisbursementPayload:
    @pytest.mark.asyncio
    async def test_transfer_bank_required_fields(self, monkeypatch):
        _patch_settings(monkeypatch, dipay_private_key_b64="x")
        captured, transport = _make_transport({})
        _install_transport(monkeypatch, transport)
        _seed_token("tok-1", 9_999_999_999.0)

        await dipay_client.create_disbursement(config=_config(),
            partner_reference_no="r-abc",
            beneficiary_account="1234567890",
            beneficiary_bank_code="014",
            amount_idr=73500,
        )

        req = _requests_to(captured, "/emoney/transfer-bank")[0]
        body = _body_of(req)
        assert body["partnerReferenceNo"] == "r-abc"
        assert body["customerNumber"] == "client-key"
        assert body["amount"] == {"value": "73500.00", "currency": "IDR"}
        assert body["beneficiaryAccountNumber"] == "1234567890"
        assert body["beneficiaryBankCode"] == "014"
        assert "additionalInfo" not in body  # all-optional fields omitted

    @pytest.mark.asyncio
    async def test_transfer_bank_optional_additional_info(self, monkeypatch):
        _patch_settings(monkeypatch, dipay_private_key_b64="x")
        captured, transport = _make_transport({})
        _install_transport(monkeypatch, transport)
        _seed_token("tok-1", 9_999_999_999.0)

        await dipay_client.create_disbursement(config=_config(),
            partner_reference_no="r-abc",
            beneficiary_account="1234567890",
            beneficiary_bank_code="014",
            amount_idr=1000,
            customer_reference="BeliAman release order 1",
            partner_merchant_id="safiya",
            beneficiary_email="seller@example.com",
        )

        body = _body_of(_requests_to(captured, "/emoney/transfer-bank")[0])
        assert body["additionalInfo"] == {
            "customerReference": "BeliAman release order 1",
            "partnerMerchantId": "safiya",
            "beneficiaryEmail": "seller@example.com",
        }

    @pytest.mark.asyncio
    async def test_bank_account_inquiry_shape(self, monkeypatch):
        _patch_settings(monkeypatch, dipay_private_key_b64="x")
        captured, transport = _make_transport({})
        _install_transport(monkeypatch, transport)
        _seed_token("tok-1", 9_999_999_999.0)

        await dipay_client.inquiry_bank_account(config=_config(),
            partner_reference_no="i-abc",
            beneficiary_account="1234567890",
            beneficiary_bank_code="014",
            amount_idr=1000,
        )

        body = _body_of(_requests_to(captured, "/emoney/bank-account-inquiry")[0])
        assert body == {
            "partnerReferenceNo": "i-abc",
            "customerNumber": "client-key",
            "amount": {"value": "1000.00", "currency": "IDR"},
            "beneficiaryAccountNumber": "1234567890",
            "additionalInfo": {"beneficiaryBankCode": "014"},
        }

    @pytest.mark.asyncio
    async def test_status_endpoint_with_service_code_43(self, monkeypatch):
        _patch_settings(monkeypatch, dipay_private_key_b64="x")
        captured, transport = _make_transport({})
        _install_transport(monkeypatch, transport)
        _seed_token("tok-1", 9_999_999_999.0)

        await dipay_client.get_disbursement_status(config=_config(), partner_reference_no="r-abc")

        body = _body_of(_requests_to(captured, "/transfer/status")[0])
        assert body == {
            "originalPartnerReferenceNo": "r-abc",
            "serviceCode": "43",
        }

    @pytest.mark.asyncio
    async def test_status_endpoint_send_callback_flag(self, monkeypatch):
        _patch_settings(monkeypatch, dipay_private_key_b64="x")
        captured, transport = _make_transport({})
        _install_transport(monkeypatch, transport)
        _seed_token("tok-1", 9_999_999_999.0)

        await dipay_client.get_disbursement_status(config=_config(),
            partner_reference_no="r-abc", send_callback=True
        )

        body = _body_of(_requests_to(captured, "/transfer/status")[0])
        assert body["additionalInfo"] == {"sendCallback": True}


class TestBalanceInquiry:
    @pytest.mark.asyncio
    async def test_get_balance_payload_and_partner_ref(self, monkeypatch):
        """get_balance sends partnerReferenceNo and balanceTypes."""
        _patch_settings(monkeypatch, dipay_private_key_b64="x")
        captured, transport = _make_transport({})
        _install_transport(monkeypatch, transport)
        _seed_token("tok-1", 9_999_999_999.0)

        await dipay_client.get_balance(config=_config(), partner_reference_no="b-123")

        req = _requests_to(captured, "/balance-inquiry")[0]
        body = _body_of(req)
        assert body == {
            "partnerReferenceNo": "b-123",
            "balanceTypes": ["deposit"],
        }
        assert req.headers["X-SIGNATURE"] == _recompute_request_signature(req, "client-secret")

    @pytest.mark.asyncio
    async def test_get_balance_generates_partner_ref_when_omitted(self, monkeypatch):
        """get_balance generates a compliant partnerReferenceNo when omitted."""
        _patch_settings(monkeypatch, dipay_private_key_b64="x")
        captured, transport = _make_transport({})
        _install_transport(monkeypatch, transport)
        _seed_token("tok-1", 9_999_999_999.0)

        await dipay_client.get_balance(config=_config())

        req = _requests_to(captured, "/balance-inquiry")[0]
        body = _body_of(req)
        assert body["balanceTypes"] == ["deposit"]
        assert body["partnerReferenceNo"].startswith("b-")
        assert len(body["partnerReferenceNo"]) <= 32
        assert req.headers["X-SIGNATURE"] == _recompute_request_signature(req, "client-secret")


class TestVerifyNotificationSignature:
    def test_verify_notification_signature_b64_success(self):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub_pem = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")

        body = {"originalPartnerReferenceNo": "q-123", "latestTransactionStatus": "00"}
        body_hash = hashlib.sha256(json.dumps(body, separators=(",", ":")).encode("utf-8")).hexdigest()
        method = "POST"
        path = "/webhooks/dipay/qris"
        ts = "2026-09-23T14:30:00+07:00"
        string_to_sign = f"{method}:{path}:{body_hash}:{ts}"
        raw_sig = key.sign(string_to_sign.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        sig_b64 = base64.b64encode(raw_sig).decode("utf-8")

        assert dipay_client.verify_notification_signature(
            method=method,
            path=path,
            body=body,
            timestamp=ts,
            signature=sig_b64,
            public_key=pub_pem,
        ) is True

    def test_verify_notification_signature_hex_success(self):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub_pem = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")

        body = {"originalPartnerReferenceNo": "q-123", "latestTransactionStatus": "00"}
        body_hash = hashlib.sha256(json.dumps(body, separators=(",", ":")).encode("utf-8")).hexdigest()
        method = "POST"
        path = "/webhooks/dipay/qris"
        ts = "2026-09-23T14:30:00+07:00"
        string_to_sign = f"{method}:{path}:{body_hash}:{ts}"
        raw_sig = key.sign(string_to_sign.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        sig_hex = raw_sig.hex()

        assert dipay_client.verify_notification_signature(
            method=method,
            path=path,
            body=body,
            timestamp=ts,
            signature=sig_hex,
            public_key=pub_pem,
        ) is True

    def test_verify_notification_signature_uses_settings_key(self, monkeypatch):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub_pem = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")

        _patch_settings(monkeypatch, dipay_callback_public_key=pub_pem)

        body = {"originalPartnerReferenceNo": "q-123", "latestTransactionStatus": "00"}
        body_hash = hashlib.sha256(json.dumps(body, separators=(",", ":")).encode("utf-8")).hexdigest()
        method = "POST"
        path = "/webhooks/dipay/qris"
        ts = "2026-09-23T14:30:00+07:00"
        string_to_sign = f"{method}:{path}:{body_hash}:{ts}"
        raw_sig = key.sign(string_to_sign.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        sig_b64 = base64.b64encode(raw_sig).decode("utf-8")

        assert dipay_client.verify_notification_signature(
            method=method,
            path=path,
            body=body,
            timestamp=ts,
            signature=sig_b64,
        ) is True

    def test_verify_notification_signature_tampered_fails(self):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub_pem = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")

        body = {"originalPartnerReferenceNo": "q-123", "latestTransactionStatus": "00"}
        body_hash = hashlib.sha256(json.dumps(body, separators=(",", ":")).encode("utf-8")).hexdigest()
        method = "POST"
        path = "/webhooks/dipay/qris"
        ts = "2026-09-23T14:30:00+07:00"
        string_to_sign = f"{method}:{path}:{body_hash}:{ts}"
        raw_sig = key.sign(string_to_sign.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        sig_b64 = base64.b64encode(raw_sig).decode("utf-8")

        # Tampered body
        assert dipay_client.verify_notification_signature(
            method=method,
            path=path,
            body={"originalPartnerReferenceNo": "q-tampered"},
            timestamp=ts,
            signature=sig_b64,
            public_key=pub_pem,
        ) is False

        # Tampered timestamp
        assert dipay_client.verify_notification_signature(
            method=method,
            path=path,
            body=body,
            timestamp="2026-09-23T15:00:00+07:00",
            signature=sig_b64,
            public_key=pub_pem,
        ) is False

        # Empty signature
        assert dipay_client.verify_notification_signature(
            method=method,
            path=path,
            body=body,
            timestamp=ts,
            signature="",
            public_key=pub_pem,
        ) is False

    def test_verify_notification_signature_missing_key(self, monkeypatch):
        _patch_settings(monkeypatch, dipay_callback_public_key="")
        assert dipay_client.verify_notification_signature(
            method="POST",
            path="/webhooks/dipay/qris",
            body={},
            timestamp="2026-09-23T14:30:00+07:00",
            signature="invalid",
            public_key=None,
        ) is False


class TestLoadPrivateKey:
    def test_load_private_key_raw_pem_and_b64(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem_bytes = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pem_str = pem_bytes.decode("utf-8")
        b64_str = base64.b64encode(pem_bytes).decode("utf-8")

        cfg_pem = _config(private_key_b64=pem_str)
        loaded_pem = dipay_client._load_private_key(cfg_pem)
        assert loaded_pem is not None

        cfg_b64 = _config(private_key_b64=b64_str)
        loaded_b64 = dipay_client._load_private_key(cfg_b64)
        assert loaded_b64 is not None

    def test_load_private_key_invalid_returns_none(self):
        cfg_invalid = _config(private_key_b64="not-a-valid-key")
        assert dipay_client._load_private_key(cfg_invalid) is None

