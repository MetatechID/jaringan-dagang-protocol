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
import os
import time
import uuid
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class DipayConfig:
    """Complete, immutable credentials and endpoint settings for one merchant.

    A config always comes entirely from one brand override or entirely from
    environment settings.  Keeping the resolved bundle explicit prevents a
    brand client key from ever being combined with the environment secret or
    private key.
    """

    base_url: str
    client_key: str
    client_secret: str
    private_key_b64: str
    private_key_path: str
    merchant_id: str

    @property
    def token_identity(self) -> tuple[str, str]:
        """Non-secret key for the per-merchant token cache."""
        return (self.base_url.rstrip("/"), self.client_key)


_BRAND_CONFIG_FIELDS = (
    "dipay_client_key",
    "dipay_client_secret",
    "dipay_private_key_b64",
    "dipay_merchant_id",
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def resolve_config(brand: Brand | Any | None = None) -> DipayConfig:
    """Resolve a complete brand override or a complete environment fallback.

    A brand override is selected when *any* brand credential field is present;
    all four brand fields must then be present.  Brand and environment fields
    are deliberately never mixed.  The environment fallback may use exactly
    one private-key source: inline base64 or a filesystem path.
    """
    brand_values = {
        field: _clean(getattr(brand, field, None))
        for field in _BRAND_CONFIG_FIELDS
    }
    configured_brand_fields = [name for name, value in brand_values.items() if value]
    if configured_brand_fields:
        missing = [name for name, value in brand_values.items() if not value]
        if missing:
            raise DipayError(
                0,
                "Incomplete brand Dipay credentials; configure all of "
                + ", ".join(_BRAND_CONFIG_FIELDS)
                + f" (missing: {', '.join(missing)})",
            )
        return DipayConfig(
            base_url=_clean(settings.dipay_base_url).rstrip("/"),
            client_key=brand_values["dipay_client_key"],
            client_secret=brand_values["dipay_client_secret"],
            private_key_b64=brand_values["dipay_private_key_b64"],
            private_key_path="",
            merchant_id=brand_values["dipay_merchant_id"],
        )

    env_values = {
        "dipay_client_key": _clean(settings.dipay_client_key),
        "dipay_client_secret": _clean(settings.dipay_client_secret),
        "dipay_merchant_id": _clean(settings.dipay_merchant_id),
    }
    private_key_b64 = _clean(settings.dipay_private_key_b64)
    private_key_path = _clean(settings.dipay_private_key_path)
    missing = [name for name, value in env_values.items() if not value]
    if not (private_key_b64 or private_key_path):
        missing.append("DIPAY_PRIVATE_KEY_B64 or DIPAY_PRIVATE_KEY_PATH")
    if missing:
        raise DipayError(
            0,
            "Incomplete environment Dipay credentials (missing: "
            + ", ".join(missing)
            + ")",
        )
    return DipayConfig(
        base_url=_clean(settings.dipay_base_url).rstrip("/"),
        client_key=env_values["dipay_client_key"],
        client_secret=env_values["dipay_client_secret"],
        private_key_b64=private_key_b64,
        private_key_path=private_key_path,
        merchant_id=env_values["dipay_merchant_id"],
    )


def is_configured(brand: Brand | Any | None = None) -> bool:
    try:
        resolve_config(brand)
    except DipayError:
        return False
    return True


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


def _load_private_key(config: DipayConfig):
    """Load ``config``'s RSA private key for access-token signing.

    Cleanly handles raw PEM text, base64-encoded PEM, DER, and file paths.
    """
    raw_key = config.private_key_b64.strip()
    if raw_key:
        if raw_key.startswith("-----BEGIN"):
            key_bytes = raw_key.encode("utf-8")
        else:
            try:
                key_bytes = base64.b64decode(raw_key)
            except Exception:
                key_bytes = raw_key.encode("utf-8")
        try:
            return serialization.load_pem_private_key(key_bytes, password=None)
        except (ValueError, TypeError):
            try:
                return serialization.load_der_private_key(key_bytes, password=None)
            except Exception:
                return None
    if config.private_key_path:
        try:
            with open(config.private_key_path, "rb") as fh:
                data = fh.read()
            try:
                return serialization.load_pem_private_key(data, password=None)
            except (ValueError, TypeError):
                return serialization.load_der_private_key(data, password=None)
        except OSError:
            return None
    return None


def _rsa_sha256(message: str, private_key) -> str:
    """SHA256withRSA — PKCS1v15 + SHA256, Base64 encoded per SNAP BI spec."""
    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def _rsa_sha256_b64(message: str, private_key) -> str:
    return _rsa_sha256(message, private_key)


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
    # SNAP timestamps are ISO-8601 of length 25 (seconds precision); Jakarta is Dipay's operating tz.
    return datetime.now(JAKARTA).isoformat(timespec="seconds")


def _string_to_sign(
    *, method: str, full_url: str, access_token: str, body_str: str, x_timestamp: str
) -> str:
    return f"{method}:{full_url}:{access_token}:{_sha256_hex(body_str)}:{x_timestamp}"


def _hmac_sha512(string_to_sign: str, client_secret: str) -> str:
    """HMAC-SHA512, Base64 encoded per SNAP BI spec."""
    digest = hmac.new(
        client_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha512,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _hmac_sha512_b64(string_to_sign: str, client_secret: str) -> str:
    return _hmac_sha512(string_to_sign, client_secret)


def _hmac_sha512_hex(string_to_sign: str, client_secret: str) -> str:
    return hmac.new(
        client_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha512,
    ).hexdigest()


# ---------------------------------------------------------------------------
# Access token (cached module-level, isolated by non-secret merchant identity)
# ---------------------------------------------------------------------------

_token_cache: dict[tuple[str, str], tuple[str, float]] = {}
_token_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _reset_token_cache() -> None:
    """Test hook / error-path helper: drop every merchant's cached token."""
    _token_cache.clear()
    _token_locks.clear()


async def _fetch_token(config: DipayConfig) -> tuple[str, int]:
    """POST /access-token/b2b → (accessToken, expiresIn_seconds)."""
    if not config.client_key:
        raise DipayError(
            0, "Incomplete environment Dipay credentials (missing: dipay_client_key)"
        )
    url = f"{config.base_url}/access-token/b2b"
    private_key = _load_private_key(config)
    if private_key is None:
        raise DipayError(0, "Dipay private key not configured")
    x_timestamp = _x_timestamp()
    signature = _rsa_sha256(f"{config.client_key}|{x_timestamp}", private_key)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            headers={
                "Content-Type": "application/json",
                "X-TIMESTAMP": x_timestamp,
                "X-CLIENT-KEY": config.client_key,
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


async def _get_token(config: DipayConfig, *, force: bool = False) -> str:
    """Cached token for ``config``; refresh early and once on forced retry."""
    identity = config.token_identity
    lock = _token_locks.setdefault(identity, asyncio.Lock())
    async with lock:
        token, expiry = _token_cache.get(identity, (None, 0.0))
        if (
            not force
            and token
            and time.time() < expiry - _TOKEN_REFRESH_MARGIN_SECONDS
        ):
            return token
        token, ttl = await _fetch_token(config)
        _token_cache[identity] = (token, time.time() + ttl)
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
    method: str,
    path: str,
    *,
    config: DipayConfig,
    access_token: str,
    json: dict | None = None,
) -> dict:
    """One signed SNAP call. Raises DipayError on HTTP >= 400."""
    url = f"{config.base_url}{path}"
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
        "X-SIGNATURE": _hmac_sha512(string_to_sign, config.client_secret),
        "X-PARTNER-ID": config.client_key,
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


async def _request(
    method: str,
    path: str,
    *,
    config: DipayConfig,
    json: dict | None = None,
) -> dict:
    """Signed request with per-config cache; retry once on a 401."""
    token = await _get_token(config)
    try:
        return await _signed_request(
            method, path, config=config, access_token=token, json=json,
        )
    except DipayError as e:
        if e.status_code != 401:
            raise
        _LOG.warning("Dipay 401 on %s %s — forcing token refresh, retrying once", method, path)
        token = await _get_token(config, force=True)
        return await _signed_request(
            method, path, config=config, access_token=token, json=json,
        )


# ---------------------------------------------------------------------------
# QRIS (buyer-facing "money-in")
# ---------------------------------------------------------------------------


async def create_qris(
    *,
    config: DipayConfig,
    partner_reference_no: str,
    amount_idr: int,
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
    if config.merchant_id:
        body["merchantId"] = config.merchant_id
    if validity_period:
        body["validityPeriod"] = validity_period
    return await _request(
        "POST", "/qr/qr-mpm-generate", config=config, json=body,
    )


async def query_qris(
    *, config: DipayConfig, partner_reference_no: str,
) -> dict:
    """Poll a QRIS transaction's state (``POST /qr/qr-mpm-query``).

    SNAP QRIS query service code is ``47``. Used by the status webhook /
    poller to confirm a payment before marking the invoice PAID.
    """
    return await _request(
        "POST",
        "/qr/qr-mpm-query",
        config=config,
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
    config: DipayConfig,
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
        config=config,
        json={
            "partnerReferenceNo": partner_reference_no,
            "customerNumber": config.client_key,
            "amount": {"value": f"{int(amount_idr)}.00", "currency": "IDR"},
            "beneficiaryAccountNumber": beneficiary_account,
            "additionalInfo": {"beneficiaryBankCode": beneficiary_bank_code},
        },
    )


async def create_disbursement(
    *,
    config: DipayConfig,
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
        "customerNumber": config.client_key,
        "amount": {"value": f"{int(amount_idr)}.00", "currency": "IDR"},
        "beneficiaryAccountNumber": beneficiary_account,
        "beneficiaryBankCode": beneficiary_bank_code,
    }
    if additional_info:
        body["additionalInfo"] = additional_info
    return await _request(
        "POST", "/emoney/transfer-bank", config=config, json=body,
    )


async def get_disbursement_status(
    *,
    config: DipayConfig,
    partner_reference_no: str,
    send_callback: bool = False,
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
    return await _request(
        "POST", "/transfer/status", config=config, json=body,
    )


async def get_balance(
    *,
    config: DipayConfig,
    partner_reference_no: str | None = None,
) -> dict:
    """Fetch the merchant's Dipay balance (``POST /balance-inquiry``).

    Requires partnerReferenceNo and balanceTypes per SNAP BI / Dipay requirements.
    """
    ref = partner_reference_no or snap_ref("b", uuid.uuid4().hex)
    return await _request(
        "POST",
        "/balance-inquiry",
        config=config,
        json={
            "partnerReferenceNo": ref,
            "balanceTypes": ["deposit"],
        },
    )


# ---------------------------------------------------------------------------
# Notification signature verification (incoming webhooks)
# ---------------------------------------------------------------------------


def _load_public_key(key_material: str | Any | None):
    """Load an RSA public key from a PEM string, base64-encoded PEM, path, or key object."""
    if key_material is None:
        return None
    if hasattr(key_material, "verify"):
        return key_material
    if not isinstance(key_material, str):
        return None
    raw = key_material.strip()
    if not raw:
        return None
    if raw.startswith("-----BEGIN"):
        key_bytes = raw.encode("utf-8")
    elif os.path.exists(raw):
        try:
            with open(raw, "rb") as fh:
                key_bytes = fh.read()
        except OSError:
            return None
    else:
        try:
            key_bytes = base64.b64decode(raw)
        except Exception:
            key_bytes = raw.encode("utf-8")
    try:
        return serialization.load_pem_public_key(key_bytes)
    except Exception:
        try:
            return serialization.load_der_public_key(key_bytes)
        except Exception:
            return None


def verify_notification_signature(
    method: str,
    path: str,
    body: dict | str | bytes | None,
    timestamp: str,
    signature: str,
    public_key: str | Any | None = None,
) -> bool:
    """Verify Dipay's incoming webhook notification signature.

    Uses SHA256withRSA over '{HTTPMethod}:{HTTPPath}:{sha256(minify(body))}:{X-TIMESTAMP}'
    with Dipay's callback public key.
    """
    key_material = (
        public_key
        if public_key is not None
        else getattr(settings, "dipay_callback_public_key", "")
    )
    pub_key_obj = _load_public_key(key_material)
    if pub_key_obj is None:
        _LOG.warning("Dipay callback public key not configured or invalid")
        return False

    if isinstance(body, dict):
        body_str = _minify(body)
    elif isinstance(body, (bytes, bytearray)):
        body_str = body.decode("utf-8")
    elif isinstance(body, str):
        body_str = body
    else:
        body_str = "{}"

    body_hash = _sha256_hex(body_str)
    string_to_verify = f"{method.upper()}:{path}:{body_hash}:{timestamp}"

    sig_str = (signature or "").strip()
    if not sig_str:
        return False

    try:
        if len(sig_str) == 512 and all(c in "0123456789abcdefABCDEF" for c in sig_str):
            sig_bytes = bytes.fromhex(sig_str)
        else:
            try:
                sig_bytes = base64.b64decode(sig_str)
            except Exception:
                sig_bytes = bytes.fromhex(sig_str)
    except Exception:
        return False

    try:
        pub_key_obj.verify(
            sig_bytes,
            string_to_verify.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception:
        return False
