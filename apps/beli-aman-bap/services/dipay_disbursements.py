"""Trigger Dipay disbursements (bank transfer) to a brand's registered bank
account.

This is the *release* leg of escrow for brands whose ``payment_provider ==
"dipay"`` — the mirror of ``services/sento_disbursements.py``. Buyer funds
settle into the merchant's single Dipay balance after they pay via a Dipay
QRIS invoice; on escrow release we transfer from that balance to the
brand/seller's bank account via ``POST /emoney/transfer-bank``. Custody
stays with Dipay throughout — we orchestrate, never touch funds ourselves.

Before wiring out, the platform takes its cut: ``platform_release_fee_pct_bp``
(basis points, 200bp = 2%) plus an optional Dipay fee model
(``dipay_disbursement_fee_pct_bp`` + ``dipay_disbursement_fee_flat_idr``).
The seller receives ``net = gross - platform_fee - dipay_fee``; if the net
falls below ``dipay_disbursement_min_amount_idr`` the disbursement is
skipped rather than sent.

Dipay/SNAP returns HTTP 200 with a business ``responseCode`` for many
outcomes, so this service classifies the code: ``20043xx`` accepted
(pending — the callback or a status poll finalizes it), ``40943xx``
duplicate (recover via the status API), ``40143xx``/``40343xx``/``40443xx``
/``40043xx``/``50043xx``/``50443xx`` rejected (raise, ledger row → FAILED),
anything else stays PENDING with a loud log.

See ``services/dipay_client.py`` for the raw HTTP wrapper.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.brand import Brand
from models.order import Order
from services import dipay_client
from services.dipay_client import DipayError, snap_ref
# Shared with the Xendit/Sento paths: ``escrow.release()`` catches one
# ``DisbursementSkipped`` that covers all providers. (Pragmatic import — a
# shared ``services/disbursement_errors.py`` is the cleaner refactor but not
# worth the churn while there are exactly three providers.)
from services.xendit_disbursements import DisbursementSkipped

_LOG = logging.getLogger("beli_aman_bap.dipay_disbursements")


# Dipay disbursement ``responseCode`` classification (SNAP service code 43 —
# successful responses start "20043", the codes below mirror the SNAP
# status/reason tables in api-docs.dipay.id).
# Accepted at creation — non-final; callback / status poll finalizes:
_RESPONSE_CODES_ACCEPTED_PREFIX = "20043"
# Duplicate partnerReferenceNo: a prior create with the same id is in
# flight. Idempotent-ish — recover the referenceNo via the status API and
# stay PENDING:
_RESPONSE_CODES_DUPLICATE_PREFIX = "40943"
# Rejected at creation — auth (401), forbidden (403), not found (404),
# bad request (400), Dipay server error (500), timeout (504). Treat as
# failure so the ledger row marks FAILED and ops sees it:
_RESPONSE_CODES_REJECTED_PREFIXES = (
    "40143",
    "40343",
    "40443",
    "40043",
    "50043",
    "50443",
)


def _fee_breakdown(gross_idr: int) -> dict[str, int]:
    """Split ``gross_idr`` into platform + Dipay fees and the seller's net.

    bp convention follows ``services/pricing.py``: 200bp = 2%, integer
    floor division (never rounds in the seller's favour by a fraction).
    """
    platform_fee = gross_idr * settings.platform_release_fee_pct_bp // 10_000
    provider_fee = (
        gross_idr * settings.dipay_disbursement_fee_pct_bp // 10_000
        + settings.dipay_disbursement_fee_flat_idr
    )
    net = gross_idr - platform_fee - provider_fee
    return {
        "gross_idr": gross_idr,
        "platform_fee_idr": platform_fee,
        "dipay_fee_idr": provider_fee,
        "net_idr": net,
    }


async def disburse_to_seller(
    db: AsyncSession,
    *,
    order: Order,
    description: str = "",
    amount_idr: int | None = None,
) -> dict[str, Any]:
    """Create a Dipay disbursement for ``order``.

    Returns ``{"id": <referenceNo or partner_ref>, "code": <responseCode>,
    "status": "pending", "gross_idr", "platform_fee_idr", "dipay_fee_idr",
    "net_idr", "partner_ref"}`` — the caller (``escrow.release``) stamps
    ``external_ref`` with ``id``. ``status`` is advisory for logging; the
    ledger row stays PENDING and is flipped to COMPLETED/FAILED by the
    Dipay disbursement callback (or a status poll).

    ``amount_idr`` overrides ``order.total_idr`` as the gross (used when a
    partial release is wired). Fees are computed off the gross.

    Raises ``DisbursementSkipped`` if the brand isn't payout-configured
    (no Dipay creds or no bank fields) or the net lands below the minimum
    — ops handles those manually until the Payouts admin form captures
    them. Raises ``DipayError`` if Dipay rejects the disbursement with a
    final-failure / bad-request / auth responseCode — the caller marks the
    ledger row FAILED.
    """
    brand_q = await db.execute(select(Brand).where(Brand.id == order.brand_id))
    brand = brand_q.scalar_one_or_none()
    if brand is None:
        raise DisbursementSkipped(f"Brand {order.brand_id} not found")
    # Dipay creds: per-Brand override, else the env master.
    if not (getattr(brand, "dipay_client_key", None) or settings.dipay_client_key):
        raise DisbursementSkipped(
            f"Brand {brand.slug!r} has no Dipay client key "
            "(env or Brand.dipay_client_key)"
        )
    if not (
        brand.dipay_disbursement_bank_code
        and brand.dipay_disbursement_bank_account
    ):
        raise DisbursementSkipped(
            f"Brand {brand.slug!r} Dipay disbursement bank fields incomplete"
        )

    gross = int(amount_idr if amount_idr is not None else order.total_idr)
    fees = _fee_breakdown(gross)
    net = fees["net_idr"]
    if net < settings.dipay_disbursement_min_amount_idr:
        raise DisbursementSkipped(
            f"net {net} below Dipay minimum "
            f"{settings.dipay_disbursement_min_amount_idr} for brand {brand.slug}"
        )

    partner_ref = snap_ref("r", str(order.id))
    response = await dipay_client.create_disbursement(
        partner_reference_no=partner_ref,
        beneficiary_account=brand.dipay_disbursement_bank_account,
        beneficiary_bank_code=brand.dipay_disbursement_bank_code,
        amount_idr=net,
        partner_merchant_id=brand.slug,
        # customer_reference is our human-readable correlation string; SNAP
        # caps it, so truncate to be safe.
        customer_reference=f"BeliAman release order {order.id}"[:30],
    )

    response_code = str(response.get("responseCode") or "")
    response_message = str(response.get("responseMessage") or "")
    reference_no = response.get("referenceNo") or None

    # Duplicate partnerReferenceNo — a prior create for the same
    # order-release is in flight. Recover the live referenceNo via the
    # status API and stay PENDING.
    if response_code.startswith(_RESPONSE_CODES_DUPLICATE_PREFIX):
        _LOG.warning(
            "Dipay disbursement duplicate partner_ref=%s for order %s — "
            "recovering via status API", partner_ref, order.id,
        )
        try:
            status_resp = await dipay_client.get_disbursement_status(
                partner_reference_no=partner_ref,
            )
            reference_no = reference_no or status_resp.get("referenceNo") or None
            response_code = str(status_resp.get("responseCode") or response_code)
        except DipayError as e:
            _LOG.warning(
                "Dipay status recovery failed for partner_ref=%s: %s — "
                "treating as pending", partner_ref, e,
            )
        return {
            "id": reference_no or partner_ref,
            "code": response_code,
            "status": "pending",
            **fees,
            "partner_ref": partner_ref,
        }

    if response_code.startswith(_RESPONSE_CODES_ACCEPTED_PREFIX):
        # Accepted, non-final: the callback or a status poll flips the
        # ledger row to COMPLETED/FAILED later.
        return {
            "id": reference_no or partner_ref,
            "code": response_code,
            "status": "pending",
            **fees,
            "partner_ref": partner_ref,
        }

    if response_code.startswith(_RESPONSE_CODES_REJECTED_PREFIXES):
        raise DipayError(
            0,
            f"Dipay disbursement rejected: {response_code} {response_message} "
            f"partner_ref={partner_ref}",
        )

    # Unknown code — don't fail the ledger row on a code we haven't
    # mapped. Stay PENDING and let the callback / a status poll resolve it.
    _LOG.warning(
        "Dipay disbursement unknown responseCode=%s for order %s "
        "partner_ref=%s — leaving PENDING", response_code, order.id, partner_ref,
    )
    return {
        "id": reference_no or partner_ref,
        "code": response_code,
        "status": "pending",
        **fees,
        "partner_ref": partner_ref,
    }
