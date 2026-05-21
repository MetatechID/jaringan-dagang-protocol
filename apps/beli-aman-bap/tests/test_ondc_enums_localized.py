"""Task A2 — the localized ONDC RET11 enum file loads and is complete.

Source of truth: the network ONDC localization layer at
``jaringan-dagang-network/network-extension/enums/retail.yaml``. It
localizes the canonical ONDC RET v1.2.x / 2.0.2 protocol enums (order
state, fulfillment state, cancel/return reason codes, item statutory /
packaged-commodity attribute keys) for the packaged-F&B sub-domain
ONDC:RET11 that Safiya transacts on.

These tests assert the file parses and contains the protocol codes the
RET11 search / select / confirm flow needs -- they do NOT re-assert the
whole upstream set (localization, not expansion).
"""

import os

import pytest
import yaml

# The network repo is a sibling checkout, not a dependency. Buyer tests
# live at apps/beli-aman-bap/tests/, so walk up to ~/Code/ (four levels)
# then into the sibling network repo. Hermetic on the standard layout.
_ENUM_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "jaringan-dagang-network",
        "network-extension",
        "enums",
        "retail.yaml",
    )
)


@pytest.fixture(scope="module")
def enums() -> dict:
    if not os.path.exists(_ENUM_PATH):
        pytest.skip(f"network-extension enums not checked out at {_ENUM_PATH}")
    with open(_ENUM_PATH, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict)
    return data


class TestFileShape:
    def test_targets_ret11_packaged_fnb(self, enums):
        assert enums["domain"] == "ONDC:RET"
        assert enums["sub_domain"] == "ONDC:RET11"
        assert enums["beckn_domain"] == "nic2004:52110"

    def test_every_localized_entry_has_bahasa_label(self, enums):
        for section in (
            "order_states",
            "fulfillment_states",
            "fulfillment_types",
            "payment_types",
            "cancellation_reasons",
            "return_reasons",
        ):
            for row in enums[section]:
                assert row.get("name"), f"{section}: missing name"
                assert row.get("name_id"), f"{section} {row.get('code')}: missing name_id"


class TestOrderAndFulfillmentStates:
    def test_order_states_match_beckn_core(self, enums):
        codes = {r["code"] for r in enums["order_states"]}
        assert codes == {
            "Created",
            "Accepted",
            "In-progress",
            "Completed",
            "Cancelled",
        }
        # ONDC RET reuses Beckn core order states verbatim.
        for r in enums["order_states"]:
            assert r["beckn_state"] == r["code"]

    def test_fulfillment_states_cover_ret_lifecycle(self, enums):
        codes = {r["code"] for r in enums["fulfillment_states"]}
        # The RET seller-fulfilled lifecycle codes (from ONDC RET 2.0.2
        # fulfillments.yaml state.descriptor).
        assert {
            "Pending",
            "Packed",
            "Agent-assigned",
            "Searching-for-agent",
            "Order-picked-up",
            "Out-for-delivery",
            "Order-delivered",
            "Cancelled",
        } == codes

    def test_fulfillment_types(self, enums):
        codes = {r["code"] for r in enums["fulfillment_types"]}
        assert codes == {"Delivery", "Self-Pickup"}


class TestReasonCodes:
    def test_cancellation_reason_codes_are_zero_padded_strings(self, enums):
        for r in enums["cancellation_reasons"]:
            assert isinstance(r["code"], str), r
            assert r["code"].isdigit()
        codes = {r["code"] for r in enums["cancellation_reasons"]}
        # Well-known anchors from the ONDC RET cancellation set.
        assert "002" in codes  # items not available (SNP, part-cancel)
        assert "005" in codes  # merchant rejected
        assert "012" in codes  # buyer doesn't want product (part-cancel)

    def test_part_cancel_and_rto_flags_preserved(self, enums):
        by_code = {r["code"]: r for r in enums["cancellation_reasons"]}
        assert by_code["002"]["part_cancel"] is True
        assert by_code["002"]["triggers_rto"] is False
        assert by_code["003"]["triggers_rto"] is True

    def test_return_reason_codes_present(self, enums):
        codes = {r["code"] for r in enums["return_reasons"]}
        assert {"001", "003", "004"}.issubset(codes)


class TestItemStatutoryAttributes:
    def test_packaged_commodities_group(self, enums):
        grp = enums["item_statutory_attributes"][
            "statutory_reqs_packaged_commodities"
        ]
        assert grp["group_code"] == "@ondc/org/statutory_reqs_packaged_commodities"
        keys = {a["key"] for a in grp["attributes"]}
        assert {
            "manufacturer_or_packer_name",
            "manufacturer_or_packer_address",
            "common_or_generic_name_of_commodity",
            "net_quantity_or_measure_of_commodity_in_pkg",
            "month_year_of_manufacture_packing_import",
        } == keys
        # Indonesia localization is present (netto / berat bersih).
        netq = next(
            a
            for a in grp["attributes"]
            if a["key"] == "net_quantity_or_measure_of_commodity_in_pkg"
        )
        assert "Netto" in netq["name_id"] or "Berat Bersih" in netq["name_id"]

    def test_prepackaged_food_group_localizes_to_bpom(self, enums):
        grp = enums["item_statutory_attributes"][
            "statutory_reqs_prepackaged_food"
        ]
        assert grp["group_code"] == "@ondc/org/statutory_reqs_prepackaged_food"
        keys = {a["key"] for a in grp["attributes"]}
        assert {
            "nutritional_info",
            "additives_info",
            "brand_owner_FSSAI_license_no",
            "other_FSSAI_license_no",
            "importer_FSSAI_license_no",
        } == keys
        brand = next(
            a
            for a in grp["attributes"]
            if a["key"] == "brand_owner_FSSAI_license_no"
        )
        assert "BPOM" in brand["name_id"]

    def test_commerce_attributes_cover_ret11_search_fields(self, enums):
        keys = {a["key"] for a in enums["item_commerce_attributes"]}
        assert {
            "returnable",
            "cancellable",
            "return_window",
            "seller_pickup_return",
            "time_to_ship",
            "available_on_cod",
            "contact_details_consumer_care",
        } == keys
