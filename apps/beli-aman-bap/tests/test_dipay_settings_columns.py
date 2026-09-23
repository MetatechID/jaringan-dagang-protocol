"""Dipay settings + model columns — structural tests.

Verifies the Dipay integration has the configuration surface its services
and webhooks read:
- ``Settings`` carries ``dipay_base_url`` / ``dipay_client_key`` /
  ``dipay_client_secret`` / ``dipay_private_key_b64`` / ``dipay_merchant_id``
  plus ``qr_public_base`` and ``platform_release_fee_pct_bp``.
- ``Brand`` ORM model carries the ``dipay_*`` mapped columns (creds +
  disbursement target).
- ``EscrowLedger`` carries ``partner_ref`` (the remit webhook's lookup key).
- ``Cart`` carries ``qris_image_url`` / ``qris_content`` (bot-flow QRIS).

These are pure import / attribute tests — no DB, no network.
"""

from __future__ import annotations

import os
import sys

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)


def test_settings_carries_dipay_block():
    """``Settings`` exposes the dipay_* fields. The local .env may populate
    them with real staging creds — we assert structural presence only, plus
    the defaults that have no cred-bearing override.
    """
    from config import Settings  # noqa: WPS433

    s = Settings()
    assert hasattr(s, "dipay_base_url"), "missing dipay_base_url"
    # Default points at Dipay's demo env; prod overrides DIPAY_BASE_URL.
    assert "api-b2x-demo.dipay.id" in s.dipay_base_url
    assert hasattr(s, "dipay_client_key"), "missing dipay_client_key"
    assert isinstance(s.dipay_client_key, str), "dipay_client_key must be a string"
    assert hasattr(s, "dipay_client_secret"), "missing dipay_client_secret"
    assert hasattr(s, "dipay_private_key_b64"), "missing dipay_private_key_b64"
    assert hasattr(s, "dipay_merchant_id"), "missing dipay_merchant_id"
    assert hasattr(s, "qr_public_base"), "missing qr_public_base"
    assert s.qr_public_base.startswith("http")
    # Platform release cut: 200bp = 2% of gross on escrow release.
    assert s.platform_release_fee_pct_bp == 200
    assert hasattr(s, "dipay_callback_public_key"), "missing dipay_callback_public_key"
    assert hasattr(s, "dipay_qris_duration_seconds"), "missing dipay_qris_duration_seconds"
    assert s.dipay_qris_duration_seconds == 1800


def test_brand_model_has_dipay_columns():
    """``Brand`` model exposes dipay_* mapped columns (creds + payout target)."""
    from models.brand import Brand  # noqa: WPS433

    columns = {c.name for c in Brand.__table__.columns}
    assert "dipay_client_key" in columns
    assert "dipay_client_secret" in columns
    assert "dipay_private_key_b64" in columns
    assert "dipay_merchant_id" in columns
    assert "dipay_disbursement_bank_code" in columns
    assert "dipay_disbursement_bank_account" in columns
    assert "dipay_disbursement_holder_name" in columns


def test_escrow_ledger_has_partner_ref_column():
    """``EscrowLedger`` exposes ``partner_ref`` — the correlation key the
    Dipay remit webhook resolves RELEASE rows by."""
    from models.escrow_ledger import EscrowLedger  # noqa: WPS433

    columns = {c.name for c in EscrowLedger.__table__.columns}
    assert {
        "partner_ref",
        "gross_amount_idr",
        "platform_fee_idr",
        "provider_fee_idr",
        "net_amount_idr",
    } <= columns
    constraints = {c.name for c in EscrowLedger.__table__.constraints}
    assert "uq_escrow_ledger_partner_ref" in constraints


def test_cart_model_has_qris_columns():
    """``Cart`` exposes the bot-flow QRIS columns (renderer URL + payload)."""
    from models.bot_rest import Cart  # noqa: WPS433

    columns = {c.name for c in Cart.__table__.columns}
    assert "qris_image_url" in columns
    assert "qris_content" in columns
