"""Async Dipay payment gateway HTTP client (SNAP v2.1).

Dipay's B2B API follows the Indonesian SNAP (Standar Nasional Open API
Pembayaran) v2.1 conventions: a B2B access token obtained with an
RSA-signed request, then per-call HMAC-SHA512 request signatures over a
``stringToSign``.

Auth flow (https://api-docs.dipay.id/ — Authentication sections):
- ``POST {base}/access-token/b2b`` with ``X-CLIENT-KEY`` + ``X-SIGNATURE``
  where the signature is **SHA256withRSA** (PKCS1v15 + SHA256) over
  ``"{client_key}|{x_timestamp}"``, **base64**-encoded, using the
  merchant's RSA private key.  Response: ``{accessToken, expiresIn}``
  (900s in the demo env).
- Business endpoints then carry ``Authorization: Bearer {accessToken}``
  plus ``X-SIGNATURE`` = **base64 HMAC-SHA512** keyed with the client
  secret over
  ``"{METHOD}:{url_path}:{accessToken}:{sha256hex(body)}:{x_timestamp}"``
  (body hash = lowercase hex SHA-256 of the *minified* JSON body — ``{}``
  when there is no body).

Conformance notes — these two points have been verified against a known-
conformant SNAP-BI implementation (Midtrans' ``SnapBi.php``), which builds
the identical 5-leg, path-form stringToSign and the identical
``client_id|timestamp`` token signature:

* The URL leg is the **path** (``"/snap/v2.1/qr/qr-mpm-generate"``), not
  the full URL — confirmed empirically 2026-09-30: the full-URL form is
  rejected by Dipay with ``4014700 Invalid Signature``.
* ``X-EXTERNAL-ID`` is *sent as a header* but is **not** part of
  ``stringToSign`` — SNAP's general spec text implies a trailing
  external-ID leg, but no real SNAP gateway (Dipay or Midtrans) signs it.

RSA key size is deliberately not enforced: Dipay's docs say "RSA 1024"
while our test fixtures use 2048, and prod key material is whatever Dipay
issued — rejecting a valid issued key would be worse than accepting it.

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
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

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
    are deliberately never mixed.  For environment credentials, inline key
    material takes precedence over a configured filesystem path.
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
        except (OSError, ValueError, TypeError):
            return None
    return None


def _token_failure_metadata(config: DipayConfig, private_key: rsa.RSAPrivateKey, signature: str) -> dict:
    """Allowlisted diagnostics; fingerprint uses SHA-256 of DER SubjectPublicKeyInfo."""
    endpoint = urlsplit(config.base_url)
    public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    inline = bool(config.private_key_b64.strip())
    return {
        "endpoint_host": endpoint.hostname,
        "endpoint_path": f"{endpoint.path.rstrip('/')}/access-token/b2b",
        "key_source": "inline" if inline else "file",
        "inline_shadows_file": inline and bool(config.private_key_path),
        "rsa_bits": private_key.key_size,
        "signature_chars": len(signature),
        "public_key_sha256_spki": hashlib.sha256(public_der).hexdigest(),
    }


def _rsa_sha256(message: str, private_key) -> str:
    """SHA256withRSA — PKCS1v15 + SHA256, Base64 encoded per SNAP BI spec."""
    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


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
    *, method: str, url_path: str, access_token: str, body_str: str, x_timestamp: str
) -> str:
    # Dipay signs the URL PATH (e.g. "/snap/v2.1/qr/qr-mpm-generate"), not the
    # full URL — confirmed empirically 2026-09-30 (full URL => 4014700 Invalid
    # Signature; path => 2004700 Successful).
    return f"{method}:{url_path}:{access_token}:{_sha256_hex(body_str)}:{x_timestamp}"


def _hmac_sha512(string_to_sign: str, client_secret: str) -> str:
    """HMAC-SHA512, Base64 encoded per SNAP BI spec."""
    digest = hmac.new(
        client_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha512,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


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
        raise DipayError(0, "Dipay private key not configured or invalid")
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise DipayError(0, "Dipay private key must be RSA")
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
        code = body.get("responseCode") if isinstance(body, dict) else None
        if not (isinstance(code, str) and len(code) == 7 and code.isascii() and code.isdigit()):
            code = "unknown"
        _LOG.warning(
            "Dipay POST /access-token/b2b -> %s response_code=%s signing=%s",
            resp.status_code,
            code,
            _token_failure_metadata(config, private_key, signature),
        )
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
    url = httpx.URL(f"{config.base_url}{path}")
    body_str = _minify(json)
    x_timestamp = _x_timestamp()
    string_to_sign = _string_to_sign(
        method=method,
        url_path=url.path,
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
    # There is deliberately NO ``customer_reference`` parameter: Dipay
    # rejects any transfer-bank body containing
    # ``additionalInfo.customerReference`` with a bogus "4014300
    # Unauthorized. Invalid Signature." (bisection-confirmed 2026-10-01 —
    # ``partnerMerchantId`` alone signs fine).  Do not add it back without
    # re-confirming against the live API.
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


def local_fixtures_allowed() -> bool:
    """Only explicitly named local modes may omit callback verification config."""
    return _clean(getattr(settings, "environment", "")).lower() in {"development", "test"}


def require_callback_public_key() -> None:
    """Fail closed without exposing material or loader errors.

    The callback key is platform-wide, independent of merchant signing keys.
    Parse using the receiver's existing format contract, but require RSA for
    SHA256withRSA verification. Do not cache: operators may rotate file keys.
    """
    if local_fixtures_allowed():
        return
    try:
        key = _load_public_key(getattr(settings, "dipay_callback_public_key", ""))
        usable = isinstance(key, rsa.RSAPublicKey)
    except Exception:
        usable = False
    if not usable:
        raise DipayError(0, "DIPAY_CALLBACK_PUBLIC_KEY must contain a usable RSA public key")


def _strip_whitespace(text: str) -> str:
    """Dipay production's ``minify``: delete EVERY whitespace character,
    including spaces inside string values ("JAKARTA SELATAN" ->
    "JAKARTASELATAN"). Confirmed against captured production QRIS
    callbacks 2026-10-28; the demo env signs the plain minified JSON."""
    return re.sub(r"\s", "", text)


def _notification_body_candidates(
    body: dict | str | bytes | None,
) -> list[tuple[str, str]]:
    """Labelled body forms Dipay may have hashed for the callback signature.

    ``whitespace-stripped`` is what Dipay production signs; ``minified-body``
    is the documented form the demo env signs; ``raw-body`` covers a gateway
    that signs the wire bytes verbatim.
    """
    raw_body: str | None = None
    parsed: Any = None
    if isinstance(body, dict):
        parsed = body
    elif isinstance(body, (bytes, bytearray)):
        raw_body = bytes(body).decode("utf-8", errors="replace")
    elif isinstance(body, str):
        raw_body = body
    else:
        parsed = {}

    if parsed is None and raw_body is not None:
        try:
            parsed = json.loads(raw_body)
        except Exception:
            parsed = None

    candidates: list[tuple[str, str]] = []
    if parsed is not None:
        minified = _minify(parsed)
        candidates.append(("whitespace-stripped", _strip_whitespace(minified)))
        candidates.append(("minified-body", minified))
    if raw_body is not None:
        candidates.append(("raw-body", raw_body))

    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for label, text in candidates:
        if text not in seen:
            seen.add(text)
            unique.append((label, text))
    return unique


def verify_notification_signature_diagnostic(
    method: str,
    path: str,
    body: dict | str | bytes | None,
    timestamp: str,
    signature: str,
    public_key: str | Any | None = None,
    urls: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Verify an inbound notification and return non-sensitive diagnostics.

    ``urls`` is an ordered list of ``(label, endpoint_url)`` forms for the
    ``EndpointUrl`` leg (Dipay's docs don't say path vs full URL); it
    defaults to the request path alone.
    """
    key_material = (
        public_key
        if public_key is not None
        else getattr(settings, "dipay_callback_public_key", "")
    )
    pub_key_obj = _load_public_key(key_material)
    if pub_key_obj is None:
        return {"valid": False, "reason": "missing_or_invalid_public_key"}

    sig_str = (signature or "").strip()
    if not sig_str:
        return {"valid": False, "reason": "missing_signature"}

    encoding = "hex" if len(sig_str) == 512 and all(
        c in "0123456789abcdefABCDEF" for c in sig_str
    ) else "base64"
    try:
        if encoding == "hex":
            sig_bytes = bytes.fromhex(sig_str)
        else:
            sig_bytes = base64.b64decode(sig_str, validate=True)
    except Exception:
        return {
            "valid": False,
            "reason": "unsupported_signature_encoding",
            "signature_encoding": encoding,
            "signature_length": len(sig_str),
        }

    if not timestamp:
        return {
            "valid": False,
            "reason": "missing_timestamp",
            "signature_encoding": encoding,
            "signature_length": len(sig_str),
        }

    url_candidates: list[tuple[str, str]] = []
    for label, url in urls or [("path", path)]:
        if url and url not in {u for _, u in url_candidates}:
            url_candidates.append((label, url))
    body_candidates = _notification_body_candidates(body)

    tried = 0
    for url_label, url in url_candidates:
        for body_label, body_str in body_candidates:
            tried += 1
            string_to_verify = (
                f"{method.upper()}:{url}:{_sha256_hex(body_str)}:{timestamp}"
            )
            try:
                pub_key_obj.verify(
                    sig_bytes,
                    string_to_verify.encode("utf-8"),
                    padding.PKCS1v15(),
                    hashes.SHA256(),
                )
            except Exception:
                continue
            return {
                "valid": True,
                "reason": "verified",
                "body_candidate": body_label,
                "url_candidate": url_label,
                "signature_encoding": encoding,
                "signature_length": len(sig_str),
            }

    return {
        "valid": False,
        "reason": "signature_verification_mismatch",
        "signature_encoding": encoding,
        "signature_length": len(sig_str),
        "candidates_tried": tried,
    }


def verify_notification_signature(
    method: str,
    path: str,
    body: dict | str | bytes | None,
    timestamp: str,
    signature: str,
    public_key: str | Any | None = None,
    urls: list[tuple[str, str]] | None = None,
) -> bool:
    """Backward-compatible boolean wrapper for inbound signature verification."""
    return verify_notification_signature_diagnostic(
        method=method,
        path=path,
        body=body,
        timestamp=timestamp,
        signature=signature,
        public_key=public_key,
        urls=urls,
    )["valid"]
