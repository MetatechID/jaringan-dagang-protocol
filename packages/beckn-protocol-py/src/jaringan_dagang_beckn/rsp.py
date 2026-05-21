"""ONDC RSP (Reconciliation & Settlement Protocol) message shapes + envelope builders.

Scope is intentionally narrow (YAGNI per Task A6):

* :class:`SettlementCounterparty`, :class:`SettlementWindow`,
  :class:`SettlementRecord` — Pydantic projections of the ONDC RSP v1
  ``message.settlement`` shape carried on ``/settle`` and ``/on_settle``.
* :func:`build_settle_envelope` — BAP-side outbound /settle envelope.
* :func:`build_on_settle_envelope` — BPP-side outbound /on_settle envelope.

A6 v1 emits / receives settlement RECORDS only. The actual money movement
(BI-FAST / BI-RTGS / SKNBI rails, operator-driven reconciliation files,
RBI-style daily settlement netting) is OUT OF SCOPE — settlement records
let the network observe the per-NP balance, but the operator still moves
the funds out-of-band in v1.

The codes in ``settlement.basis`` / ``window`` / ``status`` and the
``error_codes`` are localized in
``jaringan-dagang-network/network-extension/enums/rsp.yaml`` (network layer
source of truth). This module is a thin typed projection of those codes
onto the wire shape.

Grounding (codes are NOT invented here — they mirror upstream ONDC RSP v1):

* Settlement record shape + basis / window / status fields:
  ONDC-Official/protocol-network-extension @ release-1.0.0
  ``specifications/rsp/api/settle.yaml`` / ``on_settle.yaml`` /
  ``api/components/schemas/SettlementTerms.yaml``.

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
# Enums (allow-lists mirror network-extension/enums/rsp.yaml).
# ---------------------------------------------------------------------------

SETTLEMENT_TYPES: frozenset[str] = frozenset(
    {"NEFT", "IMPS", "RTGS", "UPI", "BANK"}
)
SETTLEMENT_STATUSES: frozenset[str] = frozenset(
    {"NOT_PAID", "PAID", "PARTIAL_PAID"}
)
SETTLEMENT_BASES: frozenset[str] = frozenset(
    {"DELIVERY", "PICKUP", "RECEIPT"}
)
# ISO 8601 duration codes accepted in v1. The yaml lists three; we keep the
# allow-list closed so unknown windows are rejected at envelope-build time
# rather than at the BPP side (faster feedback in dev).
SETTLEMENT_WINDOWS: frozenset[str] = frozenset(
    {"P1D", "P3D", "P7D"}
)

__all__ = [
    "SETTLEMENT_TYPES",
    "SETTLEMENT_STATUSES",
    "SETTLEMENT_BASES",
    "SETTLEMENT_WINDOWS",
    "SettlementCounterparty",
    "SettlementWindow",
    "SettlementRecord",
    "build_settle_envelope",
    "build_on_settle_envelope",
]


# ---------------------------------------------------------------------------
# Wire-shape pydantic models. Field shapes mirror ONDC RSP v1; we keep
# everything optional that the v1 settlement-record path doesn't strictly need
# so callers can build envelopes incrementally.
# ---------------------------------------------------------------------------


class SettlementCounterparty(BaseModel):
    """One side of a settlement record.

    On a BAP-initiated /settle this is typically the BPP (the party owed
    money for the fulfilled order). On a BPP-emitted /on_settle the
    counterparty may also be a logistics provider or a fee-collecting
    network participant.
    """

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["BAP", "BPP", "LSP", "BG"] = Field(
        ...,
        description=(
            "Network-participant role of the counterparty. v1 retail uses "
            "BAP / BPP; logistics + bg (buyer-app gateway) are accepted for "
            "forward-compat."
        ),
    )
    id: str = Field(
        ...,
        description="Subscriber id of the counterparty (canonical scheme).",
    )
    uri: Optional[str] = Field(
        default=None,
        description="Subscriber URI for callbacks / reconciliation pulls.",
    )
    amount: int = Field(
        ...,
        description=(
            "Amount owed to / by this counterparty in minor units (IDR "
            "whole rupiahs). Positive = owed TO counterparty by sender; "
            "negative = owed FROM counterparty back to sender."
        ),
    )
    currency: str = Field(
        default="IDR",
        description="ISO 4217 currency code (v1 retail is IDR-only).",
    )


class SettlementWindow(BaseModel):
    """The reconciliation window for a settlement record.

    ``duration`` is an ISO 8601 duration string (P1D / P3D / P7D in v1).
    ``starts_at`` is computed from ``settlement_basis`` + the relevant
    order milestone (fulfillment-delivered timestamp for DELIVERY etc.).
    """

    model_config = ConfigDict(populate_by_name=True)

    duration: str = Field(
        ...,
        description=(
            "ISO 8601 duration code from SETTLEMENT_WINDOWS (P1D / P3D / P7D)."
        ),
    )
    starts_at: Optional[datetime] = Field(
        default=None,
        description="When the window's clock started (basis event timestamp).",
    )
    ends_at: Optional[datetime] = Field(
        default=None,
        description="Computed deadline (``starts_at + duration``).",
    )


class SettlementRecord(BaseModel):
    """ONDC RSP v1 SettlementRecord — the body of /settle and /on_settle.

    A SettlementRecord ties a BPP order to a per-counterparty payable
    amount, the basis that triggers the reconciliation clock, the window
    duration, and (on /on_settle) the BPP-side reference (e.g. internal
    settlement ledger row id) so the BAP can correlate.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(
        ...,
        description="Globally-unique settlement record id (UUID).",
    )
    order_id: str = Field(
        ...,
        description="The BPP order id this settlement record references.",
    )
    settlement_basis: str = Field(
        ...,
        description=(
            "What event triggers the window's clock. One of "
            "SETTLEMENT_BASES (DELIVERY / PICKUP / RECEIPT)."
        ),
    )
    settlement_window: SettlementWindow = Field(
        ...,
        description="The reconciliation window for this record.",
    )
    settlement_type: Optional[str] = Field(
        default=None,
        description=(
            "Preferred rail for the eventual money movement. One of "
            "SETTLEMENT_TYPES. Optional in v1 — operator picks at "
            "execution time."
        ),
    )
    settlement_status: Optional[str] = Field(
        default="NOT_PAID",
        description=(
            "Current status of the settlement. One of SETTLEMENT_STATUSES. "
            "BAP /settle defaults to NOT_PAID; BPP /on_settle echoes."
        ),
    )
    settlement_reference: Optional[str] = Field(
        default=None,
        description=(
            "BPP-side reference (e.g. internal SettlementLedger id) "
            "carried on /on_settle so the BAP can correlate."
        ),
    )
    counterparties: list[SettlementCounterparty] = Field(
        default_factory=list,
        description=(
            "Per-counterparty payable breakdown. v1 retail typically "
            "carries the single BPP counterparty; multi-party splits "
            "(logistics, fees) are accepted for forward-compat."
        ),
    )
    created_at: Optional[datetime] = Field(
        default=None,
        description="When this settlement record was first emitted.",
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        description="When this settlement record was last touched.",
    )


# ---------------------------------------------------------------------------
# Envelope builders. These mirror the existing ``build_issue_envelope`` /
# order_flow envelope helper style on the BAP / BPP: pure functions
# returning a plain dict that the caller signs + POSTs.
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def build_settle_envelope(
    *,
    bap_id: str,
    bap_uri: str,
    bpp_id: str,
    bpp_uri: str,
    transaction_id: Optional[str] = None,
    message_id: Optional[str] = None,
    order_id: str,
    settlement_basis: str,
    settlement_window: str,
    settlement_id: Optional[str] = None,
    country_code: str = "IND",
    city_code: str = "std:080",
    core_version: str = "1.1.0",
) -> dict[str, Any]:
    """Build an ONDC /settle envelope for the BAP to POST to the BPP.

    Args:
        bap_id / bap_uri: the BAP's subscriber identity (signs the request).
        bpp_id / bpp_uri: the destination BPP.
        transaction_id: re-use the order's Beckn transaction_id where
            possible so the BPP can correlate; auto-generated if absent.
        message_id: idempotency key; auto-generated if absent.
        order_id: the BPP order id this settlement record references.
        settlement_basis: one of :data:`SETTLEMENT_BASES`.
        settlement_window: one of :data:`SETTLEMENT_WINDOWS` (P1D / P3D /
            P7D ISO 8601 duration codes).
        settlement_id: pre-assigned settlement record UUID (override for
            tests / retries).
        country_code / city_code / core_version: Beckn context fields.

    Returns:
        A signed-ready Beckn envelope ``{context, message: {settlement}}``.

    Raises:
        ValueError: if settlement_basis / window is not in the allow-list
            (we never emit unknown ONDC codes — protocol data is grounded).
    """
    if settlement_basis not in SETTLEMENT_BASES:
        raise ValueError(
            f"unknown RSP settlement_basis {settlement_basis!r}; "
            f"allowed: {sorted(SETTLEMENT_BASES)}"
        )
    if settlement_window not in SETTLEMENT_WINDOWS:
        raise ValueError(
            f"unknown RSP settlement_window {settlement_window!r}; "
            f"allowed: {sorted(SETTLEMENT_WINDOWS)}"
        )

    settlement_obj_id = settlement_id or _new_id()
    now = _now()

    settlement: dict[str, Any] = {
        "id": settlement_obj_id,
        "order_id": order_id,
        "settlement_basis": settlement_basis,
        "settlement_window": {"duration": settlement_window},
        "settlement_status": "NOT_PAID",
        "counterparties": [],
        "created_at": now,
        "updated_at": now,
    }

    return {
        "context": {
            "domain": resolve_ondc_domain(bpp_id).domain_code,
            "country": country_code,
            "city": city_code,
            "action": "settle",
            "core_version": core_version,
            "bap_id": bap_id,
            "bap_uri": bap_uri,
            "bpp_id": bpp_id,
            "bpp_uri": bpp_uri,
            "transaction_id": transaction_id or _new_id(),
            "message_id": message_id or _new_id(),
            "timestamp": now,
        },
        "message": {"settlement": settlement},
    }


def build_on_settle_envelope(
    *,
    bap_id: str,
    bap_uri: str,
    bpp_id: str,
    bpp_uri: str,
    transaction_id: str,
    message_id: Optional[str] = None,
    settlement_id: str,
    order_id: str,
    settlement_basis: str,
    settlement_window: str,
    settlement_status: str,
    settlement_reference: Optional[str] = None,
    settlement_type: Optional[str] = None,
    counterparties: Optional[list[dict[str, Any]]] = None,
    country_code: str = "IND",
    city_code: str = "std:080",
    core_version: str = "1.1.0",
) -> dict[str, Any]:
    """Build an ONDC /on_settle envelope for the BPP to POST back to the BAP.

    Args:
        bap_id / bap_uri: the destination BAP.
        bpp_id / bpp_uri: the BPP's subscriber identity (signs the response).
        transaction_id: MUST be the same transaction_id as the inbound
            /settle (ONDC requires correlated callbacks).
        message_id: this response's own idempotency key; auto-generated if
            absent.
        settlement_id: echo the settlement record id from the inbound /settle.
        order_id: the BPP order id (echoed from /settle).
        settlement_basis: one of SETTLEMENT_BASES.
        settlement_window: one of SETTLEMENT_WINDOWS.
        settlement_status: one of SETTLEMENT_STATUSES (BPP fills in real
            status — typically NOT_PAID at /on_settle time and later
            transitions to PAID / PARTIAL_PAID via operator action).
        settlement_reference: BPP-side ledger id for correlation.
        settlement_type: optional preferred rail.
        counterparties: per-counterparty payable breakdown
            ``[{type, id, uri?, amount, currency?}, ...]``.
        country_code / city_code / core_version: Beckn context fields.

    Returns:
        A signed-ready Beckn envelope ``{context, message: {settlement}}``.

    Raises:
        ValueError: if any of the basis / window / status / type codes
            is not in the allow-list.
    """
    if settlement_basis not in SETTLEMENT_BASES:
        raise ValueError(
            f"unknown RSP settlement_basis {settlement_basis!r}; "
            f"allowed: {sorted(SETTLEMENT_BASES)}"
        )
    if settlement_window not in SETTLEMENT_WINDOWS:
        raise ValueError(
            f"unknown RSP settlement_window {settlement_window!r}; "
            f"allowed: {sorted(SETTLEMENT_WINDOWS)}"
        )
    if settlement_status not in SETTLEMENT_STATUSES:
        raise ValueError(
            f"unknown RSP settlement_status {settlement_status!r}; "
            f"allowed: {sorted(SETTLEMENT_STATUSES)}"
        )
    if settlement_type is not None and settlement_type not in SETTLEMENT_TYPES:
        raise ValueError(
            f"unknown RSP settlement_type {settlement_type!r}; "
            f"allowed: {sorted(SETTLEMENT_TYPES)}"
        )

    now = _now()
    settlement: dict[str, Any] = {
        "id": settlement_id,
        "order_id": order_id,
        "settlement_basis": settlement_basis,
        "settlement_window": {"duration": settlement_window},
        "settlement_status": settlement_status,
        "counterparties": list(counterparties or []),
        "updated_at": now,
    }
    if settlement_reference is not None:
        settlement["settlement_reference"] = settlement_reference
    if settlement_type is not None:
        settlement["settlement_type"] = settlement_type

    return {
        "context": {
            "domain": resolve_ondc_domain(bpp_id).domain_code,
            "country": country_code,
            "city": city_code,
            "action": "on_settle",
            "core_version": core_version,
            "bap_id": bap_id,
            "bap_uri": bap_uri,
            "bpp_id": bpp_id,
            "bpp_uri": bpp_uri,
            "transaction_id": transaction_id,
            "message_id": message_id or _new_id(),
            "timestamp": now,
        },
        "message": {"settlement": settlement},
    }
