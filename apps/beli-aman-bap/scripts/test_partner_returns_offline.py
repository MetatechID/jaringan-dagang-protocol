"""Run partner return regressions with no dotenv, inherited config or network."""
from pathlib import Path
import os
import socket
import sys
import tempfile


def main():
    bap = Path(__file__).resolve().parents[1]
    os.environ.clear()
    os.environ.update(ENVIRONMENT="test", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    from pydantic_settings import BaseSettings
    original = BaseSettings.__init__

    def isolated(self, *args, **kwargs):
        kwargs["_env_file"] = None
        original(self, *args, **kwargs)

    BaseSettings.__init__ = isolated

    def denied(*args, **kwargs):
        raise AssertionError("Unexpected network in partner return tests")

    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.getaddrinfo = denied
    import pytest
    sys.path.insert(0, str(bap))
    with tempfile.TemporaryDirectory(prefix="partner-returns-") as directory:
        os.chdir(directory)
        return pytest.main([
            "-q", "-p", "pytest_asyncio.plugin", "--asyncio-mode=auto",
            "-o", "asyncio_default_fixture_loop_scope=function",
            "--confcutdir", str(bap / "tests"),
            str(bap / "tests/test_partner_returns.py"),
            str(bap / "tests/test_partner_integration.py"),
        ])


if __name__ == "__main__":
    raise SystemExit(main())
