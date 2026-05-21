"""Trigger Xendit disbursements from a brand's XenPlatform sub-account
to that brand's registered bank account.

This is the *release* leg of escrow: money has been sitting in the brand's
Xendit sub-account balance since the buyer paid; now we wire it out to the
brand's bank. Custody stays with Xendit throughout — we orchestrate, never
touch funds ourselves.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.brand import Brand
from models.order import Order
from services import xendit_client

_LOG = logging.getLogger("beli_aman_bap.xendit_disbursements")


class DisbursementSkipped(Exception):
    """Raised when the order's brand isn't yet payout-configured.

    Caller should still record the RELEASE ledger entry (marked PENDING),
    but should treat the missing config as an operator task — Xendit can't
    move money until the brand is onboarded as a sub-account with a
    registered bank account.
    """


async def disburse_to_seller(
    db: AsyncSession,
    *,
    order: Order,
    description: str = "",
) -> dict[str, Any]:
    """Create a Xendit disbursement for ``order``.

    Returns the raw Xendit response. Caller is responsible for updating
    the matching EscrowLedger row's ``external_ref`` to the disbursement id.

    Raises ``DisbursementSkipped`` if the brand isn't fully configured —
    ops handles those manually until vibe-admin captures the bank fields.
    """
    brand_q = await db.execute(select(Brand).where(Brand.id == order.brand_id))
    brand = brand_q.scalar_one_or_none()
    if brand is None:
        raise DisbursementSkipped(f"Brand {order.brand_id} not found")
    if not brand.xendit_sub_account_id:
        raise DisbursementSkipped(
            f"Brand {brand.slug!r} has no xendit_sub_account_id"
        )
    if not (brand.xendit_disbursement_bank_code
            and brand.xendit_disbursement_bank_account
            and brand.xendit_disbursement_holder_name):
        raise DisbursementSkipped(
            f"Brand {brand.slug!r} bank fields incomplete"
        )

    response = await xendit_client.create_disbursement(
        for_user_id=brand.xendit_sub_account_id,
        external_id=f"order-{order.id}-release",
        amount_idr=order.total_idr,
        bank_code=brand.xendit_disbursement_bank_code,
        account_holder_name=brand.xendit_disbursement_holder_name,
        account_number=brand.xendit_disbursement_bank_account,
        description=description or f"Beli Aman release — order {order.id}",
    )
    return response
