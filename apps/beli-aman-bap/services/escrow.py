"""Escrow ledger writes + PSP-side movement orchestration.

HOLD entries are written from the Xendit ``invoice.paid`` webhook —
funds are already in the seller's Xendit sub-account, so HOLD is
recorded as COMPLETED.

RELEASE and REFUND each kick off a corresponding Xendit operation
(disbursement / refund) and write the ledger row as PENDING. The
matching Xendit callback (handled in ``routers/webhooks_xendit.py``)
flips it to COMPLETED — or FAILED if Xendit rejects, which routes to
ops for manual recovery.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.escrow_ledger import (
    EscrowEntryStatus,
    EscrowEntryType,
    EscrowLedger,
)
from models.order import Order
from services import xendit_client, xendit_disbursements
from services.dipay_client import DipayError
from services.sento_client import SentoError
from services.xendit_client import XenditError
from services.xendit_disbursements import DisbursementSkipped

_LOG = logging.getLogger("beli_aman_bap.escrow")


async def hold(
    db: AsyncSession,
    *,
    order_id: str,
    amount_idr: int,
    description: str = "",
    external_ref: str | None = None,
) -> EscrowLedger:
    """Record that funds are held in the seller's Xendit sub-account.

    Called from the Xendit ``invoice.paid`` webhook handler. The money has
    already settled in Xendit by the time we run, so HOLD is COMPLETED.
    """
    entry = EscrowLedger(
        order_id=order_id,
        entry_type=EscrowEntryType.HOLD,
        amount_idr=amount_idr,
        description=description or "Funds held by Beli Aman pending receipt",
        external_ref=external_ref,
        status=EscrowEntryStatus.COMPLETED,
    )
    db.add(entry)
    await db.flush()
    return entry


async def release(
    db: AsyncSession,
    *,
    order_id: str,
    amount_idr: int,
    description: str = "",
) -> EscrowLedger:
    """Write a RELEASE ledger entry AND kick off a Xendit disbursement.

    Lifecycle:
    1. Ledger row inserted with status=PENDING.
    2. Xendit disbursement created (funds move from brand sub-account
       balance → brand's registered bank account).
    3. On success, ledger row's external_ref is set to the disbursement id.
       Status stays PENDING; flips to COMPLETED on the
       ``disbursement.completed`` webhook.
    4. If the brand isn't onboarded yet (no sub-account, no bank), the
       ledger row stays PENDING with no external_ref — ops disburses
       manually then flips the row by hand.
    5. If Xendit rejects, ledger row → FAILED. Ops recovers.
    """
    entry = EscrowLedger(
        order_id=order_id,
        entry_type=EscrowEntryType.RELEASE,
        amount_idr=amount_idr,
        description=description or "Funds released to seller after delivery confirmed",
        status=EscrowEntryStatus.PENDING,
    )
    db.add(entry)
    await db.flush()

    order = (
        await db.execute(select(Order).where(Order.id == order_id))
    ).scalar_one_or_none()
    if order is None:
        _LOG.error("release() called with unknown order_id=%s", order_id)
        return entry

    # Disbursement provider is per-Brand — mirror the invoice leg's dispatch
    # (routers/orders.py create_invoice). Sento brands disburse via the remit
    # API; Dipay brands via SNAP transfer-bank; everything else (default
    # "xendit") via Xendit. All services return {"id": <psp-id>} and raise
    # the shared DisbursementSkipped when the brand isn't payout-configured,
    # so this branch stays uniform.
    from models.brand import Brand
    from services import sento_disbursements

    brand_q = await db.execute(select(Brand).where(Brand.id == order.brand_id))
    brand = brand_q.scalar_one_or_none()
    provider = (brand.payment_provider if brand is not None else "xendit") or "xendit"

    # Race-safe Dipay correlation: the remit webhook resolves the ledger row
    # by ``partner_ref``, so stamp it BEFORE the provider call. snap_ref is
    # deterministic (same order id → same ref), and disburse_to_seller
    # computes the identical value, so both sides always agree even if the
    # callback arrives mid-create.
    if provider == "dipay":
        from services.dipay_client import snap_ref

        entry.partner_ref = snap_ref("r", str(order_id))

    response = None
    try:
        if provider == "sento":
            response = await sento_disbursements.disburse_to_seller(
                db, order=order, description=description,
            )
        elif provider == "dipay":
            from services import dipay_disbursements

            response = await dipay_disbursements.disburse_to_seller(
                db, order=order, description=description,
                amount_idr=amount_idr,
            )
        else:
            response = await xendit_disbursements.disburse_to_seller(
                db, order=order, description=description,
            )
        entry.external_ref = response.get("id")
        _LOG.info(
            "%s disbursement %s kicked off for order %s",
            provider, entry.external_ref, order_id,
        )
    except DisbursementSkipped as e:
        _LOG.warning(
            "Disbursement skipped for order %s (ops manual): %s",
            order_id, e,
        )
    except (XenditError, SentoError, DipayError) as e:
        entry.status = EscrowEntryStatus.FAILED
        _LOG.exception(
            "%s disbursement FAILED for order %s: %s — ledger row marked FAILED",
            provider, order_id, e,
        )
    except Exception:  # noqa: BLE001
        entry.status = EscrowEntryStatus.FAILED
        _LOG.exception(
            "Unexpected error kicking off disbursement for order %s — "
            "ledger row marked FAILED", order_id,
        )

    # Dipay fee math — append the gross → net split to the ledger row so ops
    # can reconcile the platform cut without opening Dipay's dashboard. The
    # provider response carries the breakdown computed off the same gross.
    if provider == "dipay" and isinstance(response, dict) and "net_idr" in response:
        entry.description = (
            (entry.description or "")
            + (
                f" — gross Rp{response['gross_idr']:,} → platform fee "
                f"Rp{response['platform_fee_idr']:,}, Dipay fee "
                f"Rp{response['dipay_fee_idr']:,}, net Rp{response['net_idr']:,}"
            )
        )
        # Belt-and-braces — same deterministic value stamped above.
        if response.get("partner_ref"):
            entry.partner_ref = response["partner_ref"]

    await db.flush()
    return entry


async def refund(
    db: AsyncSession,
    *,
    order_id: str,
    amount_idr: int,
    description: str = "",
) -> EscrowLedger:
    """Write a REFUND ledger entry AND kick off a Xendit refund.

    Mirror of ``release`` but targets the original invoice rather than a
    disbursement.
    """
    entry = EscrowLedger(
        order_id=order_id,
        entry_type=EscrowEntryType.REFUND,
        amount_idr=amount_idr,
        description=description or "Funds refunded to buyer",
        status=EscrowEntryStatus.PENDING,
    )
    db.add(entry)
    await db.flush()

    order = (
        await db.execute(select(Order).where(Order.id == order_id))
    ).scalar_one_or_none()
    if order is None:
        _LOG.error("refund() called with unknown order_id=%s", order_id)
        return entry

    invoice_id = (order.payment_method_snapshot or {}).get("invoice_id")
    if not invoice_id:
        _LOG.warning(
            "Refund skipped for order %s — no invoice_id on snapshot "
            "(ops manual)", order_id,
        )
        return entry

    # Brand sub-account routing — funds come out of the same pocket.
    from models.brand import Brand
    brand_q = await db.execute(select(Brand).where(Brand.id == order.brand_id))
    brand = brand_q.scalar_one_or_none()
    if brand is None or not brand.xendit_sub_account_id:
        _LOG.warning(
            "Refund skipped for order %s — brand not Xendit-onboarded",
            order_id,
        )
        return entry

    try:
        response = await xendit_client.create_refund(
            for_user_id=brand.xendit_sub_account_id,
            invoice_id=invoice_id,
            amount_idr=amount_idr,
            reason="REQUESTED_BY_CUSTOMER",
        )
        entry.external_ref = response.get("id")
        _LOG.info(
            "Xendit refund %s kicked off for order %s",
            entry.external_ref, order_id,
        )
    except XenditError:
        entry.status = EscrowEntryStatus.FAILED
        _LOG.exception("Xendit refund FAILED for order %s", order_id)
    except Exception:  # noqa: BLE001
        entry.status = EscrowEntryStatus.FAILED
        _LOG.exception("Unexpected error kicking off refund for order %s", order_id)

    await db.flush()
    return entry


async def held_balance(db: AsyncSession, *, order_id: str) -> int:
    """Sum of HOLD - RELEASE - REFUND for an order. Always 0 or total in v1."""
    result = await db.execute(
        select(EscrowLedger).where(EscrowLedger.order_id == order_id)
    )
    rows = result.scalars().all()
    total = 0
    for r in rows:
        if r.entry_type == EscrowEntryType.HOLD:
            total += r.amount_idr
        else:
            total -= r.amount_idr
    return total
