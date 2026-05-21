"""ONDC ``@ondc/org/...`` tag builders for the RET11 (Packaged F&B) flow.

The base Beckn ``Tag`` model (``catalog.py``) carries categorized metadata
as ``{code, list:[{code, value}]}``. The Indonesian ONDC localization
layer rides on top of Beckn (see ``domain_resolver``): ONDC adds
``@ondc/org/...`` namespaced tag groups to items, fulfillments and
payments. This module produces those groups *as the existing Beckn
``Tag``/``TagValue`` models* so the seller (BPP) and buyer (BAP) emit /
parse an identical structure without forking the protocol models.

Scope is intentionally limited to what ONDC:RET11 search / select /
confirm needs (YAGNI -- no IGM, no settlement reconciliation, no
order-flow tags):

* item statutory / packaged-commodity attribute groups
  (``@ondc/org/statutory_reqs_packaged_commodities`` /
  ``@ondc/org/statutory_reqs_prepackaged_food``)
* fulfillment delivery-terms tag (``delivery_terms`` / incoterms)
* payment settlement-terms tag (``settlement_terms``)

Grounding (codes are NOT invented here -- they mirror upstream ONDC):

* statutory sub-keys & ``@ondc/org`` item keys: ONDC-Official/
  ONDC-RET-Specifications @ ``draft-b2c-1.2.5``
  ``api/components/examples/B2C{,-F&B}/.../02_on_search*.yaml``
* incoterms enum: ONDC-Official/ONDC-RET-Specifications @
  ``release-2.0.2`` ``api/components/enums/fulfillments.yaml``
  (``tags.delivery_terms.incoterms``)
* settlement-terms keys: same repo @ ``draft-b2c-1.2.5``
  ``examples/B2C/bpp-collect-flow/.../09_on_confirm.yaml``
  (``@ondc/org/settlement_basis|settlement_window|withholding_amount|
  buyer_app_finder_fee_type|buyer_app_finder_fee_amount``)

The localized human labels for these codes live in the network ONDC
extension at ``network-extension/enums/retail.yaml`` (source of truth);
this module is a thin typed projection of the *codes* the wire needs,
not a YAML loader (the network extension is a separate repo not on this
service's import path -- same rationale as ``domain_resolver``).

This module lives in the shared ``beckn-protocol`` package and is
vendored byte-identically into the seller and buyer repos.
"""

from __future__ import annotations

from typing import Mapping, Optional

from .catalog import Tag, TagValue

__all__ = [
    "ONDC_ORG_STATUTORY_PACKAGED_COMMODITIES",
    "ONDC_ORG_STATUTORY_PREPACKAGED_FOOD",
    "PACKAGED_COMMODITIES_KEYS",
    "PREPACKAGED_FOOD_KEYS",
    "INCOTERMS",
    "SETTLEMENT_BASIS",
    "build_item_statutory_tags",
    "build_fulfillment_ondc_tags",
    "build_payment_settlement_tags",
]

# --- ONDC namespaced group codes -------------------------------------------

ONDC_ORG_STATUTORY_PACKAGED_COMMODITIES = (
    "@ondc/org/statutory_reqs_packaged_commodities"
)
ONDC_ORG_STATUTORY_PREPACKAGED_FOOD = "@ondc/org/statutory_reqs_prepackaged_food"

# Ordered allow-lists of the ONDC-standard sub-attribute codes for each
# statutory group. Ordering is preserved on the wire (deterministic
# output). We refuse unknown keys rather than silently emitting non-ONDC
# data -- protocol data must be grounded, never fabricated.
PACKAGED_COMMODITIES_KEYS: tuple[str, ...] = (
    "manufacturer_or_packer_name",
    "manufacturer_or_packer_address",
    "common_or_generic_name_of_commodity",
    "net_quantity_or_measure_of_commodity_in_pkg",
    "month_year_of_manufacture_packing_import",
)
PREPACKAGED_FOOD_KEYS: tuple[str, ...] = (
    "nutritional_info",
    "additives_info",
    "brand_owner_FSSAI_license_no",
    "other_FSSAI_license_no",
    "importer_FSSAI_license_no",
)

# Beckn/ONDC RET 2.0.2 fulfillments.yaml tags.delivery_terms.incoterms.
INCOTERMS: frozenset[str] = frozenset({"CIF", "EXW", "FOB", "DAP", "DDP"})

# ONDC RET settlement basis values (payment.@ondc/org/settlement_basis).
SETTLEMENT_BASIS: frozenset[str] = frozenset(
    {"delivery", "shipment", "return_window_expiry"}
)

# Ordered settlement-terms sub-keys (mirrors the @ondc/org payment keys).
_SETTLEMENT_KEYS: tuple[str, ...] = (
    "settlement_basis",
    "settlement_window",
    "withholding_amount",
    "buyer_app_finder_fee_type",
    "buyer_app_finder_fee_amount",
)


def _tag(code: str, pairs: list[tuple[str, str]]) -> Tag:
    """Build one Beckn ``Tag`` group from ordered (code, value) pairs.

    Emits the canonical flat ONDC tag-list shape
    ``{code, list:[{code, value}]}`` -- the ``code`` field on each
    ``TagValue`` carries the ONDC sub-attribute key.
    """
    return Tag(
        code=code,
        list=[TagValue(code=k, value=str(v)) for k, v in pairs],
    )


def _filtered_pairs(
    values: Optional[Mapping[str, object]],
    allowed: tuple[str, ...],
    group_label: str,
) -> list[tuple[str, str]]:
    """Validate keys against the ONDC allow-list, drop ``None`` values.

    Order follows ``allowed`` (deterministic wire output). Unknown keys
    raise ``ValueError`` -- we never emit non-ONDC sub-codes.
    """
    if not values:
        return []
    unknown = set(values) - set(allowed)
    if unknown:
        raise ValueError(
            f"{group_label}: unknown ONDC sub-key(s) {sorted(unknown)}; "
            f"allowed: {list(allowed)}"
        )
    return [(k, values[k]) for k in allowed if values.get(k) is not None]


def build_item_statutory_tags(
    packaged_commodities: Optional[Mapping[str, object]] = None,
    prepackaged_food: Optional[Mapping[str, object]] = None,
) -> list[Tag]:
    """Build the ONDC item statutory tag groups for a packaged-F&B item.

    Args:
        packaged_commodities: sub-key -> value map for the
            ``@ondc/org/statutory_reqs_packaged_commodities`` group. Keys
            must be in :data:`PACKAGED_COMMODITIES_KEYS`. ``None`` values
            are skipped.
        prepackaged_food: sub-key -> value map for the
            ``@ondc/org/statutory_reqs_prepackaged_food`` group. Keys must
            be in :data:`PREPACKAGED_FOOD_KEYS`.

    Returns:
        A list with 0, 1 or 2 :class:`Tag` groups (only non-empty groups
        are emitted), in packaged-commodities-then-prepackaged-food order.

    Raises:
        ValueError: if any supplied sub-key is not an ONDC-standard code.
    """
    tags: list[Tag] = []
    pkg = _filtered_pairs(
        packaged_commodities,
        PACKAGED_COMMODITIES_KEYS,
        ONDC_ORG_STATUTORY_PACKAGED_COMMODITIES,
    )
    if pkg:
        tags.append(_tag(ONDC_ORG_STATUTORY_PACKAGED_COMMODITIES, pkg))
    food = _filtered_pairs(
        prepackaged_food,
        PREPACKAGED_FOOD_KEYS,
        ONDC_ORG_STATUTORY_PREPACKAGED_FOOD,
    )
    if food:
        tags.append(_tag(ONDC_ORG_STATUTORY_PREPACKAGED_FOOD, food))
    return tags


def build_fulfillment_ondc_tags(
    incoterms: Optional[str] = None,
    named_place: Optional[str] = None,
) -> list[Tag]:
    """Build the ONDC fulfillment ``delivery_terms`` tag group.

    Args:
        incoterms: one of :data:`INCOTERMS` (RET 2.0.2 delivery terms).
        named_place: the named place of delivery (free text).

    Returns:
        ``[]`` if nothing supplied, else a single ``delivery_terms``
        :class:`Tag`.

    Raises:
        ValueError: if ``incoterms`` is not an ONDC incoterm code.
    """
    pairs: list[tuple[str, str]] = []
    if incoterms is not None:
        if incoterms not in INCOTERMS:
            raise ValueError(
                f"invalid incoterm {incoterms!r}; allowed: {sorted(INCOTERMS)}"
            )
        pairs.append(("incoterms", incoterms))
    if named_place is not None:
        pairs.append(("named_place_of_delivery", named_place))
    if not pairs:
        return []
    return [_tag("delivery_terms", pairs)]


def build_payment_settlement_tags(
    settlement_basis: Optional[str] = None,
    settlement_window: Optional[str] = None,
    withholding_amount: Optional[str] = None,
    buyer_app_finder_fee_type: Optional[str] = None,
    buyer_app_finder_fee_amount: Optional[str] = None,
) -> list[Tag]:
    """Build the ONDC payment ``settlement_terms`` tag group.

    Mirrors the ``@ondc/org/settlement_*`` /
    ``@ondc/org/buyer_app_finder_fee_*`` payment keys as a single Beckn
    tag group. Only supplied (non-``None``) terms are emitted, in a
    deterministic order.

    Raises:
        ValueError: if ``settlement_basis`` is not an ONDC value.
    """
    if settlement_basis is not None and settlement_basis not in SETTLEMENT_BASIS:
        raise ValueError(
            f"invalid settlement_basis {settlement_basis!r}; "
            f"allowed: {sorted(SETTLEMENT_BASIS)}"
        )
    supplied = {
        "settlement_basis": settlement_basis,
        "settlement_window": settlement_window,
        "withholding_amount": withholding_amount,
        "buyer_app_finder_fee_type": buyer_app_finder_fee_type,
        "buyer_app_finder_fee_amount": buyer_app_finder_fee_amount,
    }
    pairs = [(k, supplied[k]) for k in _SETTLEMENT_KEYS if supplied[k] is not None]
    if not pairs:
        return []
    return [_tag("settlement_terms", pairs)]
