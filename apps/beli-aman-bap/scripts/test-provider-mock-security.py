"""Run focused payment security tests with pre-import, offline isolation."""
from pathlib import Path
import os
import socket
import sys

# Do not inherit application/provider settings or read a developer's .env.
keep = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "LANG", "TMPDIR"}}
os.environ.clear()
os.environ.update(keep)
os.environ.update({
    "ENVIRONMENT": "test",
    "DATABASE_URL": "postgresql+asyncpg://test:test@127.0.0.1:1/offline",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
})
import dotenv
import pydantic_settings.sources.providers.dotenv as settings_dotenv

dotenv.load_dotenv = lambda *args, **kwargs: False
dotenv.dotenv_values = lambda *args, **kwargs: {}
settings_dotenv.dotenv_values = lambda *args, **kwargs: {}
settings_dotenv.DotEnvSettingsSource._read_env_file = lambda *args, **kwargs: {}


def deny_network(*args, **kwargs):
    raise AssertionError("Unexpected network access in offline security tests")


socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
socket.create_connection = deny_network
socket.getaddrinfo = deny_network
socket.socket.sendto = deny_network

app = Path(__file__).resolve().parents[1]
os.chdir(app)
sys.path.insert(0, str(app))
import pytest

raise SystemExit(pytest.main([
    "-p", "pytest_asyncio.plugin", "-p", "no:cacheprovider", "--asyncio-mode=auto",
    "-q", *(sys.argv[1:] or ["tests/test_provider_mock_security.py"]),
]))
