"""Run the narrow Sento suite without dotenv, inherited credentials or network.

Use the BAP Python 3.12 virtualenv, with the canonical protocol wheel installed.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import sys
import tempfile


def main() -> int:
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Use Python 3.12")
    bap = Path(__file__).resolve().parents[1]
    os.environ.clear()
    os.environ.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", ENVIRONMENT="test")

    def denied(*args, **kwargs):
        raise AssertionError("Network access is forbidden in the offline Sento suite")

    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.getaddrinfo = denied
    # Disable dotenv explicitly, before application imports (not just via cwd).
    from pydantic_settings import BaseSettings
    original_init = BaseSettings.__init__

    def isolated_init(self, *args, **kwargs):
        kwargs["_env_file"] = None
        original_init(self, *args, **kwargs)

    BaseSettings.__init__ = isolated_init
    import jaringan_dagang_beckn
    if "site-packages" not in str(Path(jaringan_dagang_beckn.__file__).resolve()):
        raise SystemExit("Install the canonical beckn-protocol-py wheel, not a source shim")
    import pytest

    sys.path.insert(0, str(bap))
    with tempfile.TemporaryDirectory(prefix="sento-offline-") as directory:
        os.chdir(directory)
        return pytest.main([
            "-p", "pytest_asyncio.plugin", "-q",
            "-o", "asyncio_default_fixture_loop_scope=function",
            "--confcutdir", str(bap / "tests"),
            str(bap / "tests/test_sento_client.py"),
            str(bap / "tests/test_sento_invoices.py"),
            str(bap / "tests/test_webhooks_sento.py"),
        ])


if __name__ == "__main__":
    raise SystemExit(main())
