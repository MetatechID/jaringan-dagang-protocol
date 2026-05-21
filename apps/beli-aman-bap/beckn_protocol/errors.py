"""Beckn protocol error models + ONDC retail error-code catalogue.

Defines structured error types returned in Beckn protocol responses.
Error codes follow Beckn Core Specification conventions; the ONDC retail
catalogue below standardizes the numeric codes used on the Indonesian
ONDC:RET network.

The ONDC retail catalogue (:data:`ONDC_RETAIL_ERROR_CODES`) and the
:func:`ondc_error` helper are vendored byte-identically into the seller
and buyer repos so both ends map an ONDC code to the same Beckn error.

Grounding: the numeric codes, their class bucket (Gateway / Buyer App /
Seller App) and default messages mirror the canonical ONDC RET v2.0.2
ErrorCodes set -- ONDC-Official/ONDC-RET-Specifications @ ``release-2.0.2``
``api/components/error_codes/ErrorCodes/ErrorCodes.yaml``. Codes are not
fabricated here.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class BecknErrorType(str, Enum):
    """Standard Beckn error type categories."""

    CONTEXT_ERROR = "CONTEXT-ERROR"
    CORE_ERROR = "CORE-ERROR"
    DOMAIN_ERROR = "DOMAIN-ERROR"
    POLICY_ERROR = "POLICY-ERROR"
    JSON_SCHEMA_ERROR = "JSON-SCHEMA-ERROR"


class BecknError(BaseModel):
    """Beckn protocol error object.

    Returned in the `error` field of a Beckn response when something goes wrong.
    """

    model_config = {"populate_by_name": True}

    type: BecknErrorType = Field(
        ...,
        description="Category of the error",
    )
    code: str = Field(
        ...,
        description="Beckn-standard error code (e.g. '30001')",
    )
    message: str = Field(
        ...,
        description="Human-readable error message",
    )
    path: Optional[str] = Field(
        default=None,
        description="JSON path to the field that caused the error",
    )


# ===========================================================================
# ONDC retail error-code catalogue
# ===========================================================================


class OndcErrorClass(str, Enum):
    """ONDC RET error-code owner bucket (from the upstream ErrorCodes spec).

    This is the ONDC-side classification (which network participant the
    error originates from); it is mapped onto :class:`BecknErrorType` by
    :func:`ondc_error`.
    """

    GATEWAY = "Gateway"
    BUYER_APP = "Buyer App"
    SELLER_APP = "Seller App"


@dataclass(frozen=True)
class OndcErrorCode:
    """A single ONDC retail error code: its class bucket + default message."""

    code: str
    error_class: OndcErrorClass
    message: str
    description: str


def _e(code: str, klass: OndcErrorClass, message: str, description: str) -> tuple:
    return code, OndcErrorCode(code, klass, message, description)


# The canonical ONDC RET v2.0.2 ErrorCodes set, verbatim (code, class,
# message, description). Mirrors ErrorCodes.yaml @ release-2.0.2.
ONDC_RETAIL_ERROR_CODES: dict[str, OndcErrorCode] = dict(
    [
        # --- Gateway (10xxx) ---
        _e("10000", OndcErrorClass.GATEWAY, "Bad or Invalid request error", "Generic bad or invalid request error"),
        _e("10001", OndcErrorClass.GATEWAY, "Invalid Signature", "Cannot verify signature for request"),
        _e("10002", OndcErrorClass.GATEWAY, "Invalid City Code", "Valid city code needs to be passed for search"),
        # --- Buyer App (20xxx-27xxx) ---
        _e("20000", OndcErrorClass.BUYER_APP, "Invalid catalog", "Catalog refresh is invalid"),
        _e("20001", OndcErrorClass.BUYER_APP, "Invalid Signature", "Cannot verify signature for response"),
        _e("20002", OndcErrorClass.BUYER_APP, "Stale Request", "Cannot process stale request"),
        _e("20003", OndcErrorClass.BUYER_APP, "Provider not found", "Provider not found"),
        _e("20004", OndcErrorClass.BUYER_APP, "Provider location not found", "Provider location not found"),
        _e("20005", OndcErrorClass.BUYER_APP, "Item not found", "Item not found"),
        _e("20006", OndcErrorClass.BUYER_APP, "Invalid response", "Invalid response does not meet API contract specifications"),
        _e("20007", OndcErrorClass.BUYER_APP, "Invalid order state", "Order/fulfillment state is stale or not valid"),
        _e("20008", OndcErrorClass.BUYER_APP, "Response out of sequence", "Callback received prior to ACK for request or out of sequence"),
        _e("20009", OndcErrorClass.BUYER_APP, "Timeout", "Callback received late, session timed out"),
        _e("21001", OndcErrorClass.BUYER_APP, "Feature not supported", "Feature not supported"),
        _e("21002", OndcErrorClass.BUYER_APP, "Increase in item quantity", "Increase in item quantity"),
        _e("21003", OndcErrorClass.BUYER_APP, "Change in item quote", "Change in item quote without change in quantity"),
        _e("22501", OndcErrorClass.BUYER_APP, "Part Fill Unacceptable", "Buyer doesn't accept part fill for the order, wants to cancel the order"),
        _e("22502", OndcErrorClass.BUYER_APP, "Cancellation unacceptable", "Invalid cancellation reason"),
        _e("22503", OndcErrorClass.BUYER_APP, "Cancellation unacceptable", "Updated quote does not match original order value and cancellation terms"),
        _e("22504", OndcErrorClass.BUYER_APP, "Invalid Fulfillment TAT", "Fulfillment TAT is different from what was quoted earlier"),
        _e("22505", OndcErrorClass.BUYER_APP, "Invalid Cancellation Terms", "Cancellation terms are different from what was quoted earlier"),
        _e("22506", OndcErrorClass.BUYER_APP, "Invalid Terms of Reference", "Terms of Reference are different from what was quoted earlier"),
        _e("22507", OndcErrorClass.BUYER_APP, "Invalid Quote", "Quote is invalid as it does not meet the API contract specifications"),
        _e("22508", OndcErrorClass.BUYER_APP, "Invalid Part Cancel Request", "Part cancel request is invalid"),
        _e("22509", OndcErrorClass.BUYER_APP, "Cancel Return Request", "Buyer cancelling return request"),
        _e("23001", OndcErrorClass.BUYER_APP, "Internal Error", "Cannot process response due to internal error, please retry"),
        _e("23002", OndcErrorClass.BUYER_APP, "Order validation failure", "Order validation failure"),
        _e("25001", OndcErrorClass.BUYER_APP, "Order Confirm Failure", "Buyer App cannot confirm order as no response from Seller App"),
        _e("27501", OndcErrorClass.BUYER_APP, "Terms and Conditions unacceptable", "Seller App terms and conditions not acceptable to Buyer App"),
        _e("27502", OndcErrorClass.BUYER_APP, "Order terminated", "Order terminated as Seller App did not accept terms and conditions proposed by Buyer App"),
        # --- Seller App (30xxx-50xxx) ---
        _e("30000", OndcErrorClass.SELLER_APP, "Invalid request", "Invalid request does not meet API contract specifications"),
        _e("30001", OndcErrorClass.SELLER_APP, "Provider not found", "When Seller App is unable to find the provider id sent by the Buyer App"),
        _e("30002", OndcErrorClass.SELLER_APP, "Provider location not found", "When Seller App is unable to find the provider location id sent by the Buyer App"),
        _e("30003", OndcErrorClass.SELLER_APP, "Provider category not found", "When Seller App is unable to find the provider category id sent by the Buyer App"),
        _e("30004", OndcErrorClass.SELLER_APP, "Item not found", "Unable to find details for item, may have been deleted"),
        _e("30005", OndcErrorClass.SELLER_APP, "Invalid return request", "Return reason is invalid"),
        _e("30006", OndcErrorClass.SELLER_APP, "Offer code invalid", "Offer code is not valid anymore"),
        _e("30007", OndcErrorClass.SELLER_APP, "Offer fulfillment error", "Offer cannot be fulfilled at this time"),
        _e("30008", OndcErrorClass.SELLER_APP, "Location Serviceability error", "Pickup location not serviceable by Logistics Provider"),
        _e("30009", OndcErrorClass.SELLER_APP, "Location Serviceability error", "Dropoff location not serviceable by Logistics Provider"),
        _e("30010", OndcErrorClass.SELLER_APP, "Location Serviceability error", "Delivery distance exceeds the maximum serviceability distance"),
        _e("30011", OndcErrorClass.SELLER_APP, "Order Serviceability error", "Delivery Partners not available"),
        _e("30012", OndcErrorClass.SELLER_APP, "Invalid cancellation reason", "Cancellation reason is invalid"),
        _e("30013", OndcErrorClass.SELLER_APP, "Invalid Fulfillment TAT", "Fulfillment TAT is different from what was quoted earlier"),
        _e("30014", OndcErrorClass.SELLER_APP, "Cancellation unacceptable", "Cancellation request is rejected as fulfillment TAT is not breached"),
        _e("30015", OndcErrorClass.SELLER_APP, "Invalid rating value", "When the Seller App receives an invalid value as the rating value in value"),
        _e("30016", OndcErrorClass.SELLER_APP, "Invalid Signature", "Cannot verify signature for request"),
        _e("30017", OndcErrorClass.SELLER_APP, "Merchant unavailable", "Merchant is currently not taking orders"),
        _e("30018", OndcErrorClass.SELLER_APP, "Invalid Order", "Order not found"),
        _e("30019", OndcErrorClass.SELLER_APP, "Order Confirm Error", "Seller App is unable to confirm the order"),
        _e("30020", OndcErrorClass.SELLER_APP, "Order Confirm Failure", "Seller App cannot confirm order as no response from Buyer App"),
        _e("30021", OndcErrorClass.SELLER_APP, "Merchant Inactive", "Merchant is inactive"),
        _e("30022", OndcErrorClass.SELLER_APP, "Stale Request", "Cannot process stale request"),
        _e("30023", OndcErrorClass.SELLER_APP, "Minimum order value error", "Cart value is less than minimum order value"),
        _e("31001", OndcErrorClass.SELLER_APP, "Internal Error", "Cannot process request due to internal error, please retry"),
        _e("31002", OndcErrorClass.SELLER_APP, "Order validation failure", "Order validation failure"),
        _e("31003", OndcErrorClass.SELLER_APP, "Order processing in progress", "Order processing in progress"),
        _e("31004", OndcErrorClass.SELLER_APP, "Payment Failed", "Payment fails"),
        _e("40000", OndcErrorClass.SELLER_APP, "Business Error", "Generic business error"),
        _e("40001", OndcErrorClass.SELLER_APP, "Feature not supported", "Feature not supported"),
        _e("40002", OndcErrorClass.SELLER_APP, "Item quantity unavailable", "When the Seller App is unable to fulfill the required quantity for items in the order"),
        _e("40003", OndcErrorClass.SELLER_APP, "Quote unavailable", "Quote no longer available"),
        _e("40004", OndcErrorClass.SELLER_APP, "Payment type not supported", "Payment type not supported"),
        _e("40005", OndcErrorClass.SELLER_APP, "Tracking not enabled", "Tracking not enabled for any fulfillment in the order"),
        _e("40006", OndcErrorClass.SELLER_APP, "Fulfilment agent unavailable", "When an agent for fulfilment is not available"),
        _e("40007", OndcErrorClass.SELLER_APP, "Change in item quantity", "Change in item quantity"),
        _e("40008", OndcErrorClass.SELLER_APP, "Change in quote", "Change in quote"),
        _e("40009", OndcErrorClass.SELLER_APP, "Maximum order qty exceeded", "Maximum order qty exceeded"),
        _e("40010", OndcErrorClass.SELLER_APP, "Expired authorization", "Authorization code has expired"),
        _e("40011", OndcErrorClass.SELLER_APP, "Invalid authorization", "Authorization code is invalid"),
        _e("41001", OndcErrorClass.SELLER_APP, "Finder fee not acceptable", "Buyer finder fee is not acceptable"),
        _e("50000", OndcErrorClass.SELLER_APP, "Policy Error", "Generic Policy Error"),
        _e("50001", OndcErrorClass.SELLER_APP, "Cancellation not possible", "When the Seller App is unable to cancel the order due to it's cancellation policy"),
        _e("50002", OndcErrorClass.SELLER_APP, "Updation not possible", "When the Seller App is unable to update the order due to it's updation policy"),
        _e("50003", OndcErrorClass.SELLER_APP, "Unsupported rating category", "When the Seller App receives an entity to rate which is not supported"),
        _e("50004", OndcErrorClass.SELLER_APP, "Support unavailable", "When the Seller App receives an object if for which it does not provide support"),
        _e("50005", OndcErrorClass.SELLER_APP, "Terms and Conditions unacceptable", "Buyer App terms and conditions not acceptable to Seller App"),
        _e("50006", OndcErrorClass.SELLER_APP, "Order terminated", "Order terminated as Buyer App did not accept terms proposed by Seller App"),
        _e("50007", OndcErrorClass.SELLER_APP, "Fulfillment not found", "Fulfillment not found"),
        _e("50008", OndcErrorClass.SELLER_APP, "Fulfillment cannot be updated", "Fulfillment has reached terminal state, cannot be updated"),
        # --- IGM (Issue & Grievance Management) — 90xxx ---
        # Mirrors ONDC-Official/protocol-network-extension @ release-1.0.0
        # error_codes/igm/ErrorCodes.yaml. v1 scope is the refund-request
        # path so we localize the codes the /issue + /on_issue exchanges
        # can actually emit; broader IGM error space (escalation, ODR) is
        # deferred with the rest of multi-party IGM.
        _e("90001", OndcErrorClass.SELLER_APP, "Invalid Issue", "Issue could not be processed - invalid category/sub_category or order reference"),
        _e("90002", OndcErrorClass.SELLER_APP, "Stale Issue", "Issue raised after the IGM grievance window has expired"),
        _e("90003", OndcErrorClass.SELLER_APP, "Issue not found", "Referenced issue_id does not exist on the respondent side"),
        _e("90004", OndcErrorClass.SELLER_APP, "Issue already resolved", "Cannot update or re-open an Issue that is already in a terminal state"),
        _e("90005", OndcErrorClass.SELLER_APP, "Order not eligible", "Order state does not permit raising an Issue"),
        # --- RSP (Reconciliation & Settlement Protocol) — 95xxx ---
        # Mirrors ONDC-Official/protocol-network-extension @ release-1.0.0
        # error_codes/rsp/ErrorCodes.yaml. v1 scope is the per-order
        # settlement-record exchange; broader RSP error space (rail-level
        # failures, NACH reject codes) is deferred with the actual money
        # movement integration.
        _e("95001", OndcErrorClass.SELLER_APP, "Invalid Settlement Record", "Settlement record could not be processed - invalid basis/window/counterparty"),
        _e("95002", OndcErrorClass.SELLER_APP, "Order not eligible", "Order state does not permit raising a settlement record"),
        _e("95003", OndcErrorClass.SELLER_APP, "Settlement already recorded", "A settlement record already exists for this order; cannot re-emit"),
        _e("95004", OndcErrorClass.SELLER_APP, "Settlement amount mismatch", "Counterparty's computed settlement amount differs from sender's"),
        _e("95005", OndcErrorClass.SELLER_APP, "Reconciliation window closed", "Settlement received after the reconciliation window has closed"),
        # --- Rating — 70xxx ---
        # Mirrors ONDC-Official/protocol-network-extension @ release-1.0.0
        # error_codes/rating/ErrorCodes.yaml. Note 30015 ("Invalid rating
        # value") and 50003 ("Unsupported rating category") above are the
        # legacy in-band rating errors; the 70xxx range is the rating-
        # dedicated bucket from the protocol-network-extension.
        _e("70001", OndcErrorClass.SELLER_APP, "Invalid Rating", "Rating could not be processed - invalid category or out-of-range value"),
        _e("70002", OndcErrorClass.SELLER_APP, "Rating not allowed", "Order state does not permit rating yet"),
        _e("70003", OndcErrorClass.SELLER_APP, "Rating already submitted", "Buyer has already rated this order; updates not allowed in v1"),
        _e("70004", OndcErrorClass.SELLER_APP, "Rating window closed", "Rating submitted after the post-fulfillment rating window closed"),
        _e("70005", OndcErrorClass.SELLER_APP, "Unsupported rating category", "BPP does not accept ratings for the requested entity type"),
    ]
)


def _beckn_type_for(code: str, error_class: OndcErrorClass) -> BecknErrorType:
    """Map an ONDC code + class onto the Beckn error-type taxonomy.

    Beckn buckets errors as CONTEXT / CORE / DOMAIN / POLICY / JSON-SCHEMA.
    ONDC numbers them by participant + range; the standard correspondence:

    * ``50xxx`` (policy)  -> POLICY-ERROR
    * Signature / city-code / stale / sequence / timeout (transport &
      envelope failures) -> CONTEXT-ERROR
    * Generic "invalid request / does not meet API contract / invalid
      response" -> CORE-ERROR
    * everything else (business/domain failures: not found, quantity,
      serviceability, cancellation reason, ...) -> DOMAIN-ERROR
    """
    if code.startswith("50"):
        return BecknErrorType.POLICY_ERROR
    # Envelope / transport / sequencing failures -> context.
    context_codes = {
        "10001", "10002",          # invalid signature, invalid city code
        "20001", "20002", "20008", "20009",  # signature, stale, sequence, timeout
        "30016", "30022",          # signature, stale (seller)
    }
    if code in context_codes:
        return BecknErrorType.CONTEXT_ERROR
    # Schema / API-contract conformance -> core.
    core_codes = {"10000", "20006", "30000"}
    if code in core_codes:
        return BecknErrorType.CORE_ERROR
    return BecknErrorType.DOMAIN_ERROR


def ondc_error(
    code: str,
    *,
    message: Optional[str] = None,
    path: Optional[str] = None,
) -> BecknError:
    """Construct a :class:`BecknError` from a standardized ONDC RET code.

    Args:
        code: an ONDC retail error code present in
            :data:`ONDC_RETAIL_ERROR_CODES` (e.g. ``"40002"``).
        message: optional override for the default ONDC message (useful
            to add the offending SKU/order detail).
        path: optional JSON path to the offending field.

    Returns:
        A :class:`BecknError` whose ``type`` is the Beckn taxonomy bucket
        for the code, ``code`` is the ONDC numeric code, and ``message``
        is the canonical ONDC message (unless overridden).

    Raises:
        KeyError: if ``code`` is not a known ONDC retail error code (we
            never fabricate codes/messages).
    """
    entry = ONDC_RETAIL_ERROR_CODES[code]
    return BecknError(
        type=_beckn_type_for(code, entry.error_class),
        code=code,
        message=message if message is not None else entry.message,
        path=path,
    )
