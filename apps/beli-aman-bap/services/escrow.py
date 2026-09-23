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


class ReleaseFailed(Exception):
    """Provider initiation failed; the order must not be marked released."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


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
    """Persist a release attempt, initiate payout, then return its ledger row.

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
    active = (
        await db.execute(
            select(EscrowLedger).where(
                EscrowLedger.order_id == order_id,
                EscrowLedger.entry_type == EscrowEntryType.RELEASE,
                EscrowLedger.status.in_((
                    EscrowEntryStatus.PENDING,
                    EscrowEntryStatus.COMPLETED,
                )),
            )
        )
    ).scalars().first()
    if active is not None:
        raise ReleaseFailed(
            "A release is already pending or completed for this order",
            status_code=409,
        )

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
        entry.status = EscrowEntryStatus.FAILED
        await db.commit()
        raise ReleaseFailed(f"Unknown order {order_id}")

    # Disbursement provider is per brand. A Dipay callback arrives with our
    # partnerReferenceNo, so derive it from the release-row id rather than the
    # order id: every retry has a distinct, durable correlation key.
    from models.brand import Brand
    from services import sento_disbursements
    from services.dipay_client import snap_ref

    brand_q = await db.execute(select(Brand).where(Brand.id == order.brand_id))
    brand = brand_q.scalar_one_or_none()
    provider = (brand.payment_provider if brand is not None else "xendit") or "xendit"
    if provider == "dipay":
        entry.partner_ref = snap_ref("r", str(entry.id))

    # Make the callback lookup visible before any network I/O. Callers invoke
    # release before changing order state, so this commits only the attempt.
    await db.commit()

    try:
        if provider == "sento":
            response = await sento_disbursements.disburse_to_seller(
                db, order=order, description=description,
            )
        elif provider == "dipay":
            from services import dipay_disbursements

            response = await dipay_disbursements.disburse_to_seller(
                db,
                order=order,
                partner_reference_no=entry.partner_ref,
                description=description,
                amount_idr=amount_idr,
            )
        else:
            response = await xendit_disbursements.disburse_to_seller(
                db, order=order, description=description,
            )
    except DisbursementSkipped as exc:
        entry.status = EscrowEntryStatus.FAILED
        entry.description = f"{entry.description} — initiation skipped: {exc}"
        await db.commit()
        raise ReleaseFailed(str(exc)) from exc
    except (XenditError, SentoError, DipayError) as exc:
        entry.status = EscrowEntryStatus.FAILED
        entry.description = f"{entry.description} — initiation failed: {exc}"
        await db.commit()
        _LOG.exception("%s disbursement failed for order %s", provider, order_id)
        raise ReleaseFailed(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        entry.status = EscrowEntryStatus.FAILED
        entry.description = f"{entry.description} — initiation failed"
        await db.commit()
        _LOG.exception("Unexpected disbursement failure for order %s", order_id)
        raise ReleaseFailed("Disbursement initiation failed") from exc

    entry.external_ref = response.get("id")
    if provider == "dipay" and "net_idr" in response:
        entry.gross_amount_idr = response["gross_idr"]
        entry.platform_fee_idr = response["platform_fee_idr"]
        entry.provider_fee_idr = response["dipay_fee_idr"]
        entry.net_amount_idr = response["net_idr"]
    await db.commit()
    _LOG.info(
        "%s disbursement %s accepted for order %s",
        provider,
        entry.external_ref,
        order_id,
    )
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
        entry.status = EscrowEntryStatus.FAILED
        entry.description = f"{entry.description} — unknown order"
        await db.flush()
        return entry

    invoice_id = (order.payment_method_snapshot or {}).get("invoice_id")
    if not invoice_id:
        entry.status = EscrowEntryStatus.FAILED
        entry.description = f"{entry.description} — manual: no provider invoice id"
        await db.flush()
        return entry

    # Refund support is provider-aware. Dipay has no automated refund API in
    # this integration, so record an explicit failed/manual result rather than
    # silently routing a Dipay payment through Xendit.
    from models.brand import Brand
    brand_q = await db.execute(select(Brand).where(Brand.id == order.brand_id))
    brand = brand_q.scalar_one_or_none()
    provider = (brand.payment_provider if brand is not None else "xendit") or "xendit"
    if provider == "dipay":
        entry.status = EscrowEntryStatus.FAILED
        entry.description = f"{entry.description} — manual Dipay refund required"
        await db.flush()
        return entry
    if provider != "xendit":
        entry.status = EscrowEntryStatus.FAILED
        entry.description = f"{entry.description} — manual {provider} refund required"
        await db.flush()
        return entry
    if brand is None or not brand.xendit_sub_account_id:
        entry.status = EscrowEntryStatus.FAILED
        entry.description = f"{entry.description} — brand not Xendit-onboarded"
        await db.flush()
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
        if r.status != EscrowEntryStatus.COMPLETED:
            continue
        if r.entry_type == EscrowEntryType.HOLD:
            total += r.amount_idr
        elif r.entry_type in (EscrowEntryType.RELEASE, EscrowEntryType.REFUND):
            total -= r.amount_idr
    return total
