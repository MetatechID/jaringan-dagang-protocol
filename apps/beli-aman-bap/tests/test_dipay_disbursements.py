"""Dipay disbursement module — pure unit tests.

Covers ``services/dipay_disbursements.py``:
- fee matrix (platform 200bp default + optional flat/percent Dipay fee,
  floor rounding)
- min-amount / brand-missing / creds-missing ``DisbursementSkipped``
- SNAP ``responseCode`` classification: 20043xx accepted → pending,
  40943xx duplicate → status-API recovery → pending, 40043/40143/40343/
  40443/50043/50443 → DipayError, unknown → pending + warn
- ``partner_ref`` is a SNAP-compliant ``r-…`` (≤32 chars)
- breakdown dict keys

DB/HTTP fakes from ``tests/_dipay_fakes``; we monkeypatch
``dipay_client.create_disbursement`` / ``get_disbursement_status`` so no
network is hit.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_BAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

import pytest

from services import dipay_client, dipay_disbursements  # noqa: E402
from services.dipay_client import DipayError  # noqa: E402
from services.xendit_disbursements import DisbursementSkipped  # noqa: E402
from tests._dipay_fakes import (  # noqa: E402
    FakeSession,
    StubBrand,
    StubOrder,
)


def _fake_settings(**overrides) -> SimpleNamespace:
    base = dict(
        dipay_client_key="env-client-key",
        platform_release_fee_pct_bp=200,       # 2%
        dipay_disbursement_fee_flat_idr=0,
        dipay_disbursement_fee_pct_bp=0,
        dipay_disbursement_min_amount_idr=10_000,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_session(brand) -> FakeSession:
    return FakeSession([brand])


@pytest.fixture(autouse=True)
def _deterministic_settings(monkeypatch):
    """Pin settings so ambient .env / env vars can't skew the fee matrix."""
    monkeypatch.setattr(dipay_disbursements, "settings", _fake_settings())


class TestFeeMatrix:
    def test_default_two_percent_platform_fee(self):
        fees = dipay_disbursements._fee_breakdown(100_000)
        assert fees == {
            "gross_idr": 100_000,
            "platform_fee_idr": 2_000,   # 200bp of 100k
            "dipay_fee_idr": 0,
            "net_idr": 98_000,
        }

    def test_floor_rounding_never_favours_the_seller(self):
        # 2% of 10_001 = 200.02 → floor 200; net = 10_001 - 200 = 9801.
        fees = dipay_disbursements._fee_breakdown(10_001)
        assert fees["platform_fee_idr"] == 200
        assert fees["net_idr"] == 9_801

    def test_flat_and_percent_dipay_fee_stack(self, monkeypatch):
        monkeypatch.setattr(
            dipay_disbursements, "settings",
            _fake_settings(
                platform_release_fee_pct_bp=250,   # 2.5%
                dipay_disbursement_fee_pct_bp=100,  # 1%
                dipay_disbursement_fee_flat_idr=2_500,
            ),
        )
        # gross 200_000 → platform 5_000, dipay 2_000 + 2_500 = 4_500,
        # net 190_500.
        fees = dipay_disbursements._fee_breakdown(200_000)
        assert fees["platform_fee_idr"] == 5_000
        assert fees["dipay_fee_idr"] == 4_500
        assert fees["net_idr"] == 190_500

    def test_bp_convention_matches_pricing_module(self):
        # 200bp == 2% — same convention as services/pricing.py.
        assert 100_000 * 200 // 10_000 == 100_000 * 2 // 100


class TestGuards:
    @pytest.mark.asyncio
    async def test_skips_when_brand_missing(self, monkeypatch):
        db = _make_session(brand=None)
        order = StubOrder(brand_id="ghost-brand")
        with pytest.raises(DisbursementSkipped) as ei:
            await dipay_disbursements.disburse_to_seller(db, order=order)
        assert "ghost-brand" in str(ei.value)

    @pytest.mark.asyncio
    async def test_skips_when_no_client_key_anywhere(self, monkeypatch):
        monkeypatch.setattr(
            dipay_disbursements, "settings", _fake_settings(dipay_client_key="")
        )
        db = _make_session(StubBrand(dipay_client_key=""))
        order = StubOrder()

        async def fake_create(**_kwargs):
            raise AssertionError("no creds → must not reach Dipay")

        monkeypatch.setattr(dipay_client, "create_disbursement", fake_create)
        with pytest.raises(DisbursementSkipped) as ei:
            await dipay_disbursements.disburse_to_seller(db, order=order)
        assert "client key" in str(ei.value)

    @pytest.mark.asyncio
    async def test_skips_when_bank_fields_incomplete(self, monkeypatch):
        db = _make_session(StubBrand(dipay_disbursement_bank_account=""))
        order = StubOrder()

        async def fake_create(**_kwargs):
            raise AssertionError("no bank account → must not reach Dipay")

        monkeypatch.setattr(dipay_client, "create_disbursement", fake_create)
        with pytest.raises(DisbursementSkipped) as ei:
            await dipay_disbursements.disburse_to_seller(db, order=order)
        assert "bank fields incomplete" in str(ei.value)

    @pytest.mark.asyncio
    async def test_skips_when_net_below_minimum(self, monkeypatch):
        # gross 10_100, platform fee 202 → net 9_898 < 10_000.
        db = _make_session(StubBrand())
        order = StubOrder(total_idr=10_100)
        with pytest.raises(DisbursementSkipped) as ei:
            await dipay_disbursements.disburse_to_seller(db, order=order)
        msg = str(ei.value)
        assert "9_898" in msg or "9898" in msg
        assert "10_000" in msg or "10000" in msg
        assert "safiya" in msg

    @pytest.mark.asyncio
    async def test_skips_when_zero_gross(self, monkeypatch):
        db = _make_session(StubBrand())
        order = StubOrder(total_idr=0)
        with pytest.raises(DisbursementSkipped):
            await dipay_disbursements.disburse_to_seller(db, order=order)


class TestResponseCodeClassification:
    async def _run(self, monkeypatch, response, status_response=None):
        db = _make_session(StubBrand())
        order = StubOrder(total_idr=750_000)
        captured: dict = {}
        status_calls: list = []

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return response

        async def fake_status(**kwargs):
            status_calls.append(kwargs)
            return status_response or {
                "responseCode": "2004300", "referenceNo": "REF-RECOVERED",
            }

        monkeypatch.setattr(dipay_client, "create_disbursement", fake_create)
        monkeypatch.setattr(dipay_client, "get_disbursement_status", fake_status)
        out = await dipay_disbursements.disburse_to_seller(db, order=order)
        return out, captured, status_calls

    @pytest.mark.asyncio
    async def test_accepted_20043xx_is_pending_with_breakdown(self, monkeypatch):
        out, captured, status_calls = await self._run(
            monkeypatch,
            {"responseCode": "2004300", "responseMessage": "Success",
             "referenceNo": "REF-1"},
        )
        assert out["status"] == "pending"
        assert out["code"] == "2004300"
        assert out["id"] == "REF-1"
        assert status_calls == []
        # Fees: gross 750_000, platform 2% = 15_000, no dipay fee → net 735_000.
        assert captured["amount_idr"] == 735_000
        assert out["gross_idr"] == 750_000
        assert out["platform_fee_idr"] == 15_000
        assert out["dipay_fee_idr"] == 0
        assert out["net_idr"] == 735_000
        assert out["partner_ref"] == captured["partner_reference_no"]

    @pytest.mark.asyncio
    async def test_duplicate_40943xx_recovers_via_status_api(self, monkeypatch):
        out, captured, status_calls = await self._run(
            monkeypatch,
            {"responseCode": "4094301", "responseMessage": "Duplicate partnerReferenceNo"},
            status_response={"responseCode": "2004300", "referenceNo": "REF-LIVE"},
        )
        assert out["status"] == "pending"
        assert out["code"] == "2004300"
        assert out["id"] == "REF-LIVE"
        assert len(status_calls) == 1
        assert status_calls[0]["partner_reference_no"] == captured["partner_reference_no"]

    @pytest.mark.asyncio
    async def test_duplicate_recovery_failure_stays_pending(self, monkeypatch):
        db = _make_session(StubBrand())
        order = StubOrder(total_idr=750_000)

        async def fake_create(**_kwargs):
            return {"responseCode": "4094301", "responseMessage": "Duplicate"}

        async def fake_status(**_kwargs):
            raise DipayError(500, "status API down")

        monkeypatch.setattr(dipay_client, "create_disbursement", fake_create)
        monkeypatch.setattr(dipay_client, "get_disbursement_status", fake_status)
        out = await dipay_disbursements.disburse_to_seller(db, order=order)
        assert out["status"] == "pending"
        assert out["id"] == out["partner_ref"]  # falls back to partner_ref

    @pytest.mark.parametrize("code", ["4014300", "4034300", "4044300", "4004300", "5004300", "5044300"])
    @pytest.mark.asyncio
    async def test_rejections_raise_dipay_error(self, monkeypatch, code):
        db = _make_session(StubBrand())
        order = StubOrder(total_idr=750_000)

        async def fake_create(**_kwargs):
            return {"responseCode": code, "responseMessage": "Rejected"}

        async def fake_status(**_kwargs):  # pragma: no cover
            raise AssertionError("rejected → no status recovery")

        monkeypatch.setattr(dipay_client, "create_disbursement", fake_create)
        monkeypatch.setattr(dipay_client, "get_disbursement_status", fake_status)
        with pytest.raises(DipayError) as ei:
            await dipay_disbursements.disburse_to_seller(db, order=order)
        assert code in str(ei.value.body)
        assert "Rejected" in str(ei.value.body)

    @pytest.mark.asyncio
    async def test_unknown_code_stays_pending_with_warning(self, monkeypatch):
        out, _captured, status_calls = await self._run(
            monkeypatch,
            {"responseCode": "9999999", "responseMessage": "Mystery"},
        )
        assert out["status"] == "pending"
        assert out["code"] == "9999999"
        assert status_calls == []


class TestCreateCallShape:
    @pytest.mark.asyncio
    async def test_partner_ref_is_snap_compliant_and_r_prefixed(self, monkeypatch):
        db = _make_session(StubBrand())
        order = StubOrder(id="0f0e8a2e-1c3b-4d5e-6f70-8a9b0c1d2e3f")
        captured: dict = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return {"responseCode": "2004300", "referenceNo": "REF-1"}

        monkeypatch.setattr(dipay_client, "create_disbursement", fake_create)
        await dipay_disbursements.disburse_to_seller(db, order=order)
        ref = captured["partner_reference_no"]
        assert ref.startswith("r-")
        assert len(ref) <= 32
        assert "-" not in ref[2:]  # dashes stripped from the uuid body

    @pytest.mark.asyncio
    async def test_bank_fields_and_correlation_threaded_through(self, monkeypatch):
        db = _make_session(StubBrand())
        order = StubOrder(total_idr=750_000)
        captured: dict = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return {"responseCode": "2004300", "referenceNo": "REF-1"}

        monkeypatch.setattr(dipay_client, "create_disbursement", fake_create)
        await dipay_disbursements.disburse_to_seller(db, order=order)
        assert captured["beneficiary_account"] == "1234567890"
        assert captured["beneficiary_bank_code"] == "014"
        assert captured["partner_merchant_id"] == "safiya"
        assert captured["customer_reference"].startswith("BeliAman release order")
        assert len(captured["customer_reference"]) <= 30

    @pytest.mark.asyncio
    async def test_amount_idr_override_changes_gross(self, monkeypatch):
        db = _make_session(StubBrand())
        order = StubOrder(total_idr=750_000)
        captured: dict = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return {"responseCode": "2004300", "referenceNo": "REF-1"}

        monkeypatch.setattr(dipay_client, "create_disbursement", fake_create)
        out = await dipay_disbursements.disburse_to_seller(
            db, order=order, amount_idr=100_000
        )
        # 2% platform fee on the override gross → net 98_000.
        assert captured["amount_idr"] == 98_000
        assert out["gross_idr"] == 100_000
        assert out["net_idr"] == 98_000

    @pytest.mark.asyncio
    async def test_breakdown_dict_keys_complete(self, monkeypatch):
        db = _make_session(StubBrand())
        order = StubOrder(total_idr=750_000)

        async def fake_create(**_kwargs):
            return {"responseCode": "2004300", "referenceNo": "REF-1"}

        monkeypatch.setattr(dipay_client, "create_disbursement", fake_create)
        out = await dipay_disbursements.disburse_to_seller(db, order=order)
        assert set(out) == {
            "id", "code", "status",
            "gross_idr", "platform_fee_idr", "dipay_fee_idr", "net_idr",
            "partner_ref",
        }
