"""beckn_protocol vendored-copy invariant.

The buyer repo has TWO Beckn protocol source trees, by design:

* ``packages/beckn-protocol/python/`` — canonical upstream, also used by
  other apps in the monorepo.
* ``apps/beli-aman-bap/beckn_protocol/``  — vendored copy, present
  because the Vercel project's ``rootDirectory=apps/beli-aman-bap``
  excludes anything outside that directory from the deployed function.

This was attempted as a pure re-export shim in commit ``3df4fea`` and
shipped to production in commit ``8c9c98f``. The shim worked locally
(repo root reachable) but broke every Vercel deploy: the canonical path
``/var/task/../../packages/beckn-protocol`` does not exist at runtime,
so module-import raised ``ImportError`` and every endpoint returned
``FUNCTION_INVOCATION_FAILED`` (which the browser surfaces as a CORS
error, because the failed function emits no headers).

The fix is to vendor the canonical files in-tree under
``beckn_protocol/``. This test guards that the in-tree vendored copy
stays byte-identical to the canonical upstream, so behaviour cannot
silently drift.
"""

import filecmp
import importlib
import os
import sys

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

_VENDORED_DIR = os.path.join(_BAP_DIR, "beckn_protocol")
_CANONICAL_DIR = os.path.abspath(
    os.path.join(_BAP_DIR, "..", "..", "packages", "beckn-protocol", "python")
)


# Symbols the deployed BAP actually imports from beckn_protocol.
_BAP_USED_SYMBOLS = (
    "RegistryClient",
    "SubscriberNotFound",
    "verify_request",
    "BecknSigner",
)


# Module files that exist in BOTH trees and must stay byte-identical.
# The __init__.py is intentionally different (the vendored one keeps the
# back-compat BecknProtocolUnavailable export) so it is excluded.
_SHARED_MODULES = (
    "catalog.py",
    "context.py",
    "domain_resolver.py",
    "errors.py",
    "fulfillment.py",
    "message.py",
    "ondc_tags.py",
    "order.py",
    "payment.py",
    "rating.py",
    "registry.py",
    "signer.py",
)


def test_bap_used_symbols_resolve():
    """Every symbol the live BAP imports from beckn_protocol exists."""
    beckn_protocol = importlib.import_module("beckn_protocol")
    for name in _BAP_USED_SYMBOLS:
        assert hasattr(beckn_protocol, name), (
            f"beckn_protocol is missing '{name}' — would break the "
            f"live BAP import surface"
        )


def test_vendored_modules_are_byte_identical_to_canonical():
    """Vendored copy must not drift from the canonical upstream.

    If this fails after a deliberate canonical-side change, re-vendor
    by running:
        cp packages/beckn-protocol/python/*.py \\
           apps/beli-aman-bap/beckn_protocol/
    """
    drifted = []
    for module in _SHARED_MODULES:
        vendored = os.path.join(_VENDORED_DIR, module)
        canonical = os.path.join(_CANONICAL_DIR, module)
        assert os.path.exists(vendored), f"vendored {module} missing"
        assert os.path.exists(canonical), f"canonical {module} missing"
        if not filecmp.cmp(vendored, canonical, shallow=False):
            drifted.append(module)
    assert not drifted, (
        f"vendored beckn_protocol diverged from canonical: {drifted}. "
        "Re-sync with: cp packages/beckn-protocol/python/*.py "
        "apps/beli-aman-bap/beckn_protocol/"
    )


def test_a1_a2_ondc_additions_reachable():
    """A1/A2 additions importable as `from beckn_protocol import ...`."""
    from beckn_protocol import (  # noqa: F401
        build_fulfillment_ondc_tags,
        build_item_statutory_tags,
        build_payment_settlement_tags,
        ondc_error,
        resolve_ondc_domain,
    )


def test_resolver_resolves_safiya_to_ret11():
    """The consolidated resolver maps Safiya to ONDC:RET11."""
    from beckn_protocol import resolve_ondc_domain

    resolved = resolve_ondc_domain("safiyafood.jaringan-dagang.id")
    assert resolved.domain_code == "ONDC:RET11"


def test_vendored_surface_is_superset_of_canonical():
    """Every canonical export must be re-exported by the vendored copy."""
    beckn_protocol = importlib.import_module("beckn_protocol")
    canonical = importlib.import_module("python")
    missing = [
        name for name in canonical.__all__ if not hasattr(beckn_protocol, name)
    ]
    assert not missing, f"vendored missing canonical exports: {missing}"
