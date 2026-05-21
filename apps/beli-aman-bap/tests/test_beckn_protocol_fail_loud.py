"""beckn_protocol self-containment + back-compat discriminator contract.

The BAP's ``beckn_protocol`` package is vendored in-tree (a deliberate
copy of ``packages/beckn-protocol/python/``) because the Vercel project's
``rootDirectory=apps/beli-aman-bap`` means anything outside the BAP
directory is NOT uploaded to the function image. An earlier attempt
(commit ``3df4fea``) made it a re-export shim pointing at the canonical
path; that worked locally but crashed every Vercel deploy with
``FUNCTION_INVOCATION_FAILED``.

Two invariants are guarded here:

1. Self-containment: ``import beckn_protocol`` must succeed even when
   the canonical ``packages/beckn-protocol`` directory is unreachable.
   This is the production constraint on Vercel.

2. Back-compat: ``BecknProtocolUnavailable`` is still exported, and
   ``main.py``'s ``_should_reraise_canonical_failure`` helper still
   discriminates the would-be canonical-failure case from genuinely
   optional third-party dep failures. The discriminator can no longer
   actually fire in production (vendored copy can't be missing), but
   keeping it correct avoids subtle breakage if the helper is ever
   reused in another context.
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest


_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)


# --------------------------- self-containment -------------------------


def test_beckn_protocol_imports_without_external_canonical(monkeypatch):
    """Vendored package must import even without packages/beckn-protocol on path.

    This simulates the Vercel runtime where the repo-root canonical
    directory is absent. We strip every ``packages/beckn-protocol`` entry
    from ``sys.path`` and the cached ``python`` / ``beckn_protocol``
    modules, then force a fresh import. It must succeed.
    """
    minimal_path = [
        p
        for p in sys.path
        if "packages/beckn-protocol" not in p and "packages\\beckn-protocol" not in p
    ]
    monkeypatch.setattr(sys, "path", minimal_path)

    for name in list(sys.modules):
        if name == "beckn_protocol" or name.startswith("beckn_protocol."):
            sys.modules.pop(name, None)
        if name == "python" or name.startswith("python."):
            sys.modules.pop(name, None)

    bp = importlib.import_module("beckn_protocol")
    # Spot-check core exports the BAP relies on.
    for sym in ("RegistryClient", "BecknSigner", "verify_request", "resolve_ondc_domain"):
        assert hasattr(bp, sym), f"vendored beckn_protocol missing {sym!r}"


# ----------------------- back-compat: exception -----------------------


def test_BecknProtocolUnavailable_still_exported():
    """Class remains importable for back-compat consumers."""
    from beckn_protocol import BecknProtocolUnavailable

    assert issubclass(BecknProtocolUnavailable, ImportError)


# ----------------------- back-compat: discriminator --------------------


def _import_main_helpers():
    sys.modules.pop("main", None)
    return importlib.import_module("main")


def test_discriminator_reraises_BecknProtocolUnavailable():
    main = _import_main_helpers()
    from beckn_protocol import BecknProtocolUnavailable

    assert main._should_reraise_canonical_failure(
        BecknProtocolUnavailable("nope")
    ) is True


def test_discriminator_reraises_when_canonical_in_cause_chain():
    main = _import_main_helpers()
    from beckn_protocol import BecknProtocolUnavailable

    inner = BecknProtocolUnavailable("inner canonical fail")
    try:
        raise ImportError("router import failed") from inner
    except ImportError as caught:
        assert main._should_reraise_canonical_failure(caught) is True


def test_discriminator_reraises_when_canonical_in_context_chain():
    main = _import_main_helpers()
    from beckn_protocol import BecknProtocolUnavailable

    try:
        try:
            raise BecknProtocolUnavailable("fail")
        except BecknProtocolUnavailable:
            raise ImportError("third-party adapter blew up")
    except ImportError as caught:
        assert main._should_reraise_canonical_failure(caught) is True


def test_discriminator_swallows_unrelated_ImportError():
    main = _import_main_helpers()

    err = ModuleNotFoundError("No module named 'some_optional_sdk'")
    assert main._should_reraise_canonical_failure(err) is False

    other = RuntimeError("router init failed for unrelated reason")
    assert main._should_reraise_canonical_failure(other) is False


def test_main_block_reraises_canonical_failure():
    main = _import_main_helpers()
    from beckn_protocol import BecknProtocolUnavailable

    def _attempt_import_with(exc: BaseException):
        try:
            raise exc
        except Exception as e:  # noqa: BLE001 — mirrors main.py defensive block
            if main._should_reraise_canonical_failure(e):
                raise
            return "swallowed"

    with pytest.raises(BecknProtocolUnavailable):
        _attempt_import_with(BecknProtocolUnavailable("boom"))

    assert _attempt_import_with(
        ModuleNotFoundError("No module named 'optional_sdk'")
    ) == "swallowed"
