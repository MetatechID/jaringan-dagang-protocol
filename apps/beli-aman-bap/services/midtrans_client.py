"""Midtrans Payment Link API. Server-only credentials; no automatic POST retry."""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import re
from urllib.parse import quote, urlparse

import httpx


class MidtransError(Exception):
    def __init__(self, status_code: int = 502):
        self.status_code = status_code
        super().__init__(f"Midtrans request failed ({status_code})")


@dataclass(frozen=True)
class MidtransConfig:
    server_key: str = field(repr=False)
    environment: str = "sandbox"
    merchant_id: str | None = None

    def __post_init__(self):
        if not self.server_key.strip() or self.environment not in {"sandbox", "production"}:
            raise ValueError("Incomplete Midtrans configuration")

    @property
    def base_url(self):
        return "https://api.sandbox.midtrans.com" if self.environment == "sandbox" else "https://api.midtrans.com"


def resolve_config(brand, settings) -> MidtransConfig:
    names = ("midtrans_server_key", "midtrans_environment", "midtrans_merchant_id")
    # Store selection/ownership is brand-scoped; merchant account is platform-wide.
    source = settings
    return MidtransConfig(
        getattr(source, names[0], None) or "",
        getattr(source, names[1], None) or "",
        getattr(source, names[2], None) or None,
    )


def amount_idr(value) -> int:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount != amount.to_integral_value():
            raise ValueError
        return int(amount)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Invalid integer IDR amount") from None


def base_reference(attempt_id: str) -> str:
    base, sep, timestamp = attempt_id.rpartition("-")
    if not sep or not timestamp.isdigit() or not base or len(base) > 50:
        raise ValueError("Invalid Midtrans attempt reference")
    return base


def verify_signature(payload: dict, config: MidtransConfig) -> bool:
    values = [payload.get(k) for k in ("order_id", "status_code", "gross_amount")]
    if any(not isinstance(v, (str, int)) or isinstance(v, bool) for v in values):
        return False
    signature = payload.get("signature_key")
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-fA-F]{128}", signature):
        return False
    expected = hashlib.sha512(("".join(str(v) for v in values) + config.server_key).encode()).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


def payment_url(value: str, config: MidtransConfig) -> str:
    expected = "app.sandbox.midtrans.com" if config.environment == "sandbox" else "app.midtrans.com"
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.netloc != expected or not parsed.path.startswith("/payment-links/"):
        raise MidtransError()
    return value


async def _request(config: MidtransConfig, method: str, path: str, body=None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.request(method, config.base_url + path,
                auth=httpx.BasicAuth(config.server_key, ""),
                headers={"Accept": "application/json"}, json=body)
        if not 200 <= response.status_code < 300:
            raise MidtransError(response.status_code)
        result = response.json()
        if not isinstance(result, dict):
            raise MidtransError()
        return result
    except (httpx.HTTPError, ValueError):
        raise MidtransError() from None


async def create_link(config: MidtransConfig, reference: str, total: int, duration: int = 60) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.~-]{1,50}", reference):
        raise ValueError("Invalid Midtrans base reference")
    return await _request(config, "POST", "/v1/payment-links", {
        "transaction_details": {"order_id": reference, "gross_amount": amount_idr(total)},
        "usage_limit": 1, "expiry": {"duration": duration, "unit": "minute"},
        "credit_card": {"secure": True}, "custom_field1": reference,
    })


async def get_link(config: MidtransConfig, reference: str) -> dict:
    return await _request(config, "GET", "/v1/payment-links/" + quote(reference, safe=""))


async def get_status(config: MidtransConfig, attempt_id: str) -> dict:
    return await _request(config, "GET", "/v2/" + quote(attempt_id, safe="") + "/status")
