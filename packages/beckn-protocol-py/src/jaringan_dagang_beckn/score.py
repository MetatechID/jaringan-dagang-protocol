"""ONDC Score (reputation) message shapes + deterministic band mapping.

Scope is intentionally narrow (YAGNI per Task A6):

* :class:`ScoreAttribute` — one per-attribute reputation signal (completion
  rate, return rate, response time, resolution time, rating avg).
* :class:`Score` — the rolled-up Score record per provider per period.
* :func:`compute_score_band` — deterministic mapping from per-attribute
  values onto the SCORE_BANDS bucket (mirrors score.yaml thresholds).

A6 v1 is BPP-side ONLY — each BPP computes its own Score locally over a
daily window and persists the snapshot. There is no inter-NP /score
envelope yet; the downstream consumption (Score-weighted /search ranking,
public reputation surfaces) is deferred to v2.

The codes in ``score.attributes[].name`` and ``score.band`` are localized
in ``jaringan-dagang-network/network-extension/enums/score.yaml`` (network
layer source of truth). This module is a thin typed projection of those
codes plus the deterministic compute_score_band threshold-tree.

Grounding (codes are NOT invented here — they mirror upstream ONDC Score):

* Score attribute / band shape:
  ONDC-Official/protocol-network-extension @ release-1.0.0
  ``enums/score/score_attribute.yaml`` / ``score_band.yaml``.

This module lives in the shared ``beckn-protocol`` package and is
vendored byte-identically into the seller and buyer repos AND into the
buyer's ``apps/beli-aman-bap/beckn_protocol/`` Vercel-package copy.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums (allow-lists mirror network-extension/enums/score.yaml).
# ---------------------------------------------------------------------------

SCORE_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "COMPLETION_RATE",
        "RETURN_RATE",
        "RESPONSE_TIME",
        "RESOLUTION_TIME",
        "RATING_AVG",
    }
)
SCORE_BANDS: tuple[str, ...] = ("EXCELLENT", "GOOD", "FAIR", "POOR")

# ScoreAttributeWeights: relative importance of each attribute when a
# downstream consumer rolls up multiple attributes into a single weighted
# Score. v1 doesn't have a downstream consumer yet (band is decided by a
# threshold tree, not a weighted sum) but the constant ships so v2 can
# adopt without churning the protocol module.
ScoreAttributeWeights = {
    "COMPLETION_RATE": 0.35,
    "RETURN_RATE": 0.25,
    "RESPONSE_TIME": 0.15,
    "RESOLUTION_TIME": 0.15,
    "RATING_AVG": 0.10,
}

__all__ = [
    "SCORE_ATTRIBUTES",
    "SCORE_BANDS",
    "ScoreAttributeWeights",
    "ScoreAttribute",
    "Score",
    "compute_score_band",
]


# ---------------------------------------------------------------------------
# Wire-shape pydantic models.
# ---------------------------------------------------------------------------


class ScoreAttribute(BaseModel):
    """One per-attribute reputation signal over a fixed window.

    ``value`` units depend on ``name``: COMPLETION_RATE / RETURN_RATE are
    ratios in [0.0, 1.0]; RESPONSE_TIME / RESOLUTION_TIME are mean hours;
    RATING_AVG is a 1.0..5.0 average.
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(
        ...,
        description=(
            "One of SCORE_ATTRIBUTES (COMPLETION_RATE / RETURN_RATE / "
            "RESPONSE_TIME / RESOLUTION_TIME / RATING_AVG)."
        ),
    )
    value: float = Field(
        ...,
        description="Attribute value (units depend on ``name``).",
    )
    period_start: Optional[datetime] = Field(
        default=None, description="Window start (UTC)."
    )
    period_end: Optional[datetime] = Field(
        default=None, description="Window end (UTC)."
    )


class Score(BaseModel):
    """Rolled-up Score record per provider per period.

    v1 is BPP-local — each BPP persists its own Score snapshots; there is
    no inter-NP /score envelope yet.
    """

    model_config = ConfigDict(populate_by_name=True)

    provider_id: str = Field(
        ...,
        description=(
            "Subscriber id of the BPP whose reputation this snapshot "
            "describes. Canonical scheme: ``<slug>.jaringan-dagang.id``."
        ),
    )
    attributes: list[ScoreAttribute] = Field(
        ...,
        description="Per-attribute signals computed for the period.",
    )
    band: str = Field(
        ...,
        description=(
            "Headline reputation bucket (one of SCORE_BANDS). Computed "
            "deterministically by :func:`compute_score_band` from the "
            "attribute values."
        ),
    )
    computed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When this snapshot was computed.",
    )


# ---------------------------------------------------------------------------
# Deterministic band mapping. Mirrors network-extension/enums/score.yaml
# thresholds. Re-deriving from the yaml at runtime would add a yaml parse
# dep to the protocol package; we keep the constants in lockstep with the
# yaml manually (the yaml is the source of truth — if you adjust this
# function, adjust the yaml first).
#
# Threshold model:
#   * EXCELLENT requires ALL of (completion >= 95% AND return <= 5% AND
#     rating >= 4.5)
#   * GOOD requires ALL of (completion >= 85% AND return <= 10% AND
#     rating >= 4.0)
#   * FAIR requires ALL of (completion >= 70% AND return <= 20% AND
#     rating >= 3.0)
#   * Otherwise POOR.
#
# RATING_AVG defaults to 0.0 if there are no ratings (which currently
# floors all stores at POOR until they have any rating data). When you
# add rating ingest in v2, change the fallback to "skip the rating
# threshold" instead of "fail it".
# ---------------------------------------------------------------------------


def compute_score_band(
    *,
    completion_rate: float,
    return_rate: float,
    rating_avg: float = 0.0,
    response_hours: Optional[float] = None,   # noqa: ARG001 - reserved for v2
    resolution_hours: Optional[float] = None, # noqa: ARG001 - reserved for v2
) -> str:
    """Map per-attribute values onto a SCORE_BANDS bucket.

    Args:
        completion_rate: fraction of orders that reached delivered
            without cancellation, in [0.0, 1.0].
        return_rate: fraction of delivered orders that ended with a
            refund / return, in [0.0, 1.0].
        rating_avg: post-fulfillment rating average in [0.0, 5.0]. v1
            defaults to 0.0 when no ratings exist; tighten in v2.
        response_hours / resolution_hours: reserved for v2 — the yaml
            doesn't enumerate thresholds for these yet (the headline
            band currently only fires on completion/return/rating).

    Returns:
        One of ``"EXCELLENT" / "GOOD" / "FAIR" / "POOR"``.
    """
    # v1 keeps RATING_AVG as a "must >= threshold" filter; if the BPP has
    # zero ratings yet (default 0.0), they fall to POOR. The yaml's GOOD
    # threshold (4.0) is the binding constraint for new BPPs; v2 will
    # introduce "no-rating waiver" so a perfect completion / no-return
    # BPP isn't punished for being too new to have ratings.
    if (
        completion_rate >= 0.95
        and return_rate <= 0.05
        and rating_avg >= 4.5
    ):
        return "EXCELLENT"
    if (
        completion_rate >= 0.85
        and return_rate <= 0.10
        and rating_avg >= 4.0
    ):
        return "GOOD"
    if (
        completion_rate >= 0.70
        and return_rate <= 0.20
        and rating_avg >= 3.0
    ):
        return "FAIR"
    return "POOR"
