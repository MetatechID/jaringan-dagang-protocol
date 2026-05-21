"""Task A4 — ``CATALOG_SOURCE`` env flag selects between JSON and mirror.

Three modes:
  - ``json`` (default): JSON fixtures only.
  - ``mirror``: mirror_* only, no fallback. Empty mirror -> empty result.
  - ``mirror-with-fallback`` (or ``mirror-with-json-fallback``): mirror
    first, JSON fallback if mirror has nothing.

Resolution is done at every call (not module-level) so a flip via env
takes effect without restart. We test the resolver + the list_products
dispatch by monkeypatching the mirror reader.
"""

from __future__ import annotations

import asyncio
import os
import sys

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

from services import catalog as catalog_service  # noqa: E402


class TestSourceResolver:
    def test_default_is_json(self, monkeypatch):
        monkeypatch.delenv("CATALOG_SOURCE", raising=False)
        assert catalog_service._catalog_source() == "json"

    def test_json_explicit(self, monkeypatch):
        monkeypatch.setenv("CATALOG_SOURCE", "json")
        assert catalog_service._catalog_source() == "json"

    def test_mirror(self, monkeypatch):
        monkeypatch.setenv("CATALOG_SOURCE", "mirror")
        assert catalog_service._catalog_source() == "mirror"

    def test_mirror_with_fallback(self, monkeypatch):
        monkeypatch.setenv("CATALOG_SOURCE", "mirror-with-fallback")
        assert catalog_service._catalog_source() == "mirror-with-fallback"

    def test_mirror_with_json_fallback_synonym(self, monkeypatch):
        """The spec § 5.3 spelling ``mirror-with-json-fallback`` resolves to
        the same internal value as ``mirror-with-fallback``."""
        monkeypatch.setenv("CATALOG_SOURCE", "mirror-with-json-fallback")
        assert catalog_service._catalog_source() == "mirror-with-fallback"

    def test_invalid_value_falls_back_to_json(self, monkeypatch):
        monkeypatch.setenv("CATALOG_SOURCE", "bogus-source")
        assert catalog_service._catalog_source() == "json"

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("CATALOG_SOURCE", "MIRROR")
        assert catalog_service._catalog_source() == "mirror"


class TestListDispatch:
    def test_json_mode_does_not_query_mirror(self, monkeypatch):
        monkeypatch.setenv("CATALOG_SOURCE", "json")
        called = {"n": 0}

        async def _fake_mirror_read(_slug):
            called["n"] += 1
            return [{"name": "from-mirror"}]

        monkeypatch.setattr(catalog_service, "_list_products_from_mirror", _fake_mirror_read)
        # Force load_fallback to a known shape so we don't depend on real JSONs.
        monkeypatch.setattr(catalog_service, "_load_fallback", lambda _s: {"products": [{"name": "from-json"}]})

        result = asyncio.run(catalog_service.list_products("safiyafood"))
        assert called["n"] == 0
        assert result == [{"name": "from-json"}]

    def test_mirror_mode_returns_mirror_only(self, monkeypatch):
        monkeypatch.setenv("CATALOG_SOURCE", "mirror")

        async def _fake_mirror_read(_slug):
            return [{"name": "from-mirror"}]

        monkeypatch.setattr(catalog_service, "_list_products_from_mirror", _fake_mirror_read)
        # JSON fallback MUST NOT be consulted in mirror mode.
        monkeypatch.setattr(catalog_service, "_load_fallback", lambda _s: {"products": [{"name": "from-json"}]})

        result = asyncio.run(catalog_service.list_products("safiyafood"))
        assert result == [{"name": "from-mirror"}]

    def test_mirror_mode_empty_returns_empty_not_json(self, monkeypatch):
        monkeypatch.setenv("CATALOG_SOURCE", "mirror")

        async def _empty_mirror(_slug):
            return []

        monkeypatch.setattr(catalog_service, "_list_products_from_mirror", _empty_mirror)
        monkeypatch.setattr(catalog_service, "_load_fallback", lambda _s: {"products": [{"name": "from-json"}]})

        result = asyncio.run(catalog_service.list_products("safiyafood"))
        assert result == []

    def test_fallback_mode_uses_json_when_mirror_empty(self, monkeypatch):
        monkeypatch.setenv("CATALOG_SOURCE", "mirror-with-fallback")

        async def _empty_mirror(_slug):
            return []

        monkeypatch.setattr(catalog_service, "_list_products_from_mirror", _empty_mirror)
        monkeypatch.setattr(catalog_service, "_load_fallback", lambda _s: {"products": [{"name": "from-json"}]})

        result = asyncio.run(catalog_service.list_products("safiyafood"))
        assert result == [{"name": "from-json"}]

    def test_fallback_mode_skips_json_when_mirror_has_results(self, monkeypatch):
        monkeypatch.setenv("CATALOG_SOURCE", "mirror-with-fallback")

        async def _full_mirror(_slug):
            return [{"name": "from-mirror"}]

        monkeypatch.setattr(catalog_service, "_list_products_from_mirror", _full_mirror)
        monkeypatch.setattr(catalog_service, "_load_fallback", lambda _s: {"products": [{"name": "from-json"}]})

        result = asyncio.run(catalog_service.list_products("safiyafood"))
        assert result == [{"name": "from-mirror"}]
