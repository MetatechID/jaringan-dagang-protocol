"""Sento settings + Brand columns — structural tests.

Verifies the Sento integration has the same configuration surface as OY:
- ``Settings`` carries ``sento_api_key`` / ``sento_default_username`` /
  ``sento_callback_base_url`` / ``sento_invoice_duration_seconds``.
- ``Brand`` ORM model carries ``sento_api_key`` / ``sento_username`` /
  ``sento_callback_secret`` mapped columns.

These are pure import / attribute tests — no DB, no network.
"""

from __future__ import annotations

import os
import sys

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)


def test_settings_carries_sento_block():
    """``Settings`` exposes the four sento_* fields with safe defaults."""
    from config import Settings  # noqa: WPS433

    s = Settings()
    assert hasattr(s, "sento_api_key"), "missing sento_api_key"
    assert hasattr(s, "sento_default_username"), "missing sento_default_username"
    assert hasattr(s, "sento_callback_base_url"), "missing sento_callback_base_url"
    assert hasattr(s, "sento_invoice_duration_seconds"), "missing sento_invoice_duration_seconds"
    # Defaults — empty credentials, production base URL, 24h duration.
    assert s.sento_api_key == ""
    assert s.sento_default_username == ""
    assert s.sento_callback_base_url.startswith("http")
    assert s.sento_invoice_duration_seconds == 86400


def test_brand_model_has_sento_columns():
    """``Brand`` model exposes sento_* mapped columns matching the OY shape."""
    from models.brand import Brand  # noqa: WPS433

    columns = {c.name for c in Brand.__table__.columns}
    assert "sento_api_key" in columns
    assert "sento_username" in columns
    assert "sento_callback_secret" in columns