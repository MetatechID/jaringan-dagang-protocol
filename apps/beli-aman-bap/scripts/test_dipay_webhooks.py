"""Run only offline Dipay webhook regressions with pre-import isolation."""

import os
from pathlib import Path
import socket
import sys
import tempfile
from unittest.mock import patch


BAP = Path(__file__).resolve().parents[1]


def deny_network(*args, **kwargs):
    raise AssertionError("Unexpected outbound I/O in Dipay webhook tests")


if __name__ == "__main__":
    os.environ.clear()
    os.environ.update({
        "ENVIRONMENT": "test", "DEBUG": "false",
        "DATABASE_URL": "postgresql+asyncpg://test:test@127.0.0.1:1/test",
        "BECKN_WORKERS_ENABLED": "false", "SKIP_CREATE_ALL": "true",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    })
    sys.path.insert(0, str(BAP))
    with tempfile.TemporaryDirectory(prefix="dipay-webhook-tests-") as directory:
        os.chdir(directory)  # Never read the application's real .env.
        with patch.object(socket.socket, "connect", deny_network), \
                patch.object(socket.socket, "connect_ex", deny_network), \
                patch.object(socket, "getaddrinfo", deny_network):
            import pytest
            raise SystemExit(pytest.main([
                "-p", "no:cacheprovider", "-q",
                str(BAP / "tests/test_webhooks_dipay.py"), *sys.argv[1:],
            ]))
