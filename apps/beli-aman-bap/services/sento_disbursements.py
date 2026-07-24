"""Trigger Sento disbursements ("remit") to a brand's registered bank account.

This is the *release* leg of escrow for brands whose ``payment_provider ==
"sento"`` — the mirror of ``services/xendit_disbursements.py``. Buyer funds sit
in the partner's single Sento balance after they pay via a Sento payment link;
on escrow release we disburse from that balance to the brand/seller's bank
account. Custody stays with Sento throughout — we orchestrate, never touch
funds ourselves.

Sento's disbursement API is "Fund Disbursement" in sento-docs; the endpoints
are ``POST /api/remit`` (create) and ``POST /api/remit-status`` (poll), with a
dashboard-configured callback delivering the final status (``000`` success /
``300`` failed / ``301`` pending). Unlike Xendit, Sento returns **HTTP 200 with
a business ``status.code``** for rejections, so this service classifies the
code rather than relying on HTTP errors.

See ``services/sento_client.py`` for the raw HTTP wrapper.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.brand import Brand
from models.order import Order
from services import sento_client
from services.sento_client import SentoError
# Shared with the Xendit path: ``escrow.release()`` catches one
# ``DisbursementSkipped`` that covers both providers. (Pragmatic import — a
# shared ``services/disbursement_errors.py`` is the cleaner refactor but not
# worth the churn while there are exactly two providers.)
from services.xendit_disbursements import DisbursementSkipped

_LOG = logging.getLogger("beli_aman_bap.sento_disbursements")


# Sento ``status.code`` classification (see sento-docs Fund Disbursement tables).
# Final-success:
_CODE_SUCCESS = "000"
# Final-failure (transaction created but disbursing failed):
_CODES_FAILED = {"300", "206", "225"}
# Rejected at creation — bad request shape (bank/amount/format). Treat as
# failure so the ledger row marks FAILED and ops sees it:
_CODES_BAD_REQUEST = {"205", "209", "210", "211", "990"}
# Auth / IP / rate-limit rejections — failure with a loud log (these are
# operator-side config problems, not transient against the order):
_CODES_AUTH = {"201", "202", "207", "208", "429"}
# Non-final: in-progress / pending / unknown — leave the RELEASE ledger row
# PENDING; the callback or a status poll resolves it:
_CODES_NON_FINAL = {"101", "102", "301", "504", "999"}
# Duplicate partner_trx_id: a prior create with the same id is in flight.
# Idempotent-ish — recover the trx_id via the status API and stay PENDING:
_CODES_DUPLICATE = {"203", "257"}


async def disburse_to_seller(
    db: AsyncSession,
    *,
    order: Order,
    description: str = "",
) -> dict[str, Any]:
    """Create a Sento disbursement for ``order``.

    Returns ``{"id": <trx_id>, "code": <status_code>, "status": "completed" |
    "pending"}`` — the caller (``escrow.release``) stamps ``external_ref`` with
    ``id``. ``status`` is advisory for logging; the ledger row stays PENDING and
    is flipped to COMPLETED/FAILED by the ``/webhooks/sento/remit`` callback (or
    a status poll).

    Raises ``DisbursementSkipped`` if the brand isn't payout-configured (no
    Sento creds or no bank fields) — ops handles those manually until the
    Payouts admin form captures them. Raises ``SentoError`` if Sento rejects
    the disbursement with a final-failure / bad-request / auth code — the caller
    marks the ledger row FAILED.
    """
    brand_q = await db.execute(select(Brand).where(Brand.id == order.brand_id))
    brand = brand_q.scalar_one_or_none()
    if brand is None:
        raise DisbursementSkipped(f"Brand {order.brand_id} not found")
    # Sento creds: per-Brand override, else the env master.
    if not (brand.sento_api_key or settings.sento_api_key):
        raise DisbursementSkipped(
            f"Brand {brand.slug!r} has no Sento API key (env or Brand.sento_api_key)"
        )
    if not (
        brand.sento_disbursement_bank_code
        and brand.sento_disbursement_bank_account
    ):
        raise DisbursementSkipped(
            f"Brand {brand.slug!r} Sento disbursement bank fields incomplete"
        )

    partner_trx_id = f"order-{order.id}-release"
    response = await sento_client.create_disbursement(
        recipient_bank=brand.sento_disbursement_bank_code,
        recipient_account=brand.sento_disbursement_bank_account,
        amount_idr=order.total_idr,
        partner_trx_id=partner_trx_id,
        note=description or f"Beli Aman release — order {order.id}",
        # additional_data.partner_merchant_id tags the disbursement with the
        # brand for Sento-side reconciliation. Sento requires it within
        # additional_data if the object is present.
        additional_data={"partner_merchant_id": brand.slug or str(brand.id)},
        api_key=brand.sento_api_key,
        username=brand.sento_username,
    )

    code = str((response.get("status") or {}).get("code") or "")
    trx_id = response.get("trx_id") or None

    # Duplicate partner_trx_id — a prior create for the same order-release is
    # in flight. Recover the live trx_id via the status API and stay PENDING.
    if code in _CODES_DUPLICATE:
        _LOG.warning(
            "Sento disbursement duplicate partner_trx_id=%s for order %s — "
            "recovering via status API", partner_trx_id, order.id,
        )
        try:
            status_resp = await sento_client.get_disbursement_status(
                partner_trx_id=partner_trx_id,
                api_key=brand.sento_api_key,
                username=brand.sento_username,
            )
            trx_id = trx_id or status_resp.get("trx_id") or None
            code = str((status_resp.get("status") or {}).get("code") or code)
        except SentoError as e:
            _LOG.warning(
                "Sento status recovery failed for partner_trx_id=%s: %s — "
                "treating as pending", partner_trx_id, e,
            )
        return {"id": trx_id, "code": code, "status": "pending"}

    if code == _CODE_SUCCESS:
        return {"id": trx_id, "code": code, "status": "completed"}

    if code in _CODES_NON_FINAL:
        return {"id": trx_id, "code": code, "status": "pending"}

    if code in _CODES_FAILED or code in _CODES_BAD_REQUEST or code in _CODES_AUTH:
        raise SentoError(
            0,
            f"Sento disbursement rejected: code={code} "
            f"message={((response.get('status') or {}).get('message'))!r} "
            f"partner_trx_id={partner_trx_id}",
        )

    # Unknown code — don't fail the ledger row on a code we haven't mapped.
    # Stay PENDING and let the callback / a status poll resolve it.
    _LOG.warning(
        "Sento disbursement unknown status code=%s for order %s partner_trx_id=%s "
        "— leaving PENDING", code, order.id, partner_trx_id,
    )
    return {"id": trx_id, "code": code, "status": "pending"}
