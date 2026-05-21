"""Task A2 — ONDC tag builders produce exact ONDC tag structures.

The builders live in the shared beckn-protocol package, vendored
byte-identically into the buyer repo at
``packages/beckn-protocol/python/ondc_tags.py`` (kept in lockstep with
the seller copy). They produce the Beckn
``tags: [{code, list:[{code, value}]}]`` shape with ONDC ``@ondc/org/...``
namespaced group codes, grounded in the ONDC RET v1.2.x / 2.0.2 spec.

This buyer-side copy of the suite exercises the buyer's vendored package
to prove cross-repo parity. Only RET11 search/select/confirm tags are
covered (YAGNI): item statutory/packaged tags, fulfillment delivery
tags, payment settlement-terms tags.
"""

import os
import sys

import pytest

# Buyer tests live at apps/beli-aman-bap/tests/; the vendored package is
# at the buyer repo root (three levels up).
_PROTO = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "packages", "beckn-protocol"
    )
)
if _PROTO not in sys.path:
    sys.path.insert(0, _PROTO)

from python import Tag  # noqa: E402
from python.ondc_tags import (  # noqa: E402
    ONDC_ORG_STATUTORY_PACKAGED_COMMODITIES,
    ONDC_ORG_STATUTORY_PREPACKAGED_FOOD,
    build_fulfillment_ondc_tags,
    build_item_statutory_tags,
    build_payment_settlement_tags,
)


def _as_dict(tags):
    """Normalize a list[Tag] | list[dict] to plain dicts for assertions."""
    out = []
    for t in tags:
        out.append(t.model_dump(exclude_none=True) if isinstance(t, Tag) else t)
    return out


class TestItemStatutoryTags:
    def test_packaged_commodities_exact_structure(self):
        tags = build_item_statutory_tags(
            packaged_commodities={
                "manufacturer_or_packer_name": "PT Safiya Pangan",
                "manufacturer_or_packer_address": "Jl. Mawar No. 1, Jakarta",
                "common_or_generic_name_of_commodity": "Keripik Singkong",
                "net_quantity_or_measure_of_commodity_in_pkg": "200 g",
                "month_year_of_manufacture_packing_import": "03/2026",
            }
        )
        dicts = _as_dict(tags)
        assert dicts == [
            {
                "code": ONDC_ORG_STATUTORY_PACKAGED_COMMODITIES,
                "list": [
                    {"code": "manufacturer_or_packer_name", "value": "PT Safiya Pangan"},
                    {
                        "code": "manufacturer_or_packer_address",
                        "value": "Jl. Mawar No. 1, Jakarta",
                    },
                    {
                        "code": "common_or_generic_name_of_commodity",
                        "value": "Keripik Singkong",
                    },
                    {
                        "code": "net_quantity_or_measure_of_commodity_in_pkg",
                        "value": "200 g",
                    },
                    {
                        "code": "month_year_of_manufacture_packing_import",
                        "value": "03/2026",
                    },
                ],
            }
        ]

    def test_returns_typed_tag_models(self):
        tags = build_item_statutory_tags(
            packaged_commodities={"manufacturer_or_packer_name": "X"}
        )
        assert all(isinstance(t, Tag) for t in tags)
        assert tags[0].code == ONDC_ORG_STATUTORY_PACKAGED_COMMODITIES
        assert tags[0].list[0].value == "X"

    def test_prepackaged_food_group(self):
        # Input order is intentionally reversed vs the ONDC key order to
        # prove the builder emits a *deterministic, spec-ordered* list
        # (nutritional_info precedes brand_owner_FSSAI_license_no).
        tags = build_item_statutory_tags(
            prepackaged_food={
                "brand_owner_FSSAI_license_no": "BPOM-MD-123456",
                "nutritional_info": "Energi 500kkal per 100g",
            }
        )
        dicts = _as_dict(tags)
        assert dicts == [
            {
                "code": ONDC_ORG_STATUTORY_PREPACKAGED_FOOD,
                "list": [
                    {"code": "nutritional_info", "value": "Energi 500kkal per 100g"},
                    {"code": "brand_owner_FSSAI_license_no", "value": "BPOM-MD-123456"},
                ],
            }
        ]

    def test_both_groups_emitted_when_both_supplied(self):
        tags = build_item_statutory_tags(
            packaged_commodities={"manufacturer_or_packer_name": "A"},
            prepackaged_food={"additives_info": "Tanpa pengawet"},
        )
        codes = [t.code for t in tags]
        assert codes == [
            ONDC_ORG_STATUTORY_PACKAGED_COMMODITIES,
            ONDC_ORG_STATUTORY_PREPACKAGED_FOOD,
        ]

    def test_unknown_subkey_rejected(self):
        # Builders must not silently emit non-ONDC sub-keys.
        with pytest.raises(ValueError):
            build_item_statutory_tags(packaged_commodities={"not_a_real_key": "x"})

    def test_empty_input_yields_no_tags(self):
        assert build_item_statutory_tags() == []
        assert build_item_statutory_tags(packaged_commodities={}) == []

    def test_none_values_skipped(self):
        tags = build_item_statutory_tags(
            packaged_commodities={
                "manufacturer_or_packer_name": "A",
                "manufacturer_or_packer_address": None,
            }
        )
        assert [v.code for v in tags[0].list] == ["manufacturer_or_packer_name"]


class TestFulfillmentTags:
    def test_delivery_terms_incoterms(self):
        tags = build_fulfillment_ondc_tags(incoterms="DAP", named_place="Jakarta")
        dicts = _as_dict(tags)
        assert dicts == [
            {
                "code": "delivery_terms",
                "list": [
                    {"code": "incoterms", "value": "DAP"},
                    {"code": "named_place_of_delivery", "value": "Jakarta"},
                ],
            }
        ]

    def test_invalid_incoterm_rejected(self):
        with pytest.raises(ValueError):
            build_fulfillment_ondc_tags(incoterms="ZZZ")

    def test_empty_yields_no_tags(self):
        assert build_fulfillment_ondc_tags() == []


class TestPaymentSettlementTags:
    def test_settlement_terms_exact_structure(self):
        tags = build_payment_settlement_tags(
            settlement_basis="delivery",
            settlement_window="P1D",
            withholding_amount="10.00",
            buyer_app_finder_fee_type="percent",
            buyer_app_finder_fee_amount="3",
        )
        dicts = _as_dict(tags)
        assert dicts == [
            {
                "code": "settlement_terms",
                "list": [
                    {"code": "settlement_basis", "value": "delivery"},
                    {"code": "settlement_window", "value": "P1D"},
                    {"code": "withholding_amount", "value": "10.00"},
                    {"code": "buyer_app_finder_fee_type", "value": "percent"},
                    {"code": "buyer_app_finder_fee_amount", "value": "3"},
                ],
            }
        ]

    def test_invalid_settlement_basis_rejected(self):
        with pytest.raises(ValueError):
            build_payment_settlement_tags(settlement_basis="someday")

    def test_partial_terms(self):
        tags = build_payment_settlement_tags(settlement_basis="delivery")
        assert _as_dict(tags) == [
            {
                "code": "settlement_terms",
                "list": [{"code": "settlement_basis", "value": "delivery"}],
            }
        ]

    def test_empty_yields_no_tags(self):
        assert build_payment_settlement_tags() == []
