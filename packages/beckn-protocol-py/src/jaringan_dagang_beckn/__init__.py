"""Beckn protocol library for the Beli Aman BAP.

This package is a **vendored copy** of the canonical
``packages/beckn-protocol/python/`` source, present here because the
Vercel project's ``rootDirectory=apps/beli-aman-bap`` setting means
anything outside that directory is NOT uploaded to the function image.
A re-export shim that points at ``<repo-root>/packages/beckn-protocol``
therefore cannot work on Vercel (the path simply does not exist at
runtime — the function boot crashes at import time and every endpoint
returns ``FUNCTION_INVOCATION_FAILED``, which the browser surfaces as
spurious CORS errors).

Keep these files in sync with ``packages/beckn-protocol/python/*.py``
when changing either side. The two directories are intentionally
byte-identical for every shared module; if they ever diverge, the
canonical upstream wins.

The ``BecknProtocolUnavailable`` exception is preserved for backward
compatibility with callers that ``except`` on it (it can never actually
be raised now that the package is self-contained, but main.py and the
defensive router blocks still import it).
"""


class BecknProtocolUnavailable(ImportError):
    """Retained for backward compatibility.

    Older code paths (notably ``main.py``'s defensive router-import
    blocks) discriminate "canonical Beckn-protocol package missing →
    fail loud" from "router's optional third-party dep missing → degrade
    gracefully" by catching this type. Now that ``beckn_protocol`` is
    vendored in-tree, the canonical-missing case can no longer arise at
    runtime, but keeping the class exported avoids a wide blast-radius
    rename across the BAP and its sibling repos.
    """


# Context
from .context import (
    BecknAction,
    BecknCity,
    BecknContext,
    BecknCountry,
    BecknLocation,
)

# Catalog
from .catalog import (
    Catalog,
    CategoryId,
    Descriptor,
    Image,
    Item,
    Price,
    Provider,
    Quantity,
    QuantityDetail,
    QuantityMeasure,
    Tag,
    TagValue,
)

# Order
from .order import (
    Billing,
    CancellationTerm,
    Order,
    OrderItem,
    OrderState,
    Quote,
    QuoteBreakup,
    QuoteBreakupItem,
)

# Payment
from .payment import (
    Payment,
    PaymentCollectedBy,
    PaymentParams,
    PaymentStatus,
    PaymentType,
)

# Fulfillment
from .fulfillment import (
    Address,
    Contact,
    Fulfillment,
    FulfillmentEnd,
    FulfillmentStart,
    FulfillmentState,
    FulfillmentStop,
    FulfillmentType,
    Gps,
    Location,
    Person,
)

# Rating (envelope builders added in Task A6)
from .rating import (
    RATING_CATEGORIES,
    Rating,
    RatingCategory,
    build_on_rating_envelope,
    build_rating_envelope,
)

# Message envelopes
from .message import (
    AckMessage,
    AckResponse,
    AckStatus,
    BecknRequest,
    BecknResponse,
)

# Errors (+ ONDC retail error-code catalogue)
from .errors import (
    ONDC_RETAIL_ERROR_CODES,
    BecknError,
    BecknErrorType,
    OndcErrorClass,
    OndcErrorCode,
    ondc_error,
)

# ONDC @ondc/org tag builders (RET11 search/select/confirm)
from .ondc_tags import (
    ONDC_ORG_STATUTORY_PACKAGED_COMMODITIES,
    ONDC_ORG_STATUTORY_PREPACKAGED_FOOD,
    build_fulfillment_ondc_tags,
    build_item_statutory_tags,
    build_payment_settlement_tags,
)

# Signing utilities
from .signer import (
    BecknSigner,
    KeyPair,
    generate_keypair,
    sign_request,
    verify_request,
)

# Registry lookup
from .registry import (
    RegistryClient,
    Subscriber,
    SubscriberNotFound,
)

# ONDC domain resolution
from .domain_resolver import (
    DEFAULT_ONDC_DOMAIN,
    ONDC_RETAIL_BECKN_BASE,
    OndcDomain,
    resolve_ondc_domain,
)

# ONDC IGM (Issue & Grievance Management) v1 — refund-request scope
from .igm import (
    COMPLAINANT_ACTIONS,
    ISSUE_CATEGORIES,
    ISSUE_SUB_CATEGORIES_ITEM,
    Issue,
    IssueActor,
    IssueDescription,
    IssueLevel,
    IssueResolutionAction,
    RESPONDENT_ACTIONS,
    build_issue_envelope,
    build_on_issue_envelope,
)

# ONDC RSP (Reconciliation & Settlement Protocol) v1 — settlement records
from .rsp import (
    SETTLEMENT_BASES,
    SETTLEMENT_STATUSES,
    SETTLEMENT_TYPES,
    SETTLEMENT_WINDOWS,
    SettlementCounterparty,
    SettlementRecord,
    SettlementWindow,
    build_on_settle_envelope,
    build_settle_envelope,
)

# ONDC Score (reputation) v1 — BPP-local snapshot + band mapping
from .score import (
    SCORE_ATTRIBUTES,
    SCORE_BANDS,
    Score,
    ScoreAttribute,
    ScoreAttributeWeights,
    compute_score_band,
)

__all__ = [
    "BecknProtocolUnavailable",
    # Context
    "BecknAction",
    "BecknCity",
    "BecknContext",
    "BecknCountry",
    "BecknLocation",
    # Catalog
    "Catalog",
    "CategoryId",
    "Descriptor",
    "Image",
    "Item",
    "Price",
    "Provider",
    "Quantity",
    "QuantityDetail",
    "QuantityMeasure",
    "Tag",
    "TagValue",
    # Order
    "Billing",
    "CancellationTerm",
    "Order",
    "OrderItem",
    "OrderState",
    "Quote",
    "QuoteBreakup",
    "QuoteBreakupItem",
    # Payment
    "Payment",
    "PaymentCollectedBy",
    "PaymentParams",
    "PaymentStatus",
    "PaymentType",
    # Fulfillment
    "Address",
    "Contact",
    "Fulfillment",
    "FulfillmentEnd",
    "FulfillmentStart",
    "FulfillmentState",
    "FulfillmentStop",
    "FulfillmentType",
    "Gps",
    "Location",
    "Person",
    # Rating (envelope builders added in Task A6)
    "RATING_CATEGORIES",
    "Rating",
    "RatingCategory",
    "build_on_rating_envelope",
    "build_rating_envelope",
    # Message envelopes
    "AckMessage",
    "AckResponse",
    "AckStatus",
    "BecknRequest",
    "BecknResponse",
    # Errors
    "BecknError",
    "BecknErrorType",
    "ONDC_RETAIL_ERROR_CODES",
    "OndcErrorClass",
    "OndcErrorCode",
    "ondc_error",
    # ONDC tag builders
    "ONDC_ORG_STATUTORY_PACKAGED_COMMODITIES",
    "ONDC_ORG_STATUTORY_PREPACKAGED_FOOD",
    "build_fulfillment_ondc_tags",
    "build_item_statutory_tags",
    "build_payment_settlement_tags",
    # Signing
    "BecknSigner",
    "KeyPair",
    "generate_keypair",
    "sign_request",
    "verify_request",
    # Registry
    "RegistryClient",
    "Subscriber",
    "SubscriberNotFound",
    # ONDC domain resolution
    "DEFAULT_ONDC_DOMAIN",
    "ONDC_RETAIL_BECKN_BASE",
    "OndcDomain",
    "resolve_ondc_domain",
    # ONDC IGM (Issue & Grievance Management) v1
    "COMPLAINANT_ACTIONS",
    "ISSUE_CATEGORIES",
    "ISSUE_SUB_CATEGORIES_ITEM",
    "Issue",
    "IssueActor",
    "IssueDescription",
    "IssueLevel",
    "IssueResolutionAction",
    "RESPONDENT_ACTIONS",
    "build_issue_envelope",
    "build_on_issue_envelope",
    # ONDC RSP (Reconciliation & Settlement Protocol) v1
    "SETTLEMENT_BASES",
    "SETTLEMENT_STATUSES",
    "SETTLEMENT_TYPES",
    "SETTLEMENT_WINDOWS",
    "SettlementCounterparty",
    "SettlementRecord",
    "SettlementWindow",
    "build_on_settle_envelope",
    "build_settle_envelope",
    # ONDC Score (reputation) v1
    "SCORE_ATTRIBUTES",
    "SCORE_BANDS",
    "Score",
    "ScoreAttribute",
    "ScoreAttributeWeights",
    "compute_score_band",
]
