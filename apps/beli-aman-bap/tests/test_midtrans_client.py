"""Offline Midtrans contract/security checks; never calls a provider."""
import hashlib
from types import SimpleNamespace
import pytest
from services.midtrans_client import (
    MidtransConfig, MidtransError, amount_idr, base_reference, payment_url,
    resolve_config, verify_signature,
)


def test_signature_preserves_decimal_representation():
    config = MidtransConfig("secret")
    payload = {"order_id": "mt-a-b-1234567890123", "status_code": "200", "gross_amount": "10000.00"}
    payload["signature_key"] = hashlib.sha512((payload["order_id"] + "20010000.00secret").encode()).hexdigest()
    assert verify_signature(payload, config)
    payload["gross_amount"] = "10000"
    assert not verify_signature(payload, config)


@pytest.mark.parametrize("value", [0, -1, "NaN", "Infinity", "1.2", None, True])
def test_reject_invalid_amount(value):
    with pytest.raises(ValueError):
        amount_idr(value)


def test_amount_and_reference():
    assert amount_idr("10000.00") == 10000
    assert base_reference("mt-hyphen-base-1234567890123") == "mt-hyphen-base"
    with pytest.raises(ValueError):
        base_reference("no-timestamp")


def test_config_uses_global_bundle_only():
    global_settings = SimpleNamespace(midtrans_server_key="env-secret", midtrans_environment="sandbox", midtrans_merchant_id="env")
    brand = SimpleNamespace(midtrans_server_key="brand-secret", midtrans_environment="production", midtrans_merchant_id="brand")
    config = resolve_config(brand, global_settings)
    assert config.server_key == "env-secret"
    assert config.environment == "sandbox"
    assert config.merchant_id == "env"
    assert "env-secret" not in repr(config)
    global_settings.midtrans_server_key = ""
    with pytest.raises(ValueError):
        resolve_config(brand, global_settings)
    global_settings.midtrans_server_key = "env-secret"
    global_settings.midtrans_environment = "unknown"
    with pytest.raises(ValueError):
        resolve_config(brand, global_settings)


def test_payment_url_environment_binding():
    config = MidtransConfig("secret")
    assert payment_url("https://app.sandbox.midtrans.com/payment-links/opaque", config)
    for value in ["https://app.midtrans.com/payment-links/opaque", "https://evil.test/payment-links/a", "javascript:alert(1)"]:
        with pytest.raises(MidtransError):
            payment_url(value, config)


@pytest.mark.asyncio
async def test_create_payload(monkeypatch):
    from services import midtrans_client
    calls = []
    async def fake(config, method, path, body=None):
        calls.append((method, path, body))
        return {"order_id": "mt-test", "payment_url": "https://app.sandbox.midtrans.com/payment-links/test"}
    monkeypatch.setattr(midtrans_client, "_request", fake)
    await midtrans_client.create_link(MidtransConfig("secret"), "mt-test", 10000)
    method, path, body = calls[0]
    assert (method, path) == ("POST", "/v1/payment-links")
    assert body["usage_limit"] == 1
    assert body["transaction_details"]["gross_amount"] == 10000
    assert body["credit_card"]["secure"] is True
    assert "customer_details" not in body
