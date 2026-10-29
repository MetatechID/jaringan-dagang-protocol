"""JD-004 callback-key completeness; run via scripts/test-dipay-callback-key.py."""
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from fastapi import HTTPException
from starlette.requests import Request

import main
from config import Settings
from routers import partner, webhooks_dipay
from services import dipay_client, dipay_invoices
from tests._dipay_fakes import FakeSession, StubBrand, StubCart, StubOrder


@pytest.fixture(scope="module")
def keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key()
    return private, public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


@pytest.fixture(autouse=True)
def config(monkeypatch):
    s = Settings(_env_file=None, environment="production")
    for module in (main, dipay_client, dipay_invoices, webhooks_dipay):
        monkeypatch.setattr(module, "settings", s)
    return s


@pytest.mark.parametrize("mode", ["production", "staging", "preview", "", None])
@pytest.mark.parametrize("surface", ["cart", "order", "partner"])
@pytest.mark.parametrize("material", ["", "invalid", "ec"])
@pytest.mark.asyncio
async def test_missing_key_blocks_before_effects(mode, surface, material, config, monkeypatch):
    config.environment = mode
    config.dipay_callback_public_key = (
        ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode() if material == "ec" else material
    )
    brand = StubBrand()
    db = FakeSession([brand])
    reservation = AsyncMock()
    network = AsyncMock(return_value={"responseCode": "2004700", "qrContent": "synthetic"})
    monkeypatch.setattr(dipay_client, "create_qris", network)
    obj = StubCart() if surface == "cart" else StubOrder()
    before = vars(obj).copy()
    with pytest.raises(dipay_client.DipayError, match="DIPAY_CALLBACK_PUBLIC_KEY"):
        if surface == "cart":
            await dipay_invoices.create_invoice_for_cart(db, obj, reservation_db=reservation)
        elif surface == "order":
            await dipay_invoices.create_invoice_for_order(db, obj, reservation_db=reservation)
        else:
            await partner._create_dipay_invoice(brand, obj, 1800)
    assert vars(obj) == before
    assert not reservation.mock_calls
    network.assert_not_called()


@pytest.mark.parametrize("material", ["", "  ", "invalid", "ec", "private", "missing-path"])
@pytest.mark.asyncio
async def test_startup_rejects_unusable_key(material, config, keys, tmp_path, monkeypatch, caplog):
    private, _ = keys
    values = {
        "ec": ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode(),
        "private": private.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
        "missing-path": str(tmp_path / "missing.pem"),
    }
    config.dipay_client_key = "synthetic-enabled"
    config.dipay_callback_public_key = values.get(material, material)
    engine = AsyncMock()
    monkeypatch.setattr(main, "engine", engine)
    with pytest.raises(dipay_client.DipayError, match="DIPAY_CALLBACK_PUBLIC_KEY") as error:
        async with main.lifespan(main.app):
            pytest.fail("startup accepted unusable callback key")
    assert not engine.mock_calls
    if config.dipay_callback_public_key.strip():
        assert config.dipay_callback_public_key not in str(error.value)
        assert config.dipay_callback_public_key not in caplog.text


@pytest.mark.parametrize("encoding", ["pem", "b64-pem", "b64-der", "path"])
@pytest.mark.asyncio
async def test_accepted_key_contract(encoding, config, keys, tmp_path, monkeypatch):
    private, pem = keys
    path = tmp_path / "callback.pem"
    path.write_text(pem)
    config.dipay_callback_public_key = {
        "pem": pem,
        "b64-pem": base64.b64encode(pem.encode()).decode(),
        "b64-der": base64.b64encode(private.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
        )).decode(),
        "path": str(path),
    }[encoding]
    config.dipay_client_key = "synthetic-enabled"
    monkeypatch.setattr(main, "engine", AsyncMock())
    async with main.lifespan(main.app):
        pass
    assert dipay_invoices._resolved_config(StubBrand()) is not None
    body, timestamp, endpoint = {}, "synthetic-timestamp", "/webhooks/dipay/qris"
    message = f"POST:{endpoint}:{dipay_client._sha256_hex('{}')}:{timestamp}"
    signature = base64.b64encode(private.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()
    assert dipay_client.verify_notification_signature("POST", endpoint, body, timestamp, signature)


def test_environment_fallback_also_requires_callback_key(config):
    config.dipay_client_key = "synthetic-env-key"
    config.dipay_client_secret = "synthetic-env-secret"
    config.dipay_private_key_b64 = "synthetic-signing-key"
    config.dipay_merchant_id = "synthetic-merchant"
    brand = StubBrand(dipay_client_key="", dipay_client_secret="",
                      dipay_private_key_b64="", dipay_merchant_id="")
    with pytest.raises(dipay_client.DipayError, match="DIPAY_CALLBACK_PUBLIC_KEY"):
        dipay_invoices._resolved_config(brand)


@pytest.mark.parametrize("mode", ["development", "test"])
def test_explicit_local_fixture_remains_allowed(mode, config):
    config.environment = mode
    assert dipay_invoices._resolved_config(StubBrand()) is not None
    assert dipay_invoices._resolved_config(None) is None


@pytest.mark.asyncio
async def test_disabled_environment_startup_does_not_require_key(config, monkeypatch):
    monkeypatch.setattr(main, "engine", AsyncMock())
    async with main.lifespan(main.app):
        pass


@pytest.mark.parametrize("mode", ["staging", "preview", "", None])
@pytest.mark.asyncio
async def test_unknown_modes_never_skip_callback_verification(mode, config):
    config.environment = mode
    request = Request({"type": "http", "method": "POST", "path": "/webhooks/dipay/qris", "headers": []})
    with pytest.raises(HTTPException) as error:
        await webhooks_dipay._verify_inbound_signature(request, b"{}", None, None)
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_partner_endpoint_checks_before_row_creation(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(partner, "create_partner_order", create)
    with pytest.raises(HTTPException) as error:
        await partner.register_order(None, StubBrand(), AsyncMock())
    assert error.value.status_code == 502
    create.assert_not_called()


@pytest.mark.asyncio
async def test_rotated_file_rechecked_at_runtime(config, keys, tmp_path):
    path = tmp_path / "rotating.pem"
    path.write_text(keys[1])
    config.dipay_callback_public_key = str(path)
    assert dipay_invoices._resolved_config(StubBrand()) is not None
    path.write_text("invalid")
    with pytest.raises(dipay_client.DipayError, match="DIPAY_CALLBACK_PUBLIC_KEY"):
        dipay_invoices._resolved_config(StubBrand())


def test_missing_environment_never_enables_mock(monkeypatch):
    monkeypatch.setattr(dipay_invoices, "settings", SimpleNamespace())
    assert not dipay_invoices._mock_allowed()
