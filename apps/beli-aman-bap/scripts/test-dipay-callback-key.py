"""Run Dipay regressions without inherited app config, dotenv, plugins or network."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import sys

APP = Path(__file__).resolve().parents[1]
# Keep only process necessities, never ambient application credentials/config.
keep = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT"}
for name in list(os.environ):
    if name not in keep:
        del os.environ[name]
os.environ.update(
    PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    ENVIRONMENT="test",
    BECKN_WORKERS_ENABLED="false",
    SKIP_CREATE_ALL="true",
)

from pydantic_settings.sources import DotEnvSettingsSource

DotEnvSettingsSource._read_env_files = lambda self: {}


def no_network(*args, **kwargs):
    raise AssertionError("Real network is forbidden in Dipay regression tests")


socket.socket.connect = no_network
socket.socket.connect_ex = no_network
socket.create_connection = no_network
socket.getaddrinfo = no_network
sys.path.insert(0, str(APP))

import importlib.metadata
import pytest

# Require the installed canonical distribution, not a source-tree substitute.
dist = importlib.metadata.distribution("jaringan-dagang-beckn")
assert any(str(p).endswith(".dist-info/WHEEL") for p in dist.files or ())
raise SystemExit(pytest.main([
    "-p", "pytest_asyncio.plugin", "-q", "--import-mode=importlib",
    *[str(APP / "tests" / name) for name in (sys.argv[1:] or ["test_dipay_callback_key.py"])],
]))
