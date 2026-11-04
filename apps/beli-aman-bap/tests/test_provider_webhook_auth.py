"""JD-036: provider callback authentication before payload or business effects."""

import importlib
import os
from pathlib import Path
import socket
import sys
from unittest.mock import AsyncMock

import pytest
from pydantic_settings import BaseSettings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for key in list(os.environ):
        monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(BaseSettings.model_config, "env_file", None)
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
    def deny(*args, **kwargs):
        raise AssertionError("Unexpected outbound I/O")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


CASES = [
    ("xendit", "invoice"), ("xendit", "disbursement"),
    ("xendit", "refund"), ("biteship", "tracking"),
]


@pytest.mark.parametrize("provider,endpoint", CASES)
@pytest.mark.parametrize("expected,presented,status", [
    ("", None, 503), ("", "arbitrary", 503),
    ("   ", "   ", 503), ("\t", "arbitrary", 503),
    ("synthetic-secret", None, 401), ("synthetic-secret", "", 401),
    ("synthetic-secret", "wrong", 401), ("synthetic-secret", " synthetic-secret ", 401),
])
def test_denied_before_body_or_business_io(monkeypatch, provider, endpoint, expected, presented, status):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from config import settings
    from database import get_db
    module = importlib.import_module(f"routers.webhooks_{provider}")
    monkeypatch.setattr(settings, f"{provider}_webhook_token", expected)
    app = FastAPI()
    app.include_router(module.router)
    db = AsyncMock()
    async def fake_db():
        yield db
    app.dependency_overrides[get_db] = fake_db
    headers = {}
    if presented is not None:
        headers["x-callback-token" if provider == "xendit" else "authorization"] = presented
    with TestClient(app) as client:
        response = client.post(f"/webhooks/{provider}/{endpoint}", content=b"invalid-json", headers=headers)
    assert response.status_code == status
    db.execute.assert_not_called()
    db.add.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.parametrize("provider", ["xendit", "biteship"])
def test_valid_exact_credentials_and_nonascii_mismatch(monkeypatch, provider):
    from fastapi import HTTPException
    from config import settings
    module = importlib.import_module(f"routers.webhooks_{provider}")
    monkeypatch.setattr(settings, f"{provider}_webhook_token", "synthetic-secret")
    if provider == "xendit":
        module._verify_callback_token("synthetic-secret")
        with pytest.raises(HTTPException) as error:
            module._verify_callback_token("wrong-é")
    else:
        module._verify("synthetic-secret", None)
        module._verify("Bearer synthetic-secret", None)
        module._verify(None, "synthetic-secret")
        with pytest.raises(HTTPException) as error:
            module._verify(None, "wrong-é")
    assert error.value.status_code == 401


@pytest.mark.parametrize("provider,endpoint", CASES)
def test_valid_token_reaches_payload_validation(monkeypatch, provider, endpoint):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from config import settings
    from database import get_db
    module = importlib.import_module(f"routers.webhooks_{provider}")
    monkeypatch.setattr(settings, f"{provider}_webhook_token", "synthetic-secret")
    app = FastAPI()
    app.include_router(module.router)
    async def fake_db():
        yield AsyncMock()
    app.dependency_overrides[get_db] = fake_db
    headers = {"x-callback-token" if provider == "xendit" else "authorization": "synthetic-secret"}
    with TestClient(app) as client:
        response = client.post(f"/webhooks/{provider}/{endpoint}", json={}, headers=headers)
    assert response.status_code == 400


def test_biteship_blank_query_credential_is_not_configuration(monkeypatch):
    from fastapi import HTTPException
    from config import settings
    from routers.webhooks_biteship import _verify
    monkeypatch.setattr(settings, "biteship_webhook_token", "   ")
    with pytest.raises(HTTPException) as error:
        _verify(None, "   ")
    assert error.value.status_code == 503
