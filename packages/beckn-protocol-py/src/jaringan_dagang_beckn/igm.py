"""ONDC IGM (Issue & Grievance Management) message shapes + envelope builders.

Scope is intentionally narrow (YAGNI for the refund-request path):

* :class:`Issue` and its sub-shapes (``IssueActor``, ``IssueDescription``,
  ``IssueLevel``, ``IssueResolutionAction``) — the wire shape carried in
  ``message.issue`` for ``/issue`` and ``message.issue`` for ``/on_issue``.
* :func:`build_issue_envelope` — BAP-side outbound /issue envelope, with the
  ONDC ``context`` block (signed by the BAP using its standard signer).
* :func:`build_on_issue_envelope` — BPP-side outbound /on_issue envelope
  with the resolution action.

The codes in ``issue.category`` / ``issue.sub_category`` / the resolution
``respondent_action`` and ``complainant_action`` fields are localized in
``jaringan-dagang-network/network-extension/enums/igm.yaml`` (network layer
source of truth). This module is a thin typed projection of those codes
onto the wire shape; the YAML carries the human labels (English ``name``
+ Bahasa ``name_id``).

Grounding (codes are NOT invented here — they mirror upstream ONDC IGM v1):

* Issue shape + actor / description / level / resolution fields:
  ONDC-Official/protocol-network-extension @ release-1.0.0
  ``specifications/igm/api/issue.yaml`` / ``on_issue.yaml`` /
  ``api/components/schemas/Issue.yaml``.

This module lives in the shared ``beckn-protocol`` package and is
vendored byte-identically into the seller and buyer repos AND into the
buyer's ``apps/beli-aman-bap/beckn_protocol/`` Vercel-package copy.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .domain_resolver import resolve_ondc_domain

# ---------------------------------------------------------------------------
# Enums (allow-lists mirror network-extension/enums/igm.yaml).
# We accept the upstream IGM v1 set; concrete validation that ITM01-05
# belong under category=ITEM is left to the application layer (one error
# code, not a cross-field constraint).
# ---------------------------------------------------------------------------

ISSUE_CATEGORIES: frozenset[str] = frozenset(
    {"ITEM", "ORDER", "FULFILLMENT", "AGENT", "PAYMENT", "PAYMENT-FNB"}
)
ISSUE_SUB_CATEGORIES_ITEM: frozenset[str] = frozenset(
    {"ITM01", "ITM02", "ITM03", "ITM04", "ITM05"}
)
RESPONDENT_ACTIONS: frozenset[str] = frozenset(
    {"PROCESSING", "RESOLVED", "REJECTED", "ESCALATE"}
)
COMPLAINANT_ACTIONS: frozenset[str] = frozenset(
    {"OPEN", "CLOSE", "ESCALATE"}
)

__all__ = [
    "ISSUE_CATEGORIES",
    "ISSUE_SUB_CATEGORIES_ITEM",
    "RESPONDENT_ACTIONS",
    "COMPLAINANT_ACTIONS",
    "Issue",
    "IssueActor",
    "IssueDescription",
    "IssueLevel",
    "IssueResolutionAction",
    "build_issue_envelope",
    "build_on_issue_envelope",
]


# ---------------------------------------------------------------------------
# Wire-shape pydantic models. Field shapes mirror ONDC IGM v1; we keep
# everything optional that the v1 refund-request path doesn't strictly need
# so callers can build envelopes incrementally.
# ---------------------------------------------------------------------------


class IssueActor(BaseModel):
    """Identifies the human actor on each side of an Issue.

    On a buyer-raised Issue, this is the ``complainant`` (buyer-side
    end-user). On a seller response, ``respondent`` carries the seller's
    customer-care contact.
    """

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["CONSUMER", "AGENT"] = Field(
        default="CONSUMER",
        description="Whether the actor is the end-consumer or a network agent.",
    )
    id: str = Field(
        ...,
        description=(
            "Stable identifier for the actor. For buyers this is the BAP "
            "profile id; for sellers a customer-care contact id."
        ),
    )
    name: Optional[str] = Field(default=None, description="Display name.")
    email: Optional[str] = Field(default=None, description="Contact email.")
    phone: Optional[str] = Field(default=None, description="Contact phone.")


class IssueDescription(BaseModel):
    """Free-text description of the Issue."""

    model_config = ConfigDict(populate_by_name=True)

    short_desc: Optional[str] = Field(
        default=None, description="One-line summary of the Issue."
    )
    long_desc: Optional[str] = Field(
        default=None, description="Detailed Issue body."
    )
    additional_desc: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional structured supplementary data (e.g. evidence URLs, "
            "amounts). Free-form — handlers may ignore."
        ),
    )


class IssueLevel(BaseModel):
    """Severity / escalation level. v1 only uses ``ISSUE`` (no IGP)."""

    model_config = ConfigDict(populate_by_name=True)

    level: Literal["ISSUE", "GRIEVANCE", "DISPUTE"] = Field(
        default="ISSUE",
        description=(
            "Where in the IGM ladder this sits. v1 stays at ISSUE; "
            "GRIEVANCE / DISPUTE map to multi-party escalation (deferred)."
        ),
    )


class IssueResolutionAction(BaseModel):
    """One step in the Issue resolution timeline.

    Both sides append to ``issue.issue_actions`` as the Issue moves through
    PROCESSING -> RESOLVED | REJECTED. ``respondent_action`` is populated
    on /on_issue; ``complainant_action`` on the OPEN / CLOSE / ESCALATE
    posts the BAP makes back.
    """

    model_config = ConfigDict(populate_by_name=True)

    respondent_action: Optional[str] = Field(
        default=None,
        description=(
            "One of RESPONDENT_ACTIONS: PROCESSING, RESOLVED, REJECTED, "
            "ESCALATE. Set on the BPP-emitted /on_issue."
        ),
    )
    complainant_action: Optional[str] = Field(
        default=None,
        description=(
            "One of COMPLAINANT_ACTIONS: OPEN, CLOSE, ESCALATE. Set on "
            "the BAP-emitted /issue (OPEN) or /issue follow-up."
        ),
    )
    short_desc: Optional[str] = Field(
        default=None, description="Short note attached to this action."
    )
    updated_at: Optional[datetime] = Field(
        default=None, description="When the action was recorded."
    )
    updated_by: Optional[IssueActor] = Field(
        default=None, description="Who recorded this action."
    )


class Issue(BaseModel):
    """ONDC IGM v1 Issue object — the body of /issue and /on_issue.

    For a BAP-raised refund-request Issue v1 will populate ``category`` =
    ``ITEM``, ``sub_category`` = an ``ITM0x`` code, ``order_details.id``
    pointing at the BPP order, and ``complainant_info``. The BPP response
    populates ``issue_actions`` with the resolution and (on RESOLVED) the
    refund details under ``resolution``.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(
        ...,
        description="Globally-unique Issue id (UUID). Stable across retries.",
    )
    category: str = Field(
        ...,
        description="Top-level Issue category (one of ISSUE_CATEGORIES).",
    )
    sub_category: str = Field(
        ...,
        description=(
            "Issue sub-category — for ITEM-category v1 disputes one of "
            "ISSUE_SUB_CATEGORIES_ITEM (ITM01-05)."
        ),
    )
    complainant_info: IssueActor = Field(
        ..., description="The buyer-side complainant (BAP profile)."
    )
    order_details: dict[str, Any] = Field(
        ...,
        description=(
            "Order reference. Always carries ``id`` (the BPP order id); "
            "may also carry ``state``, ``items`` etc."
        ),
    )
    description: IssueDescription = Field(
        ..., description="Buyer's description of the Issue."
    )
    source: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Network-source descriptor (network_participant_id + "
            "type=BAP). Defaulted in build_issue_envelope from the "
            "context.bap_id."
        ),
    )
    expected_response_time: Optional[dict[str, str]] = Field(
        default=None,
        description="Buyer's expected response window (ISO 8601 duration).",
    )
    expected_resolution_time: Optional[dict[str, str]] = Field(
        default=None, description="Buyer's expected resolution window."
    )
    status: Optional[str] = Field(
        default="OPEN",
        description=(
            "High-level Issue status. BAP /issue defaults to OPEN; BPP "
            "/on_issue sets PROCESSING / RESOLVED / REJECTED."
        ),
    )
    issue_type: Optional[str] = Field(
        default="ISSUE",
        description="ISSUE | GRIEVANCE | DISPUTE; v1 stays at ISSUE.",
    )
    issue_actions: Optional[dict[str, list[IssueResolutionAction]]] = Field(
        default=None,
        description=(
            "Resolution timeline as ``{complainant_actions: [...], "
            "respondent_actions: [...]}``. Each side appends to its own "
            "list."
        ),
    )
    resolution: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Final resolution payload (set on respondent_action=RESOLVED): "
            "``{short_desc, long_desc, action_triggered, refund_amount?}``."
        ),
    )
    created_at: Optional[datetime] = Field(
        default=None, description="When the Issue was first raised."
    )
    updated_at: Optional[datetime] = Field(
        default=None, description="When this Issue object was last touched."
    )


# ---------------------------------------------------------------------------
# Envelope builders. These mirror the existing ``build_ondc_context`` /
# order_flow envelope helper style on the BAP / BPP: pure functions
# returning a plain dict that the caller signs + POSTs.
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def build_issue_envelope(
    *,
    bap_id: str,
    bap_uri: str,
    bpp_id: str,
    bpp_uri: str,
    transaction_id: Optional[str] = None,
    message_id: Optional[str] = None,
    complainant_id: str,
    complainant_name: Optional[str] = None,
    complainant_email: Optional[str] = None,
    complainant_phone: Optional[str] = None,
    category: str,
    sub_category: str,
    description: str,
    order_id: str,
    refund_amount: Optional[int] = None,
    refund_currency: str = "IDR",
    country_code: str = "IND",
    city_code: str = "std:080",
    core_version: str = "1.1.0",
    issue_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build an ONDC /issue envelope for the BAP to POST to the BPP.

    Args:
        bap_id / bap_uri: the BAP's subscriber identity (signs the request).
        bpp_id / bpp_uri: the destination BPP.
        transaction_id: re-use the order's Beckn transaction_id where
            possible so the BPP can correlate; auto-generated if absent.
        message_id: idempotency key; auto-generated if absent.
        complainant_id: stable buyer-side identifier (BAP profile id).
        complainant_name / email / phone: optional contact details.
        category: one of :data:`ISSUE_CATEGORIES` (will raise ValueError otherwise).
        sub_category: one of :data:`ISSUE_SUB_CATEGORIES_ITEM` for ITEM
            disputes; other categories accept any string in v1 (deferred).
        description: free-text Issue body.
        order_id: the BPP order id this Issue references.
        refund_amount: if the buyer is requesting a specific refund amount,
            carried into ``message.issue.description.additional_desc.refund``.
        country_code / city_code / core_version: Beckn context fields.
        issue_id: pre-assigned Issue UUID (override for tests / retries).

    Returns:
        A signed-ready Beckn envelope ``{context, message: {issue}}``.

    Raises:
        ValueError: if category / sub_category is not in the allow-list
            (we never emit unknown ONDC codes — protocol data is grounded).
    """
    if category not in ISSUE_CATEGORIES:
        raise ValueError(
            f"unknown IGM category {category!r}; allowed: "
            f"{sorted(ISSUE_CATEGORIES)}"
        )
    if category == "ITEM" and sub_category not in ISSUE_SUB_CATEGORIES_ITEM:
        raise ValueError(
            f"unknown IGM sub_category {sub_category!r} for category=ITEM; "
            f"allowed: {sorted(ISSUE_SUB_CATEGORIES_ITEM)}"
        )

    additional_desc: dict[str, Any] = {}
    if refund_amount is not None:
        additional_desc["refund"] = {
            "amount": str(refund_amount),
            "currency": refund_currency,
        }

    issue_obj_id = issue_id or _new_id()
    now = _now()

    issue: dict[str, Any] = {
        "id": issue_obj_id,
        "category": category,
        "sub_category": sub_category,
        "complainant_info": {
            "type": "CONSUMER",
            "id": complainant_id,
            **({"name": complainant_name} if complainant_name else {}),
            **({"email": complainant_email} if complainant_email else {}),
            **({"phone": complainant_phone} if complainant_phone else {}),
        },
        "order_details": {"id": order_id},
        "description": {
            "short_desc": description[:120] if description else "",
            "long_desc": description or "",
            **(
                {"additional_desc": additional_desc}
                if additional_desc
                else {}
            ),
        },
        "source": {"network_participant_id": bap_id, "type": "BAP"},
        "status": "OPEN",
        "issue_type": "ISSUE",
        "issue_actions": {
            "complainant_actions": [
                {
                    "complainant_action": "OPEN",
                    "short_desc": "Issue opened by buyer.",
                    "updated_at": now,
                    "updated_by": {
                        "type": "CONSUMER",
                        "id": complainant_id,
                        **(
                            {"name": complainant_name}
                            if complainant_name
                            else {}
                        ),
                    },
                }
            ],
            "respondent_actions": [],
        },
        "created_at": now,
        "updated_at": now,
    }

    return {
        "context": {
            "domain": resolve_ondc_domain(bpp_id).domain_code,
            "country": country_code,
            "city": city_code,
            "action": "issue",
            "core_version": core_version,
            "bap_id": bap_id,
            "bap_uri": bap_uri,
            "bpp_id": bpp_id,
            "bpp_uri": bpp_uri,
            "transaction_id": transaction_id or _new_id(),
            "message_id": message_id or _new_id(),
            "timestamp": now,
        },
        "message": {"issue": issue},
    }


def build_on_issue_envelope(
    *,
    bap_id: str,
    bap_uri: str,
    bpp_id: str,
    bpp_uri: str,
    transaction_id: str,
    message_id: Optional[str] = None,
    issue_id: str,
    respondent_action: str,
    short_desc: str = "",
    long_desc: str = "",
    refund_amount: Optional[int] = None,
    refund_currency: str = "IDR",
    refund_id: Optional[str] = None,
    respondent_id: Optional[str] = None,
    respondent_name: Optional[str] = None,
    respondent_email: Optional[str] = None,
    respondent_phone: Optional[str] = None,
    country_code: str = "IND",
    city_code: str = "std:080",
    core_version: str = "1.1.0",
) -> dict[str, Any]:
    """Build an ONDC /on_issue envelope for the BPP to POST back to the BAP.

    Args:
        bap_id / bap_uri: the destination BAP.
        bpp_id / bpp_uri: the BPP's subscriber identity (signs the response).
        transaction_id: MUST be the same transaction_id as the inbound
            /issue (ONDC requires correlated callbacks).
        message_id: this response's own idempotency key; auto-generated if
            absent.
        issue_id: the Issue id assigned at /issue time (echoed back).
        respondent_action: one of :data:`RESPONDENT_ACTIONS` —
            PROCESSING (ack), RESOLVED, REJECTED.
        short_desc / long_desc: human-readable resolution text.
        refund_amount: on RESOLVED with a refund, the refunded amount.
        refund_id: on RESOLVED, the seller-side refund id (Xendit refund or
            internal RefundRequest id) so the BAP can correlate.
        respondent_id / name / email / phone: BPP customer-care contact
            details surfaced in the resolution.
        country_code / city_code / core_version: Beckn context fields.

    Returns:
        A signed-ready Beckn envelope ``{context, message: {issue}}``.

    Raises:
        ValueError: if ``respondent_action`` is not in
            :data:`RESPONDENT_ACTIONS`.
    """
    if respondent_action not in RESPONDENT_ACTIONS:
        raise ValueError(
            f"unknown IGM respondent_action {respondent_action!r}; "
            f"allowed: {sorted(RESPONDENT_ACTIONS)}"
        )

    now = _now()
    updated_by: dict[str, Any] = {
        "type": "AGENT",
        "id": respondent_id or bpp_id,
    }
    if respondent_name:
        updated_by["name"] = respondent_name
    if respondent_email:
        updated_by["email"] = respondent_email
    if respondent_phone:
        updated_by["phone"] = respondent_phone

    resolution: Optional[dict[str, Any]] = None
    if respondent_action in {"RESOLVED", "REJECTED"}:
        resolution = {
            "short_desc": short_desc or respondent_action.title(),
            "long_desc": long_desc or "",
            "action_triggered": respondent_action,
        }
        if refund_amount is not None:
            resolution["refund_amount"] = {
                "value": str(refund_amount),
                "currency": refund_currency,
            }
        if refund_id is not None:
            resolution["refund_id"] = refund_id

    issue: dict[str, Any] = {
        "id": issue_id,
        "status": respondent_action,
        "issue_type": "ISSUE",
        "issue_actions": {
            "complainant_actions": [],
            "respondent_actions": [
                {
                    "respondent_action": respondent_action,
                    "short_desc": short_desc or respondent_action.title(),
                    "updated_at": now,
                    "updated_by": updated_by,
                }
            ],
        },
        **({"resolution": resolution} if resolution else {}),
        "updated_at": now,
    }

    return {
        "context": {
            "domain": resolve_ondc_domain(bpp_id).domain_code,
            "country": country_code,
            "city": city_code,
            "action": "on_issue",
            "core_version": core_version,
            "bap_id": bap_id,
            "bap_uri": bap_uri,
            "bpp_id": bpp_id,
            "bpp_uri": bpp_uri,
            "transaction_id": transaction_id,
            "message_id": message_id or _new_id(),
            "timestamp": now,
        },
        "message": {"issue": issue},
    }
