"""Per-store ONDC domain resolver.

Beckn transports messages on a transport-level ``domain`` string. Indonesia
runs an ONDC-style localization layer on top of the base Beckn domains: the
network ONDC extension at
``jaringan-dagang-network/network-extension/domains/retail.yaml`` defines
``domain.code: ONDC:RET`` (Retail) with packaged-goods sub-domains, e.g.::

    - code: "ONDC:RET11"          # F&B (Packaged)
      beckn_domain: "nic2004:52110"

This module maps a *store* (by ``subscriber_id`` or slug) to the ONDC domain
code it should transact on, while preserving the underlying Beckn transport
base. It is intentionally **multi-tenant**: there is no global domain
constant — each store resolves its own code so additional sellers (e.g. a
future grocery or fashion store) only need a new mapping entry, not a code
change.

This is a thin, typed projection of ``retail.yaml`` rather than a runtime
YAML loader: the network extension is a separate repo not on this service's
import path, and neither service depends on a YAML parser. When the taxonomy
in ``retail.yaml`` changes, update :data:`_STORE_DOMAINS` / the constants
below to match (the YAML remains the source of truth).

This module lives in the shared ``beckn-protocol`` package so the seller
(BPP) and buyer (BAP) consume an identical resolver.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "OndcDomain",
    "ONDC_RETAIL_BECKN_BASE",
    "DEFAULT_ONDC_DOMAIN",
    "resolve_ondc_domain",
]

# Base Beckn transport domain for the ONDC:RET (Retail) family. Every retail
# sub-domain in retail.yaml that we support today maps onto this base; the
# ONDC code layer added here does NOT change this transport base.
ONDC_RETAIL_BECKN_BASE = "nic2004:52110"


@dataclass(frozen=True)
class OndcDomain:
    """An ONDC domain code paired with its Beckn transport base.

    Attributes:
        domain_code: ONDC domain/sub-domain code, e.g. ``"ONDC:RET11"``
            (the value placed in the Beckn context ``domain`` field).
        beckn_domain: Underlying Beckn transport domain, e.g.
            ``"nic2004:52110"`` — the spec-level retail base, unchanged by
            the ONDC code layer.
    """

    domain_code: str
    beckn_domain: str


# Store-level retail default used for unknown / unspecified stores. We know
# the network is the Indonesian retail network (ONDC:RET, base nic2004:52110)
# but cannot infer a store's catalogue mix, so we fall back to the
# *store-level* domain code rather than guessing a sub-domain.
DEFAULT_ONDC_DOMAIN = OndcDomain(
    domain_code="ONDC:RET",
    beckn_domain=ONDC_RETAIL_BECKN_BASE,
)

# ONDC:RET11 — "F&B (Packaged)" / "Makanan & Minuman Kemasan"
# (retail.yaml sub_domains). Safiya & Matchamu sell packaged food & beverages.
_ONDC_RET11_PACKAGED_FNB = OndcDomain(
    domain_code="ONDC:RET11",
    beckn_domain=ONDC_RETAIL_BECKN_BASE,
)

# ONDC:RET12 — "Fashion" / "Fashion & Pakaian" (retail.yaml sub_domains).
# Antarestar sells jackets / daypacks.
_ONDC_RET12_FASHION = OndcDomain(
    domain_code="ONDC:RET12",
    beckn_domain=ONDC_RETAIL_BECKN_BASE,
)

# ONDC:RET15 — "Health & Beauty" / "Kesehatan & Kecantikan" (retail.yaml
# sub_domains). Gendes sells feminine care; Optimum Nutrition sells
# supplements (vitamin / suplemen in the retail.yaml examples list).
_ONDC_RET15_HEALTH_BEAUTY = OndcDomain(
    domain_code="ONDC:RET15",
    beckn_domain=ONDC_RETAIL_BECKN_BASE,
)

# Per-store mapping keyed by normalized store identifier (subscriber_id or
# slug, lower-cased & trimmed). Multi-tenant: add a row per onboarded store.
#
# Canonical subscriber_id scheme: ``<slug>.jaringan-dagang.id`` (Task A3).
# Legacy ``bpp.*.local`` / ``*.bpp.metatech.id`` identifiers are intentionally
# NOT in the table — they fall back to the store-level retail default. The
# live DB is being migrated to canonical via
# ``jaringan-dagang-seller/scripts/migrate-subscriber-ids.py``.
#
# YourBrand is the white-label demo toko (no real catalogue mix); it
# deliberately stays on the store-level retail default rather than being
# mapped to a specific sub-domain.
_STORE_DOMAINS: dict[str, OndcDomain] = {
    # Safiya — live packaged-F&B seller.
    "safiyafood.jaringan-dagang.id": _ONDC_RET11_PACKAGED_FNB,
    "safiyafood": _ONDC_RET11_PACKAGED_FNB,
    # Matchamu — premium matcha & powder beverages (packaged F&B).
    "matchamu.jaringan-dagang.id": _ONDC_RET11_PACKAGED_FNB,
    "matchamu": _ONDC_RET11_PACKAGED_FNB,
    # Antarestar — outdoor fashion (jackets, daypacks).
    "antarestar.jaringan-dagang.id": _ONDC_RET12_FASHION,
    "antarestar": _ONDC_RET12_FASHION,
    # Gendes — feminine-care wash / spray (health & beauty).
    "gendes.jaringan-dagang.id": _ONDC_RET15_HEALTH_BEAUTY,
    "gendes": _ONDC_RET15_HEALTH_BEAUTY,
    # Optimum Nutrition — sports supplements (whey, BCAA, vitamin).
    "optimumnutrition.jaringan-dagang.id": _ONDC_RET15_HEALTH_BEAUTY,
    "optimumnutrition": _ONDC_RET15_HEALTH_BEAUTY,
}


def _normalize(store_identifier: str | None) -> str | None:
    """Lower-case and trim a store identifier; ``None`` if not usable."""
    if not store_identifier:
        return None
    normalized = store_identifier.strip().lower()
    return normalized or None


def resolve_ondc_domain(store_identifier: str | None) -> OndcDomain:
    """Resolve a store to the ONDC domain it should transact on.

    Args:
        store_identifier: The store's Beckn ``subscriber_id``
            (e.g. ``"safiyafood.jaringan-dagang.id"``) or its slug
            (e.g. ``"safiyafood"``). Matching is case-insensitive and
            whitespace-trimmed. ``None``/empty is allowed.

    Returns:
        The matching :class:`OndcDomain`, or :data:`DEFAULT_ONDC_DOMAIN`
        (store-level ``ONDC:RET``) for unknown or unspecified stores. The
        Beckn transport base is always preserved.
    """
    key = _normalize(store_identifier)
    if key is None:
        return DEFAULT_ONDC_DOMAIN
    return _STORE_DOMAINS.get(key, DEFAULT_ONDC_DOMAIN)
