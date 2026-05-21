"""Beckn protocol rating models + ONDC /rating envelope builders.

Models for submitting and receiving ratings in the Beckn protocol.
Ratings can apply to items, providers, fulfillments, or the overall order.

Task A6 extends this module with envelope builders for the buyer-emitted
``/rating`` action and the BPP-emitted ``/on_rating`` ack. The existing
:class:`Rating` and :class:`RatingCategory` types are unchanged.

Grounding (codes are NOT invented here — they mirror upstream ONDC):

* Rating category set:
  ONDC-Official/ONDC-RET-Specifications @ release-2.0.2
  ``api/components/enums/rating_category.yaml``.
* Rating envelope shape:
  ONDC-Official/ONDC-RET-Specifications @ release-2.0.2
  ``api/components/schemas/Rating.yaml`` +
  ``specifications/rating.yaml`` / ``on_rating.yaml``.

This module lives in the shared ``beckn-protocol`` package and is
vendored byte-identically into the seller and buyer repos AND into the
buyer's ``apps/beli-aman-bap/beckn_protocol/`` Vercel-package copy.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from .catalog import Tag
from .domain_resolver import resolve_ondc_domain

# ---------------------------------------------------------------------------
# Enums (allow-list mirrors network-extension/enums/rating.yaml).
# ---------------------------------------------------------------------------

RATING_CATEGORIES: frozenset[str] = frozenset(
    {"Item", "Order", "Fulfillment", "Provider", "Agent"}
)


class RatingCategory(str, Enum):
    """What entity is being rated."""

    ITEM = "Item"
    ORDER = "Order"
    FULFILLMENT = "Fulfillment"
    PROVIDER = "Provider"
    AGENT = "Agent"


class Rating(BaseModel):
    """A rating submitted by the buyer or acknowledged by the seller.

    Used in the `rating` action (buyer -> BAP -> BPP) and
    `on_rating` callback (BPP -> BAP).
    """

    model_config = {"populate_by_name": True}

    id: Optional[str] = Field(
        default=None,
        description="Rating ID (assigned by BPP on acknowledgment)",
    )
    rating_category: Optional[RatingCategory] = Field(
        default=None,
        description="The type of entity being rated",
    )
    value: Optional[str] = Field(
        default=None,
        description="Rating value as string (e.g. '4', '4.5')",
    )
    # The ID of the entity being rated
    id_ref: Optional[str] = Field(
        default=None,
        description="ID of the entity being rated (item_id, provider_id, etc.)",
    )
    feedback_form: Optional[list[dict[str, str]]] = Field(
        default=None,
        description="Structured feedback form responses",
    )
    feedback_id: Optional[str] = Field(
        default=None,
        description="Reference to a feedback form template",
    )
    tags: Optional[list[Tag]] = Field(
        default=None,
        description="Additional rating metadata",
    )


__all__ = [
    "RATING_CATEGORIES",
    "Rating",
    "RatingCategory",
    "build_rating_envelope",
    "build_on_rating_envelope",
]


# ---------------------------------------------------------------------------
# Envelope builders (Task A6). Mirror the IGM/RSP builder style: pure
# functions returning a plain dict that the caller signs + POSTs.
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _validate_rating_value(value: str) -> None:
    """Reject rating values outside the 1.0-5.0 range.

    ONDC accepts any numeric string in the envelope but defines 1-5 as the
    valid range; we enforce that here so /rating envelopes never carry
    nonsense values that BPPs would 70001-error on anyway.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"rating value {value!r} is not a parseable number"
        )
    if not (1.0 <= v <= 5.0):
        raise ValueError(
            f"rating value {v} is outside the allowed 1..5 range"
        )


def build_rating_envelope(
    *,
    bap_id: str,
    bap_uri: str,
    bpp_id: str,
    bpp_uri: str,
    transaction_id: Optional[str] = None,
    message_id: Optional[str] = None,
    order_id: str,
    ratings: list[dict[str, Any]],
    country_code: str = "IND",
    city_code: str = "std:080",
    core_version: str = "1.1.0",
) -> dict[str, Any]:
    """Build an ONDC /rating envelope for the BAP to POST to the BPP.

    Args:
        bap_id / bap_uri: the BAP's subscriber identity (signs the request).
        bpp_id / bpp_uri: the destination BPP.
        transaction_id: re-use the order's Beckn transaction_id where
            possible; auto-generated if absent.
        message_id: idempotency key; auto-generated if absent.
        order_id: the BPP order id being rated.
        ratings: list of ``{rating_category, value, id?, comments?}`` dicts.
            * ``rating_category`` must be in :data:`RATING_CATEGORIES`.
            * ``value`` must be a numeric string in [1.0, 5.0].
            * ``id`` is the id of the rated entity (item_id, provider_id);
              omitted for Order-level ratings.
            * ``comments`` (optional) is mapped into a tag.
        country_code / city_code / core_version: Beckn context fields.

    Returns:
        A signed-ready Beckn envelope ``{context, message: {ratings, id}}``.

    Raises:
        ValueError: if any rating fails validation (we never emit unknown
            ONDC codes — protocol data is grounded).
    """
    if not ratings:
        raise ValueError("ratings list is empty")

    out_ratings: list[dict[str, Any]] = []
    for r in ratings:
        cat = r.get("rating_category") or r.get("category")
        val = r.get("value")
        if cat not in RATING_CATEGORIES:
            raise ValueError(
                f"unknown rating_category {cat!r}; "
                f"allowed: {sorted(RATING_CATEGORIES)}"
            )
        if val is None:
            raise ValueError("rating value is required")
        val_str = str(val)
        _validate_rating_value(val_str)
        entry: dict[str, Any] = {
            "rating_category": cat,
            "value": val_str,
        }
        if r.get("id"):
            entry["id"] = r["id"]
        comments = r.get("comments")
        if comments:
            entry["tags"] = [
                {
                    "code": "comments",
                    "list": [{"code": "comments", "value": str(comments)}],
                }
            ]
        out_ratings.append(entry)

    now = _now()
    return {
        "context": {
            "domain": resolve_ondc_domain(bpp_id).domain_code,
            "country": country_code,
            "city": city_code,
            "action": "rating",
            "core_version": core_version,
            "bap_id": bap_id,
            "bap_uri": bap_uri,
            "bpp_id": bpp_id,
            "bpp_uri": bpp_uri,
            "transaction_id": transaction_id or _new_id(),
            "message_id": message_id or _new_id(),
            "timestamp": now,
        },
        "message": {
            "id": order_id,
            "ratings": out_ratings,
        },
    }


def build_on_rating_envelope(
    *,
    bap_id: str,
    bap_uri: str,
    bpp_id: str,
    bpp_uri: str,
    transaction_id: str,
    message_id: Optional[str] = None,
    feedback_acknowledged: bool = True,
    feedback_form_id: Optional[str] = None,
    country_code: str = "IND",
    city_code: str = "std:080",
    core_version: str = "1.1.0",
) -> dict[str, Any]:
    """Build an ONDC /on_rating envelope for the BPP to POST back to the BAP.

    Args:
        bap_id / bap_uri: the destination BAP.
        bpp_id / bpp_uri: the BPP's subscriber identity (signs the response).
        transaction_id: MUST be the same transaction_id as the inbound
            /rating (ONDC requires correlated callbacks).
        message_id: this response's own idempotency key; auto-generated if
            absent.
        feedback_acknowledged: whether the BPP accepted the ratings. v1
            BPPs always return True (we persist; consumption is deferred).
        feedback_form_id: optional follow-up structured feedback form id
            the BPP wants the buyer to fill (forward-compat; v1 omits).
        country_code / city_code / core_version: Beckn context fields.

    Returns:
        A signed-ready Beckn envelope ``{context, message: {feedback_ack}}``.
    """
    now = _now()
    msg: dict[str, Any] = {
        "feedback_ack": bool(feedback_acknowledged),
    }
    if feedback_form_id is not None:
        msg["feedback_form_id"] = feedback_form_id

    return {
        "context": {
            "domain": resolve_ondc_domain(bpp_id).domain_code,
            "country": country_code,
            "city": city_code,
            "action": "on_rating",
            "core_version": core_version,
            "bap_id": bap_id,
            "bap_uri": bap_uri,
            "bpp_id": bpp_id,
            "bpp_uri": bpp_uri,
            "transaction_id": transaction_id,
            "message_id": message_id or _new_id(),
            "timestamp": now,
        },
        "message": msg,
    }
