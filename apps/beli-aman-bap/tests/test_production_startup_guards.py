"""JD-026: isolated configuration and real lifespan tests; no external I/O."""

import asyncio
import importlib
import os
from pathlib import Path
import socket
import sys
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings

BAP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BAP_DIR))
PRODUCTION = {
    "environment": "production",
    "debug": False,
    "admin_token": "synthetic-admin-credential",
    "seller_bridge_token": "synthetic-bridge-credential",
}


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    # Replace inherited configuration BEFORE importing any application modules.
    for key in list(os.environ):
        monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(BaseSettings.model_config, "env_file", None)

    def deny_network(*args, **kwargs):
        raise AssertionError("unexpected external I/O")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
    monkeypatch.setenv("BECKN_WORKERS_ENABLED", "false")
    monkeypatch.setenv("SKIP_CREATE_ALL", "true")


def settings(**overrides):
    from config import Settings
    return Settings(_env_file=None, **overrides)


@pytest.mark.parametrize("mode", ["development", "test", "staging", "production"])
def test_valid_modes_are_normalized(mode):
    value = settings(**{**PRODUCTION, "environment": f"  {mode.upper()}  "})
    assert value.environment == mode


@pytest.mark.parametrize("mode", ["", " ", "prod", "dev", "production-typo", "unknown"])
def test_invalid_environment_rejected(mode):
    with pytest.raises(ValidationError, match="environment"):
        settings(environment=mode)


@pytest.mark.parametrize("field, default", [
    ("admin_token", "dev-admin-token"),
    ("seller_bridge_token", "dev-seller-bridge-token"),
])
@pytest.mark.parametrize("kind", ["missing", "empty", "whitespace", "default", "padded_default"])
@pytest.mark.parametrize("mode", ["production", "staging"])
def test_deployed_credentials_required_even_when_bridge_disabled(field, default, kind, mode):
    values = {**PRODUCTION, "environment": mode, "seller_bridge_enabled": False}
    values.pop(field)
    if kind != "missing":
        values[field] = {"empty": "", "whitespace": " \t\n", "default": default,
                         "padded_default": f"  {default}  "}[kind]
    with pytest.raises(ValidationError, match=field.upper()):
        settings(**values)


@pytest.mark.parametrize("mode", ["production", "staging"])
def test_debug_rejected_outside_local_modes(mode):
    with pytest.raises(ValidationError, match="DEBUG"):
        settings(**{**PRODUCTION, "environment": mode, "debug": True})


@pytest.mark.parametrize("marker", ["", " ", "production", "tru", "2"])
def test_malformed_marker_fails_closed_without_echo(monkeypatch, marker):
    monkeypatch.setenv("BAP_PRODUCTION_DEPLOYMENT", marker)
    with pytest.raises(ValidationError, match="BAP_PRODUCTION_DEPLOYMENT must be a boolean") as error:
        settings(**PRODUCTION)
    assert PRODUCTION["admin_token"] not in str(error.value)
    assert PRODUCTION["seller_bridge_token"] not in str(error.value)


@pytest.mark.parametrize("mode", ["development", "test", "staging"])
def test_false_marker_allows_nonproduction_modes(monkeypatch, mode):
    monkeypatch.setenv("BAP_PRODUCTION_DEPLOYMENT", "false")
    assert settings(**{**PRODUCTION, "environment": mode}).environment == mode


@pytest.mark.parametrize("mode", ["development", "test", "staging"])
def test_independent_marker_rejects_conflicting_mode(monkeypatch, mode):
    monkeypatch.setenv("BAP_PRODUCTION_DEPLOYMENT", "true")
    with pytest.raises(ValidationError, match="ENVIRONMENT"):
        settings(**{**PRODUCTION, "environment": mode})


def test_marker_cannot_come_from_dotenv(monkeypatch, tmp_path):
    monkeypatch.setenv("BAP_PRODUCTION_DEPLOYMENT", "true")
    env_file = tmp_path / "synthetic.env"
    env_file.write_text("BAP_PRODUCTION_DEPLOYMENT=false\nENVIRONMENT=development\n")
    from config import Settings
    with pytest.raises(ValidationError, match="ENVIRONMENT"):
        Settings(_env_file=env_file)


def test_valid_marker_control(monkeypatch):
    monkeypatch.setenv("BAP_PRODUCTION_DEPLOYMENT", "true")
    assert settings(**PRODUCTION).environment == "production"


@pytest.mark.parametrize("mode", ["development", "test"])
def test_local_defaults_still_work(mode):
    value = settings(environment=mode, debug=True)
    assert value.admin_token == "dev-admin-token"
    assert value.seller_bridge_token == "dev-seller-bridge-token"


def test_validation_errors_do_not_echo_credentials():
    with pytest.raises(ValidationError) as error:
        settings(**{**PRODUCTION, "debug": True})
    assert PRODUCTION["admin_token"] not in str(error.value)
    assert PRODUCTION["seller_bridge_token"] not in str(error.value)


@pytest.mark.parametrize("invalid", [
    {"admin_token": " "},
    {"seller_bridge_token": "dev-seller-bridge-token"},
    {"debug": True},
    {"environment": "invalid"},
    {"environment": "development", "marker": "true"},
    {"marker": "malformed-synthetic-marker"},
    {"environment": "staging", "debug": True},
    {"environment": "staging", "admin_token": " "},
    {"environment": "staging", "seller_bridge_token": "dev-seller-bridge-token"},
])
def test_lifespan_rejects_before_database_or_worker_side_effects(monkeypatch, invalid):
    main = importlib.import_module("main")
    value = settings(**PRODUCTION)
    for key, item in invalid.items():
        if key == "marker":
            monkeypatch.setenv("BAP_PRODUCTION_DEPLOYMENT", item)
        else:
            setattr(value, key, item)
    monkeypatch.setattr(main, "settings", value)
    begin = Mock(side_effect=AssertionError("database touched before validation"))
    monkeypatch.setattr(main, "engine", Mock(begin=begin))
    worker_task = Mock(side_effect=AssertionError("worker started before validation"))
    monkeypatch.setattr(asyncio, "create_task", worker_task)
    monkeypatch.setenv("SKIP_CREATE_ALL", "false")
    monkeypatch.setenv("BECKN_WORKERS_ENABLED", "true")

    async def run():
        with pytest.raises(ValueError):
            async with main.app.router.lifespan_context(main.app):
                pytest.fail("invalid startup reached serving state")
    asyncio.run(run())
    begin.assert_not_called()
    worker_task.assert_not_called()


@pytest.mark.parametrize("mode", ["development", "test", "staging", "production"])
def test_valid_lifespan_controls(monkeypatch, mode):
    main = importlib.import_module("main")
    monkeypatch.setattr(main, "settings", settings(**{**PRODUCTION, "environment": mode}))
    dispose = AsyncMock()
    monkeypatch.setattr(main, "engine", Mock(
        dispose=dispose, begin=Mock(side_effect=AssertionError("unexpected database"))))

    async def run():
        async with main.app.router.lifespan_context(main.app):
            assert main.app.title == "Beli Aman BAP"
    asyncio.run(run())
    dispose.assert_awaited_once()


def test_docker_runtime_owns_production_marker():
    dockerfile = (BAP_DIR / "Dockerfile").read_text()
    runtime = dockerfile.split("AS runtime", 1)[1]
    assert "BAP_PRODUCTION_DEPLOYMENT=true" in runtime
    assert "ENVIRONMENT=production" in runtime
