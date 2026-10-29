"""Run signer/resource authorization tests without inherited secrets or network."""
from pathlib import Path
import os
import socket
import sys

keep = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "TMPDIR"}}
os.environ.clear()
os.environ.update(keep)
os.environ.update({
    "ENVIRONMENT": "test", "BECKN_REQUIRE_SIGNATURE": "true",
    "DATABASE_URL": "postgresql+asyncpg://test:test@127.0.0.1:1/offline",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
})
import dotenv
import pydantic_settings.sources.providers.dotenv as settings_dotenv

dotenv.load_dotenv = lambda *a, **kw: False
dotenv.dotenv_values = lambda *a, **kw: {}
settings_dotenv.dotenv_values = lambda *a, **kw: {}
settings_dotenv.DotEnvSettingsSource._read_env_file = lambda *a, **kw: {}


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
    "-p", "pytest_asyncio.plugin", "-p", "no:cacheprovider", "--asyncio-mode=auto", "-o", "asyncio_default_fixture_loop_scope=function", "-q",
    *(sys.argv[1:] or ["tests/test_beckn_ownership_security.py"]),
]))
