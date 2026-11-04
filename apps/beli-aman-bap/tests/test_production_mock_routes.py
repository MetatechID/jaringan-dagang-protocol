"""JD-005: exercise real app lifespan and every registered mock route offline."""

import importlib
import os
from pathlib import Path
import socket
import sys
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from pydantic_settings import BaseSettings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for key in list(os.environ):
        monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(BaseSettings.model_config, "env_file", None)
    for key, value in {
        "ENVIRONMENT": "test", "DEBUG": "false",
        "DATABASE_URL": "postgresql+asyncpg://test:test@127.0.0.1:1/test",
        "BECKN_WORKERS_ENABLED": "false", "SKIP_CREATE_ALL": "true",
        "ADMIN_TOKEN": "synthetic-jd005-admin", "SELLER_BRIDGE_TOKEN": "synthetic-jd005-bridge",
    }.items():
        monkeypatch.setenv(key, value)
    def deny(*args, **kwargs):
        raise AssertionError("unexpected external I/O")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


@pytest.mark.parametrize("mode", ["production", "staging", "development", "test"])
def test_mock_routes_are_local_only(monkeypatch, mode):
    from config import settings
    from database import get_db
    from routers.internal_mock import router
    monkeypatch.setattr(settings, "environment", mode)
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "admin_token", "synthetic-jd005-admin")
    monkeypatch.setattr(settings, "seller_bridge_token", "synthetic-jd005-bridge")
    main = importlib.import_module("main")
    main = importlib.reload(main)
    monkeypatch.setattr(main, "engine", Mock(dispose=AsyncMock()))
    touched = Mock(side_effect=AssertionError("mock route touched database"))
    async def deny_db():
        touched()
        yield
    main.app.dependency_overrides[get_db] = deny_db
    try:
        with TestClient(main.app) as client:
            paths = main.app.openapi()["paths"]
            for route in router.routes:
                path = route.path.replace("{order_id}", "synthetic-order")
                for method in route.methods - {"HEAD", "OPTIONS"}:
                    if mode in {"production", "staging"}:
                        response = client.request(method, path, headers={
                            "X-Admin-Token": "synthetic-jd005-admin",
                        }, json={"invoice_id": "dipay-dev-synthetic"})
                        assert response.status_code == 404, (mode, method, path, response.text)
                        assert route.path not in paths
                    else:
                        assert route.path in paths
                        # Existing admin protection must remain for local tools.
                        response = client.request(method, path)
                        assert response.status_code in {401, 403}
            touched.assert_not_called()
    finally:
        main.app.dependency_overrides.clear()
