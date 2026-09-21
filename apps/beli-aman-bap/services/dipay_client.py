"""Async Dipay payment gateway HTTP client (SNAP v2.1).

Dipay's B2B API follows the Indonesian SNAP (Standard Payment API)
v2.1 conventions: a B2B access token obtained with an RSA-signed request,
then per-call HMAC-SHA512 request signatures over a ``stringToSign``.

Auth flow (https://api-docs.dipay.id/ — Authentication sections):
- ``POST {base}/access-token/b2b`` with ``X-CLIENT-KEY`` + ``X-SIGNATURE``
  where the signature is **SHA256withRSA** (PKCS1v15 + SHA256) over
  ``"{client_key}|{x_timestamp}"`` using the merchant's RSA private key.
  Response: ``{accessToken, expiresIn}`` (900s in the demo env).
- Business endpoints then carry ``Authorization: Bearer {accessToken}``
  plus ``X-SIGNATURE`` = hex **HMAC-SHA512** keyed with the client secret
  over ``"{METHOD}:{full_url}:{accessToken}:{sha256(body)}:{x_timestamp}"``
  (body hash = lowercase hex SHA-256 of the *minified* JSON body — ``{}``
  when there is no body).

``partnerReferenceNo`` is our idempotency / correlation key and is capped
at **32 characters** by the SNAP spec — use :func:`snap_ref` to build
compliant values (``q-...`` for QRIS invoices, ``r-...`` for release
disbursements).

QRIS:    ``POST /qr/qr-mpm-generate`` / ``POST /qr/qr-mpm-query``
Transfer: ``POST /emoney/transfer-bank`` + inquiry + status + balance.

See https://api-docs.dipay.id/ (Authentication / QRIS MPM / Disbursement
pages). Token responses and signatures are cached module-level so the
async client stays stateless per call (fresh ``httpx.AsyncClient`` per
request, matching the Sento/OY client convention).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config import settings
from services.release_clock import JAKARTA

_LOG = logging.getLogger("beli_aman_bap.dipay")

# Refresh the cached access token this many seconds before it actually
# expires — avoids signing a request with a token that dies mid-flight.
_TOKEN_REFRESH_MARGIN_SECONDS = 120
# Default TTL when Dipay omits ``expiresIn`` (demo env sends "900").
_DEFAULT_TOKEN_TTL_SECONDS = 900


class DipayError(Exception):
    """Raised when Dipay returns a non-2xx response (or creds are missing)."""

    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Dipay {status_code}: {body!r}")


def snap_ref(prefix: str, raw_id: str) -> str:
    """Build a SNAP-compliant ``partnerReferenceNo`` (≤ 32 chars).

    ``snap_ref("q", "0f0e8a2e-…")`` → ``"q-0f0e8a2e…"``. Dashes are
    stripped from the raw id (they burn characters without adding
    uniqueness for our UUIDs) and the remainder is truncated to 30 so
    ``prefix + "-" + body`` never exceeds the 32-char cap.
    """
    return f"{prefix}-{raw_id.replace('-', '')[:30]}"


# ---------------------------------------------------------------------------
# Signing helpers
# ---------------------------------------------------------------------------


def _load_private_key():
    """Load the RSA private key for SHA256withRSA token signing.

    ``dipay_private_key_b64`` (base64-encoded PEM) wins over
    ``dipay_private_key_path`` when both are configured.
    """
    pem_b64 = (settings.dipay_private_key_b64 or "").strip()
    if pem_b64:
        pem = base64.b64decode(pem_b64)
        return serialization.load_pem_private_key(pem, password=None)
    path = (settings.dipay_private_key_path or "").strip()
    if path:
        with open(path, "rb") as fh:
            return serialization.load_pem_private_key(fh.read(), password=None)
    return None


def _rsa_sha256_hex(message: str, private_key) -> str:
    """SHA256withRSA — PKCS1v15 + SHA256, lowercase-hex encoded."""
    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return signature.hex()


def _minify(body: dict | None) -> str:
    """Canonical JSON used for the body-hash leg of ``stringToSign``.

    Must match the bytes actually sent, so ``_signed_request`` posts this
    exact string as ``content=`` (httpx's own serializer would differ).
    No body (e.g. balance inquiry) hashes the literal ``"{}"``.
    """
    if not body:
        return "{}"
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _x_timestamp() -> str:
    # SNAP timestamps are ISO-8601; Jakarta is Dipay's operating tz.
    return datetime.now(JAKARTA).isoformat(timespec="milliseconds")


def _string_to_sign(
    *, method: str, full_url: str, access_token: str, body_str: str, x_timestamp: str
) -> str:
    return f"{method}:{full_url}:{access_token}:{_sha256_hex(body_str)}:{x_timestamp}"


def _hmac_sha512_hex(string_to_sign: str, client_secret: str) -> str:
    return hmac.new(
        client_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha512,
    ).hexdigest()


def _base_url() -> str:
    # ponytail: env-driven so demo/prod switch is a .env flip, no code
    # edit. Default demo
    # (https://api-b2x-demo.dipay.id/snap/v2.1); prod overrides DIPAY_BASE_URL.
    return settings.dipay_base_url.rstrip("/")


# ---------------------------------------------------------------------------
# Access token (cached module-level)
# ---------------------------------------------------------------------------

_token_value: str | None = None
_token_expiry: float = 0.0  # epoch seconds after which the token is stale
_token_lock = asyncio.Lock()


def _reset_token_cache() -> None:
    """Test hook / error-path helper: drop the cached token."""
    global _token_value, _token_expiry
    _token_value = None
    _token_expiry = 0.0


async def _fetch_token() -> tuple[str, int]:
    """POST /access-token/b2b → (accessToken, expiresIn_seconds)."""
    url = f"{_base_url()}/access-token/b2b"
    private_key = _load_private_key()
    if private_key is None:
        raise DipayError(
            0,
            "Dipay private key not configured "
            "(DIPAY_PRIVATE_KEY_B64 or DIPAY_PRIVATE_KEY_PATH)",
        )
    x_timestamp = _x_timestamp()
    signature = _rsa_sha256_hex(f"{settings.dipay_client_key}|{x_timestamp}", private_key)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            headers={
                "Content-Type": "application/json",
                "X-TIMESTAMP": x_timestamp,
                "X-CLIENT-KEY": settings.dipay_client_key,
                "X-SIGNATURE": signature,
            },
            json={"grantType": "client_credentials"},
        )
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = resp.text
        _LOG.warning("Dipay POST /access-token/b2b -> %s: %s", resp.status_code, body)
        raise DipayError(resp.status_code, body)
    payload = resp.json()
    return payload["accessToken"], int(payload.get("expiresIn") or _DEFAULT_TOKEN_TTL_SECONDS)


async def _get_token(*, force: bool = False) -> str:
    """Cached access token. Refreshes 120s before expiry (or on ``force``)."""
    global _token_value, _token_expiry
    async with _token_lock:
        if (
            not force
            and _token_value
            and time.time() < _token_expiry - _TOKEN_REFRESH_MARGIN_SECONDS
        ):
            return _token_value
        token, ttl = await _fetch_token()
        _token_value = token
        _token_expiry = time.time() + ttl
        return token


# ---------------------------------------------------------------------------
# Signed request plumbing
# ---------------------------------------------------------------------------


def _error_from_response(resp: httpx.Response) -> DipayError:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = resp.text
    return DipayError(resp.status_code, body)


async def _signed_request(
    method: str, path: str, *, access_token: str, json: dict | None = None
) -> dict:
    """One signed SNAP call. Raises DipayError on HTTP >= 400."""
    url = f"{_base_url()}{path}"
    body_str = _minify(json)
    x_timestamp = _x_timestamp()
    string_to_sign = _string_to_sign(
        method=method,
        full_url=url,
        access_token=access_token,
        body_str=body_str,
        x_timestamp=x_timestamp,
    )
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "X-TIMESTAMP": x_timestamp,
        "X-SIGNATURE": _hmac_sha512_hex(string_to_sign, settings.dipay_client_secret),
        "X-PARTNER-ID": settings.dipay_client_key,
        "X-EXTERNAL-ID": uuid.uuid4().hex,  # 32 alnum, unique per call
        "CHANNEL-ID": "DPAPI",
    }
    # Send the exact minified bytes we hashed — httpx's json= serializer
    # would re-order / re-space and break the signature.
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(method, url, headers=headers, content=body_str)
    if resp.status_code >= 400:
        body = _error_from_response(resp).body
        _LOG.warning("Dipay %s %s -> %s: %s", method, path, resp.status_code, body)
        raise DipayError(resp.status_code, body)
    return resp.json()


async def _request(method: str, path: str, *, json: dict | None = None) -> dict:
    """Signed request with the cached token; retries ONCE on 401 with a
    forced token refresh (SNAP tokens expire every 15 min)."""
    if not settings.dipay_client_key:
        raise DipayError(0, "DIPAY_CLIENT_KEY not configured")
    token = await _get_token()
    try:
        return await _signed_request(method, path, access_token=token, json=json)
    except DipayError as e:
        if e.status_code != 401:
            raise
        _LOG.warning("Dipay 401 on %s %s — forcing token refresh, retrying once", method, path)
        token = await _get_token(force=True)
        return await _signed_request(method, path, access_token=token, json=json)


# ---------------------------------------------------------------------------
# QRIS (buyer-facing "money-in")
# ---------------------------------------------------------------------------


async def create_qris(
    *,
    partner_reference_no: str,
    amount_idr: int,
    merchant_id: str | None = None,
    validity_period: str | None = None,
) -> dict:
    """Generate a QRIS MPM QR (``POST /qr/qr-mpm-generate``).

    Returns the raw response — ``qrString``/``qrContent`` carries the
    EMVCo payload the buyer scans (we render our own PNG from it). The
    response's ``responseCode`` for success starts with ``2004700``-style
    prefixes per SNAP QRIS service codes (``47`` here).
    """
    body: dict[str, Any] = {
        "partnerReferenceNo": partner_reference_no,
        "amount": {"value": f"{int(amount_idr)}.00", "currency": "IDR"},
    }
    resolved_merchant_id = merchant_id or settings.dipay_merchant_id
    if resolved_merchant_id:
        body["merchantId"] = resolved_merchant_id
    if validity_period:
        body["validityPeriod"] = validity_period
    return await _request("POST", "/qr/qr-mpm-generate", json=body)


async def query_qris(*, partner_reference_no: str) -> dict:
    """Poll a QRIS transaction's state (``POST /qr/qr-mpm-query``).

    SNAP QRIS query service code is ``47``. Used by the status webhook /
    poller to confirm a payment before marking the invoice PAID.
    """
    return await _request(
        "POST",
        "/qr/qr-mpm-query",
        json={
            "originalPartnerReferenceNo": partner_reference_no,
            "serviceCode": "47",
        },
    )


# ---------------------------------------------------------------------------
# Disbursement ("money-out") — see https://api-docs.dipay.id/ disbursement
# pages. On escrow release we transfer from the merchant's Dipay balance to
# the brand/seller's bank account.
# ---------------------------------------------------------------------------


async def inquiry_bank_account(
    *,
    partner_reference_no: str,
    beneficiary_account: str,
    beneficiary_bank_code: str,
    amount_idr: int,
) -> dict:
    """Verify a beneficiary bank account (``POST /emoney/bank-account-inquiry``).

    Optional pre-check before ``create_disbursement`` — confirms the
    account exists at ``beneficiary_bank_code`` for ``amount_idr``.
    """
    return await _request(
        "POST",
        "/emoney/bank-account-inquiry",
        json={
            "partnerReferenceNo": partner_reference_no,
            "customerNumber": settings.dipay_client_key,
            "amount": {"value": f"{int(amount_idr)}.00", "currency": "IDR"},
            "beneficiaryAccountNumber": beneficiary_account,
            "additionalInfo": {"beneficiaryBankCode": beneficiary_bank_code},
        },
    )


async def create_disbursement(
    *,
    partner_reference_no: str,
    beneficiary_account: str,
    beneficiary_bank_code: str,
    amount_idr: int,
    customer_reference: str | None = None,
    partner_merchant_id: str | None = None,
    beneficiary_email: str | None = None,
) -> dict:
    """Create a bank transfer from the Dipay balance (``POST /emoney/transfer-bank``).

    ``partner_reference_no`` is the idempotency key — Dipay echoes it back
    in the status callback / status query and rejects duplicates
    (``responseCode`` ``40943xx``). Disbursement status service code is
    ``43``, so a successful create is ``20043xx``.

    Returns the raw response (``referenceNo``, ``responseCode``,
    ``responseMessage``, …). Non-2xx HTTP raises :class:`DipayError`;
    business rejections are classified by the caller
    (``services/dipay_disbursements.py``) since SNAP returns HTTP 200 with
    an error ``responseCode`` for many failures.
    """
    additional_info: dict[str, Any] = {}
    if customer_reference:
        additional_info["customerReference"] = customer_reference
    if partner_merchant_id:
        additional_info["partnerMerchantId"] = partner_merchant_id
    if beneficiary_email:
        additional_info["beneficiaryEmail"] = beneficiary_email
    body: dict[str, Any] = {
        "partnerReferenceNo": partner_reference_no,
        "customerNumber": settings.dipay_client_key,
        "amount": {"value": f"{int(amount_idr)}.00", "currency": "IDR"},
        "beneficiaryAccountNumber": beneficiary_account,
        "beneficiaryBankCode": beneficiary_bank_code,
    }
    if additional_info:
        body["additionalInfo"] = additional_info
    return await _request("POST", "/emoney/transfer-bank", json=body)


async def get_disbursement_status(
    *, partner_reference_no: str, send_callback: bool = False
) -> dict:
    """Poll a disbursement (``POST /transfer/status``, service code ``43``).

    Set ``send_callback=True`` to ask Dipay to re-fire the status callback
    (useful when the configured callback URL missed one).
    """
    body: dict[str, Any] = {
        "originalPartnerReferenceNo": partner_reference_no,
        "serviceCode": "43",
    }
    if send_callback:
        body["additionalInfo"] = {"sendCallback": True}
    return await _request("POST", "/transfer/status", json=body)


async def get_balance() -> dict:
    """Fetch the merchant's Dipay balance (``POST /balance-inquiry``).

    Optional pre-check before disbursing; not required for the happy path.
    """
    return await _request("POST", "/balance-inquiry", json={})
