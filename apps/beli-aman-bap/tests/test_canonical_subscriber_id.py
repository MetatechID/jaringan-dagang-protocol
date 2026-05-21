"""Task A3 — buyer BAP runs on the canonical ``*.jaringan-dagang.id``
subscriber_id scheme.

Verifies:
  * ``settings.subscriber_id`` matches the canonical regex
  * Default is the production-shaped ``beli-aman.bap.jaringan-dagang.id``
  * No legacy ``bap.beli-aman.local`` / ``*.metatech.id`` identifier
    remains as the BAP's own identity
"""

from __future__ import annotations

import os
import re
import sys

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)


# Canonical scheme:
#   - BAP:        beli-aman.bap.jaringan-dagang.id
#   - BPP toko:   <slug>.jaringan-dagang.id
#   - BPP fallback: bpp.jaringan-dagang.id
#   - Gateway:    gateway.jaringan-dagang.id
#   - Registry:   registry.jaringan-dagang.id
CANONICAL_SUBSCRIBER_ID_RE = re.compile(
    r"^(?:"
    r"beli-aman\.bap\.jaringan-dagang\.id"  # BAP
    r"|bpp\.jaringan-dagang\.id"  # BPP fallback
    r"|gateway\.jaringan-dagang\.id"  # gateway
    r"|registry\.jaringan-dagang\.id"  # registry
    r"|[a-z][a-z0-9-]*\.jaringan-dagang\.id"  # per-toko BPP slug
    r")$"
)


def test_settings_subscriber_id_is_canonical():
    """BAP's own subscriber_id must match the canonical scheme."""
    from config import settings

    assert CANONICAL_SUBSCRIBER_ID_RE.match(settings.subscriber_id), (
        f"BAP subscriber_id {settings.subscriber_id!r} is NOT canonical. "
        "Expected something like 'beli-aman.bap.jaringan-dagang.id'."
    )


def test_settings_subscriber_id_default_is_beli_aman_bap_canonical():
    """Default (no env override) must be the canonical BAP id."""
    from config import Settings

    # Construct a fresh Settings ignoring .env file pickup is not trivial
    # with pydantic-settings; instead assert the *class* default attribute
    # at the schema level matches canonical.
    default_val = Settings.model_fields["subscriber_id"].default
    assert default_val == "beli-aman.bap.jaringan-dagang.id", (
        f"BAP subscriber_id schema default is {default_val!r}; "
        "expected 'beli-aman.bap.jaringan-dagang.id'."
    )


def test_no_legacy_bap_beli_aman_local_in_default():
    """The legacy 'bap.beli-aman.local' string MUST NOT be the schema
    default for subscriber_id (it was the pre-A3 value)."""
    from config import Settings

    default_val = Settings.model_fields["subscriber_id"].default
    assert "bap.beli-aman.local" not in str(default_val), (
        "Legacy 'bap.beli-aman.local' must not be the schema default."
    )


def test_no_metatech_id_in_default():
    """Legacy '.metatech.id' must not be in the schema default."""
    from config import Settings

    default_val = Settings.model_fields["subscriber_id"].default
    assert "metatech.id" not in str(default_val), (
        "Legacy '.metatech.id' must not be the schema default."
    )


def test_seller_bridge_url_default_unaffected():
    """Sanity: bridge_url still defaults to the local seller port."""
    from config import Settings

    default_val = Settings.model_fields["seller_bridge_url"].default
    assert default_val.startswith("http"), (
        f"seller_bridge_url default should be a URL, got {default_val!r}"
    )
