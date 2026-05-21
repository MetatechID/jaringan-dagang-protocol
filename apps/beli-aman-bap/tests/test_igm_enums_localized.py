"""Task A5 — localized ONDC IGM enum file loads + contains the v1 codes.

Source of truth: the network ONDC localization layer at
``jaringan-dagang-network/network-extension/enums/igm.yaml`` (added in A5).
This file localizes the ONDC IGM v1 enums to Bahasa for the
refund-request Issue path the buyer BAP raises against the seller BPP.

YAGNI-narrow: we only assert the codes the v1 path actually uses (ITEM
category + ITM01..ITM05) plus the action vocab and the IGM error code
range.
"""

import os

import pytest
import yaml

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
        "igm.yaml",
    )
)


@pytest.fixture(scope="module")
def igm() -> dict:
    if not os.path.exists(_ENUM_PATH):
        pytest.skip(f"network-extension IGM enums not checked out at {_ENUM_PATH}")
    with open(_ENUM_PATH, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict)
    return data


class TestFileShape:
    def test_targets_ret11_packaged_fnb(self, igm):
        assert igm["domain"] == "ONDC:RET"
        assert igm["sub_domain"] == "ONDC:RET11"

    def test_every_section_has_bahasa_labels(self, igm):
        for section in (
            "issue_category",
            "issue_sub_category",
            "respondent_action",
            "complainant_action",
            "igm_error_codes",
        ):
            for row in igm[section]:
                assert row.get("name"), f"{section}: missing name"
                assert row.get(
                    "name_id"
                ), f"{section} {row.get('code')}: missing name_id"


class TestIssueCategoryCodes:
    def test_v1_categories_present(self, igm):
        codes = {r["code"] for r in igm["issue_category"]}
        assert {"ITEM", "ORDER", "FULFILLMENT", "AGENT", "PAYMENT", "PAYMENT-FNB"} <= codes


class TestSubCategoryCodes:
    def test_v1_item_sub_categories_present(self, igm):
        codes = {r["code"] for r in igm["issue_sub_category"]}
        # ITM01..ITM05 — the refund-relevant Item sub-categories.
        assert {"ITM01", "ITM02", "ITM03", "ITM04", "ITM05"} <= codes

    def test_each_item_subcat_carries_category_link(self, igm):
        for row in igm["issue_sub_category"]:
            if row["code"].startswith("ITM"):
                assert row.get("category") == "ITEM", (
                    f"{row['code']} must declare category=ITEM"
                )


class TestActions:
    def test_respondent_actions_cover_v1_states(self, igm):
        codes = {r["code"] for r in igm["respondent_action"]}
        assert {"PROCESSING", "RESOLVED", "REJECTED", "ESCALATE"} <= codes

    def test_complainant_actions_cover_v1_states(self, igm):
        codes = {r["code"] for r in igm["complainant_action"]}
        assert {"OPEN", "CLOSE", "ESCALATE"} <= codes


class TestErrorCodes:
    def test_igm_error_range_is_present(self, igm):
        codes = {r["code"] for r in igm["igm_error_codes"]}
        # 90001..90005 — the codes the v1 /issue + /on_issue path emits.
        assert {"90001", "90002", "90003", "90004", "90005"} <= codes


class TestBecknProtocolMirror:
    """The beckn-protocol package must accept the same set of codes."""

    def test_protocol_categories_match_yaml(self, igm):
        import sys

        proto = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__), "..", "..", "..", "packages", "beckn-protocol"
            )
        )
        if proto not in sys.path:
            sys.path.insert(0, proto)
        from python import ISSUE_CATEGORIES, ISSUE_SUB_CATEGORIES_ITEM

        yaml_cats = {r["code"] for r in igm["issue_category"]}
        # The protocol set must be the same as the YAML set (no drift).
        assert ISSUE_CATEGORIES == yaml_cats
        yaml_subs = {
            r["code"] for r in igm["issue_sub_category"]
            if r.get("category") == "ITEM"
        }
        assert ISSUE_SUB_CATEGORIES_ITEM == yaml_subs
