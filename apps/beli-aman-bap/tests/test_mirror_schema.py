"""Task A4 — MirrorStore schema parity check.

Confirms the buyer's mirror_stores table carries the same columns the
spec § 5.1 lists plus the new ``image_base_url`` we add in A4 (parallels
seller's ``Store.image_base_url`` from A7). This is a static introspection
test — no DB connection needed.
"""

from __future__ import annotations

import os
import sys

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

from models.mirror import (  # noqa: E402
    MirrorProduct,
    MirrorProductImage,
    MirrorSKU,
    MirrorSKUImage,
    MirrorStore,
)


def _cols(model) -> set[str]:
    return {c.name for c in model.__table__.columns}


class TestMirrorStoreSchema:
    def test_has_canonical_columns_from_spec(self):
        expected = {
            "id",
            "bpp_id",
            "slug",
            "name",
            "logo_url",
            "domain",
            "city",
            "bpp_uri",
            "last_pushed_at",
            "last_pulled_at",
            "catalog_version",
            "created_at",
        }
        cols = _cols(MirrorStore)
        missing = expected - cols
        assert not missing, f"MirrorStore missing columns: {missing}"

    def test_has_image_base_url_column(self):
        """A4-added column parallels seller's Store.image_base_url (A7).

        The buyer keeps absolute URLs in mirror_*_images today; this column
        is reserved for future cases where the BAP re-derives a different
        CDN origin without re-pulling the catalog (e.g. CDN swap, image
        proxy rewrite).
        """
        cols = _cols(MirrorStore)
        assert "image_base_url" in cols


class TestMirrorChildSchemas:
    def test_mirror_product_columns(self):
        expected = {
            "id", "store_id", "bpp_product_id", "sku", "name",
            "description", "status", "attributes", "last_synced_at",
        }
        assert expected <= _cols(MirrorProduct)

    def test_mirror_sku_columns(self):
        expected = {
            "id", "product_id", "bpp_sku_id", "variant_name", "variant_value",
            "sku_code", "price", "original_price", "stock", "weight_grams",
            "last_synced_at",
        }
        assert expected <= _cols(MirrorSKU)

    def test_mirror_product_image_columns(self):
        expected = {"id", "product_id", "url", "position", "is_primary"}
        assert expected <= _cols(MirrorProductImage)

    def test_mirror_sku_image_columns(self):
        expected = {"id", "sku_id", "url", "position", "is_primary"}
        assert expected <= _cols(MirrorSKUImage)


class TestOrderFulfillmentColumns:
    def test_order_has_fulfillment_columns(self):
        """Spec § 7.4 lists Order.fulfillment_status, .tracking_url,
        .fulfillment_last_event_at — scaffolded today via /on_status (A2b)
        but called out by A4 so we keep the columns checked."""
        from models.order import Order

        cols = {c.name for c in Order.__table__.columns}
        assert "fulfillment_status" in cols
        assert "fulfillment_tracking_url" in cols
        assert "fulfillment_last_event_at" in cols
