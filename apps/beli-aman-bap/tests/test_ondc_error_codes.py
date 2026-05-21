"""Task A2 — ONDC retail error-code catalogue + BecknError helper.

The catalogue + helper live in the shared beckn-protocol package,
vendored byte-identically into the buyer repo at
``packages/beckn-protocol/python/errors.py`` (kept in lockstep with the
seller copy). The numeric codes, their type bucket (Gateway / Buyer App
/ Seller App) and default messages are the canonical ONDC RET v2.0.2
ErrorCodes set (ONDC-Official/ONDC-RET-Specifications @ release-2.0.2
api/components/error_codes/ErrorCodes/ErrorCodes.yaml).

This buyer-side copy of the suite exercises the buyer's vendored package
to prove cross-repo parity. The helper maps an ONDC code -> the existing
BecknError (type from BecknErrorType + default message).
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

from python.errors import (  # noqa: E402
    ONDC_RETAIL_ERROR_CODES,
    BecknError,
    BecknErrorType,
    OndcErrorClass,
    ondc_error,
)


class TestCatalogue:
    def test_well_known_codes_present_with_message_and_class(self):
        # Anchors spanning each ONDC range.
        for code in ("10001", "20005", "30004", "40002", "50001"):
            assert code in ONDC_RETAIL_ERROR_CODES
            entry = ONDC_RETAIL_ERROR_CODES[code]
            assert entry.message
            assert isinstance(entry.error_class, OndcErrorClass)

    def test_class_buckets_match_ondc_ranges(self):
        assert ONDC_RETAIL_ERROR_CODES["10001"].error_class is OndcErrorClass.GATEWAY
        assert ONDC_RETAIL_ERROR_CODES["20005"].error_class is OndcErrorClass.BUYER_APP
        assert ONDC_RETAIL_ERROR_CODES["30004"].error_class is OndcErrorClass.SELLER_APP
        assert ONDC_RETAIL_ERROR_CODES["40002"].error_class is OndcErrorClass.SELLER_APP

    def test_specific_messages_are_canonical(self):
        assert ONDC_RETAIL_ERROR_CODES["30004"].message == "Item not found"
        assert ONDC_RETAIL_ERROR_CODES["40002"].message == "Item quantity unavailable"
        assert ONDC_RETAIL_ERROR_CODES["10001"].message == "Invalid Signature"


class TestHelper:
    def test_builds_becknerror_with_mapped_type_and_message(self):
        err = ondc_error("40002")
        assert isinstance(err, BecknError)
        assert err.code == "40002"
        assert err.message == "Item quantity unavailable"
        # 40xxx seller business errors map to Beckn DOMAIN-ERROR.
        assert err.type is BecknErrorType.DOMAIN_ERROR

    def test_policy_range_maps_to_policy_error(self):
        err = ondc_error("50001")
        assert err.type is BecknErrorType.POLICY_ERROR
        assert err.message == "Cancellation not possible"

    def test_gateway_context_range_maps_to_context_error(self):
        # 10002 = Invalid City Code -> a context-level failure.
        err = ondc_error("10002")
        assert err.type is BecknErrorType.CONTEXT_ERROR

    def test_core_range_maps_to_core_error(self):
        # 30000 = Invalid request (does not meet API contract) -> CORE.
        err = ondc_error("30000")
        assert err.type is BecknErrorType.CORE_ERROR

    def test_message_and_path_overrides(self):
        err = ondc_error("30004", message="SKU xyz deleted", path="message.order.items[0].id")
        assert err.code == "30004"
        assert err.message == "SKU xyz deleted"
        assert err.path == "message.order.items[0].id"

    def test_unknown_code_raises(self):
        with pytest.raises(KeyError):
            ondc_error("99999")

    def test_serialises_to_beckn_error_shape(self):
        payload = ondc_error("30001").model_dump(exclude_none=True)
        assert payload == {
            "type": "DOMAIN-ERROR",
            "code": "30001",
            "message": "Provider not found",
        }
