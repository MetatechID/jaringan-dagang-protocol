"""Task A2b — outbound BAP envelopes emit the per-BPP ONDC domain code.

The deployed Beli Aman BAP previously emitted the generic
``settings.domain`` literal (``"retail"``) in every outbound Beckn
``context.domain`` field. A2b wires
:func:`beckn_protocol.resolve_ondc_domain` into the three real outbound
context-build sites so each /confirm, /update and /search envelope carries
the *per-store* ONDC domain code (Safiya -> ``ONDC:RET11``, unknown BPPs
fall back to ``ONDC:RET``).

The tests exercise the BAP's own helper / call sites (not the resolver in
isolation -- ``test_beckn_protocol_consolidation.py`` already proves that).
They capture the envelope a real call would build by either:

* invoking the small ``build_ondc_context`` helper directly (the
  three-site DRY factoring), or
* monkeypatching ``beckn.outbound.send_beckn_request`` and calling the
  public entry point (``services.beckn_orders.confirm_order``,
  ``routers.disputes`` inline /update construction reproduced via the
  helper).

YAGNI: no HTTP, no DB. Just the constructed envelope.
"""

from __future__ import annotations

import asyncio
import os
import sys

# Make apps/beli-aman-bap importable so the local modules resolve.
_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)


# --- build_ondc_context helper -------------------------------------------


def test_build_ondc_context_safiya_emits_ret11():
    """Helper resolves bpp_id -> ONDC:RET11 for Safiya."""
    from beckn.outbound import build_ondc_context

    ctx = build_ondc_context(
        action="confirm",
        bpp_id="safiyafood.jaringan-dagang.id",
        bpp_uri="https://safiyafood.example.com/beckn",
        transaction_id="txn-1",
    )
    assert ctx["domain"] == "ONDC:RET11"
    assert ctx["bpp_id"] == "safiyafood.jaringan-dagang.id"
    assert ctx["bpp_uri"] == "https://safiyafood.example.com/beckn"
    assert ctx["action"] == "confirm"
    assert ctx["transaction_id"] == "txn-1"


def test_build_ondc_context_unknown_bpp_falls_back_to_retail_default():
    """Unknown bpp_id -> resolver's store-level retail default (ONDC:RET)."""
    from beckn.outbound import build_ondc_context

    ctx = build_ondc_context(
        action="search",
        bpp_id="some.unknown.bpp.local",
        bpp_uri="http://unknown.example/beckn",
    )
    assert ctx["domain"] == "ONDC:RET"


def test_build_ondc_context_preserves_core_version_and_bap_identity():
    """core_version + bap_id/bap_uri come from settings (not hardcoded)."""
    from beckn.outbound import build_ondc_context
    from config import settings

    ctx = build_ondc_context(
        action="update",
        bpp_id="safiyafood",
        bpp_uri="http://example/beckn",
    )
    assert ctx["core_version"] == settings.core_version
    assert ctx["bap_id"] == settings.subscriber_id
    assert ctx["bap_uri"] == settings.subscriber_url
    assert ctx["country"] == settings.country_code
    assert ctx["city"] == settings.city_code


def test_build_ondc_context_generates_transaction_and_message_ids():
    """When transaction_id is None, a UUID is generated; message_id always set."""
    from beckn.outbound import build_ondc_context

    ctx = build_ondc_context(
        action="confirm",
        bpp_id="safiyafood",
        bpp_uri="http://example/beckn",
    )
    assert ctx["transaction_id"]
    assert ctx["message_id"]
    assert ctx["timestamp"]


# --- /confirm: services.beckn_orders.confirm_order -----------------------


def test_confirm_order_envelope_uses_safiya_ondc_ret11(monkeypatch):
    """confirm_order to Safiya emits context.domain == ONDC:RET11."""
    import beckn.outbound as outbound_mod
    import services.beckn_orders as bo

    captured: dict = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(outbound_mod, "send_beckn_request", _capture)
    # confirm_order does `from beckn.outbound import send_beckn_request`
    # at module import time, so patch the bound name on services.beckn_orders too.
    monkeypatch.setattr(bo, "send_beckn_request", _capture)

    asyncio.run(
        bo.confirm_order(
            order_dict={
                "bpp_id": "safiyafood.jaringan-dagang.id",
                "transaction_id": "txn-a2b-1",
                "order_id": "order-1",
                "items": [{"id": "sku-1"}],
                "buyer": {"name": "Buyer"},
                "shipping_address": "Jl. Mawar No. 1",
                "total_idr": 100000,
                "escrow_status": "held",
            }
        )
    )
    env = captured["body"]
    assert env["context"]["domain"] == "ONDC:RET11"
    assert env["context"]["bpp_id"] == "safiyafood.jaringan-dagang.id"
    assert env["context"]["action"] == "confirm"


def test_confirm_order_envelope_unknown_bpp_falls_back_to_ondc_ret(monkeypatch):
    """Unknown BPP -> context.domain == ONDC:RET (resolver default)."""
    import beckn.outbound as outbound_mod
    import services.beckn_orders as bo

    captured: dict = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(outbound_mod, "send_beckn_request", _capture)
    monkeypatch.setattr(bo, "send_beckn_request", _capture)

    asyncio.run(
        bo.confirm_order(
            order_dict={
                "bpp_id": "bpp.someunknownstore.local",
                "order_id": "order-2",
                "total_idr": 0,
            }
        )
    )
    env = captured["body"]
    assert env["context"]["domain"] == "ONDC:RET"


# --- /update: routers.disputes refund-request envelope -------------------


def test_update_envelope_uses_safiya_ondc_ret11():
    """Refund /update built via build_ondc_context emits ONDC:RET11.

    The disputes.py inline envelope construction is refactored to use
    ``build_ondc_context``; this test exercises that helper with a
    Safiya bpp_id (mirroring how disputes.py calls it) and asserts
    domain == ONDC:RET11.
    """
    from beckn.outbound import build_ondc_context

    ctx = build_ondc_context(
        action="update",
        bpp_id="safiyafood.jaringan-dagang.id",
        bpp_uri="https://safiyafood.example.com/beckn",
        transaction_id="order-uuid-abc",
    )
    assert ctx["domain"] == "ONDC:RET11"
    assert ctx["action"] == "update"
    assert ctx["transaction_id"] == "order-uuid-abc"
