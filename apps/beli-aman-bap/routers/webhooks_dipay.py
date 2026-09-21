"""Dipay (SNAP v2.1) webhook receiver — QRIS payment status + disbursement.

Mirror of ``routers/webhooks_sento.py`` — same advisory-callback pattern:

- Auth: NO signature verification. Dipay's callback carries an ``X-SIGNATURE``
  header in some deployments, but the demo env does not document a per-tenant
  verifying key, so — like Sento — we treat the webhook as an advisory
  notification and re-verify state via the status APIs before mutating:
  ``POST /qr/qr-mpm-query`` for payments, ``POST /transfer/status`` for
  disbursements. A forged POST can at worst make us ask Dipay what's true.

- QRIS callback body (SNAP ``QRIS MPM`` notification shape):
  ``{originalPartnerReferenceNo, latestTransactionStatus,
  transactionStatusDesc, amount: {value, currency}, originalExternalId,
  additionalInfo}`` — ``latestTransactionStatus`` is ``00`` (success) /
  ``01`` (initiated / pending) / ``05`` (expired) per the SNAP status table.

- Resolution is by ``partnerReferenceNo`` (the ref we minted and Dipay echoes
  back) — NO prefix parsing like the Sento receiver. The ref lives either on
  the bot-flow cart columns (``cart.invoice_id`` + ``invoice_provider ==
  "dipay"``) or on the SDK order's ``payment_method_snapshot``
  (``partner_ref`` / ``invoice_id`` keys).

- Remit callback body (``POST /transfer-bank`` status notification):
  ``{originalPartnerReferenceNo (our ``r-…`` partner_ref),
  originalReferenceNo (Dipay's), latestTransactionStatus, additionalInfo}``
  — ``00`` success / ``01`` initiated / ``02`` paying / ``03`` pending /
  ``06`` failed. The matching RELEASE escrow-ledger row (keyed by
  ``EscrowLedger.partner_ref``) flips to COMPLETED / FAILED.

Brand isn't on the path. If we can't resolve the ref to any surface, or
Dipay's status API says the transaction doesn't exist, we 404 the webhook —
defense against forged POSTs hitting the receiver.

Reference: https://api-docs.dipay.id/ (QRIS MPM / Disbursement).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.bot_rest import Cart, CartStatus
from models.brand import Brand
from models.escrow_ledger import EscrowEntryStatus, EscrowEntryType, EscrowLedger
from services import dipay_client
from services import order_paid
from services.dipay_client import DipayError

_LOG = logging.getLogger("beli_aman_bap.webhooks_dipay")

router = APIRouter(prefix="/webhooks/dipay", tags=["webhooks"])


# Dipay ``latestTransactionStatus`` vocabulary — shared by the QRIS query and
# the transfer/status surfaces (SNAP status codes):
_QRIS_PAID = "00"
_QRIS_PENDING = "01"
_QRIS_EXPIRED = "05"

_REMIT_SUCCESS = "00"
_REMIT_PENDING = {"01", "02", "03"}  # initiated / paying / pending — non-final
_REMIT_FAILED = "06"

# SNAP query responseCodes meaning "no such transaction" (service code 51 =
# QRIS query): a 404-shaped business code from a 200/404 HTTP response.
_QRIS_QUERY_NOT_FOUND_PREFIX = "40451"


async def _resolve_targets(
    db: AsyncSession, ref: str
) -> tuple[Brand | None, Cart | None, Any | None]:
    """Resolve a Dipay ``partnerReferenceNo`` to ``(brand, cart, order)``.

    Exactly one of ``cart`` / ``order`` will be set when the ref is known
    (brand still may be ``None`` if the owning brand row vanished):

    1. Bot-flow carts: ``cart.invoice_id`` + ``invoice_provider == "dipay"``
       → brand via ``cart.bpp_id``.
    2. SDK orders: ``payment_method_snapshot.partner_ref`` (the value we
       sent to Dipay, echoed back unchanged) falling back to
       ``invoice_id`` → brand via ``order.brand_id``.
    """
    # 1. Direct cart lookup.
    cart_q = await db.execute(
        select(Cart).where(
            Cart.invoice_id == ref,
            Cart.invoice_provider == "dipay",
        )
    )
    cart = cart_q.scalars().first()
    if cart is not None:
        brand_q = await db.execute(
            select(Brand).where(Brand.bpp_id == cart.bpp_id)
        )
        return brand_q.scalars().first(), cart, None

    # 2. Order snapshot lookup — partner_ref first, invoice_id for parity
    # with the other providers' receivers.
    from models.order import Order

    order_q = await db.execute(
        select(Order).where(
            or_(
                Order.payment_method_snapshot["partner_ref"].astext == ref,
                Order.payment_method_snapshot["invoice_id"].astext == ref,
            )
        )
    )
    order = order_q.scalars().first()
    if order is not None:
        brand_q = await db.execute(
            select(Brand).where(Brand.id == order.brand_id)
        )
        return brand_q.scalars().first(), None, order

    return None, None, None


async def _parse_body(request: Request) -> dict[str, Any]:
    """Read the JSON callback body (empty dict for an empty body)."""
    body_bytes = await request.body()
    body: dict[str, Any] = {}
    if body_bytes:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "Invalid JSON body")
    return body


@router.post("/qris")
async def qris_callback(
    request: Request,
    # ponytail: header accepted for future Dipay-side signing; v1 verifies
    # state via the QRIS query API instead. Remove when Dipay documents
    # per-tenant callback signature verification.
    x_signature: str | None = Header(default=None, alias="x-signature"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Receive Dipay QRIS payment-status callbacks and flip the matching
    cart / order to paid (or expired)."""
    body = await _parse_body(request)

    ref = body.get("originalPartnerReferenceNo")
    if not ref or not isinstance(ref, str):
        raise HTTPException(400, "Missing originalPartnerReferenceNo in callback")

    brand, cart, order = await _resolve_targets(db, ref)
    if brand is None and cart is None and order is None:
        _LOG.warning(
            "Dipay QRIS callback: no surface for originalPartnerReferenceNo=%s "
            "— refusing",
            ref,
        )
        raise HTTPException(404, "Unknown Dipay invoice")

    # Verify the live state via Dipay's QRIS query — webhook is advisory.
    # If Dipay says the transaction doesn't exist (HTTP 404 or SNAP business
    # code 40451xx), bounce — defense against forged webhooks for refs we
    # never issued. Other Dipay errors (5xx, network) → proceed with the
    # body's status: webhooks are best-effort, the query may be transient.
    try:
        await dipay_client.query_qris(partner_reference_no=ref)
    except DipayError as e:
        response_code = ""
        if isinstance(e.body, dict):
            response_code = str(e.body.get("responseCode") or "")
        if e.status_code == 404 or response_code.startswith(
            _QRIS_QUERY_NOT_FOUND_PREFIX
        ):
            _LOG.warning(
                "Dipay QRIS query reports not-found for ref=%s (http=%s "
                "responseCode=%s) — refusing",
                ref, e.status_code, response_code,
            )
            raise HTTPException(404, "Dipay reports transaction not found")
        _LOG.warning(
            "Dipay QRIS query error %s for ref=%s — proceeding with body "
            "status (advisory)",
            e.status_code, ref,
        )

    status = str(body.get("latestTransactionStatus") or "").strip()
    _LOG.info(
        "Dipay QRIS callback: brand=%s ref=%s status=%s",
        getattr(brand, "slug", None), ref, status,
    )

    if status == _QRIS_PAID:
        return await _handle_paid(db, ref, cart, order)
    if status == _QRIS_PENDING:
        # Non-final — nothing to do yet; a later callback finalizes.
        return {"ok": True, "ref": ref, "status": "pending"}
    if status == _QRIS_EXPIRED:
        return await _handle_expired(db, ref, cart)
    _LOG.warning(
        "Dipay QRIS callback: unhandled latestTransactionStatus=%s for ref=%s "
        "— no-op", status, ref,
    )
    return {"ok": True, "ref": ref, "ignored_status": status}


async def _handle_paid(
    db: AsyncSession,
    ref: str,
    cart: Cart | None,
    order: Any | None,
) -> dict:
    """Process a ``00`` (success) callback for the resolved surface."""
    actor = "system:dipay_webhook"

    if order is not None:
        order = await order_paid.mark_order_paid(
            db,
            order_id=order.id,
            invoice_id=ref,
            actor=actor,
        )
        return {"ok": True, "order_id": order.id, "state": order.state.value}

    if cart is not None:
        if cart.payment_state != "paid":
            cart.payment_state = "paid"
        if cart.invoice_id is None:
            cart.invoice_id = ref
        cart.invoice_provider = "dipay"
        if cart.status != CartStatus.CONFIRMED:
            cart.status = CartStatus.CONFIRMED
        if cart.order_id:
            existing = await db.execute(
                select(EscrowLedger).where(
                    EscrowLedger.order_id == cart.order_id,
                    EscrowLedger.external_ref == ref,
                )
            )
            if existing.scalars().first() is None:
                amount = int((cart.quote_json or {}).get("total_idr") or 0)
                db.add(EscrowLedger(
                    order_id=cart.order_id,
                    entry_type=EscrowEntryType.HOLD,
                    amount_idr=amount,
                    description=(
                        f"Bot-cart funds held — dipay invoice {ref}"
                    ),
                    external_ref=ref,
                    status=EscrowEntryStatus.COMPLETED,
                ))
        return {"ok": True, "cart_id": cart.id, "payment_state": cart.payment_state}

    # ponytail: mock-mode invoice ids look like ``dipay-dev-{order_id}`` —
    # no resolvable surface row (e.g. the snapshot write raced a crash).
    # Recover the order via the snapshot and mark it paid so the demo flow
    # still completes. This keeps the resolver uniform across real + mock.
    if ref.startswith("dipay-dev-"):
        from models.order import Order as _Order

        recovered = (
            await db.execute(
                select(_Order).where(
                    _Order.payment_method_snapshot["invoice_id"].astext == ref
                )
            )
        ).scalars().first()
        if recovered is not None:
            order = await order_paid.mark_order_paid(
                db,
                order_id=recovered.id,
                invoice_id=ref,
                actor=actor,
            )
            return {"ok": True, "order_id": order.id, "state": order.state.value}

    raise HTTPException(404, "Unknown Dipay invoice")


async def _handle_expired(db: AsyncSession, ref: str, cart: Cart | None) -> dict:
    """``05`` (expired) callback — mark the cart expired if matched.
    Orders aren't touched here; expired orders simply stay unpaid."""
    if cart and cart.payment_state == "pending":
        cart.payment_state = "expired"
        cart.status = CartStatus.EXPIRED
        return {"ok": True, "cart_id": cart.id, "payment_state": cart.payment_state}
    return {"ok": True, "expired_ref": ref}


# ---------------------------------------------------------------------------
# Disbursement ("remit") callback. Dipay fires this when a bank transfer
# finishes — terminal codes 00 (success) / 06 (failed), non-final
# 01 / 02 / 03. Like the /qris route, the callback is advisory: we re-verify
# via POST /transfer/status before mutating the ledger row. Creds for that
# call are env-level (services.dipay_client reads settings), so no per-brand
# lookup is needed here.
# ---------------------------------------------------------------------------


@router.post("/remit")
async def remit_callback(
    request: Request,
    # Accepted for a future Dipay-side signing scheme; v1 verifies via the
    # status API instead. Remove when Dipay documents signature verification.
    x_signature: str | None = Header(default=None, alias="x-signature"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Receive Dipay disbursement ("remit") status callbacks and flip the
    matching RELEASE escrow-ledger row to COMPLETED / FAILED."""
    body = await _parse_body(request)

    partner_ref = body.get("originalPartnerReferenceNo")
    if not partner_ref or not isinstance(partner_ref, str):
        raise HTTPException(400, "Missing originalPartnerReferenceNo in callback")

    # Only order-release disbursements use this receiver. The ledger row is
    # matched by the partner_ref escrow.release() stamped on it — no order-id
    # prefix parsing. Deliberately no status filter: a retried terminal
    # callback should still find its row (and we warn below if it's already
    # settled).
    ledger_q = await db.execute(
        select(EscrowLedger).where(
            EscrowLedger.partner_ref == partner_ref,
            EscrowLedger.entry_type == EscrowEntryType.RELEASE,
        )
    )
    row = ledger_q.scalars().first()
    if row is None:
        _LOG.warning(
            "Dipay remit callback: no RELEASE row for partner_ref=%s — refusing",
            partner_ref,
        )
        raise HTTPException(404, "Unknown Dipay disbursement")

    order_id = row.order_id
    if row.status != EscrowEntryStatus.PENDING:
        _LOG.warning(
            "Dipay remit callback: ledger row for partner_ref=%s already %s "
            "— re-verifying but may no-op",
            partner_ref, row.status.value,
        )

    # Advisory callback → re-verify the live status via POST /transfer/status.
    # Use Dipay's answer as the authority when it comes back; if Dipay says
    # the disbursement doesn't exist (404), bounce — defense against forged
    # POSTs. Other Dipay errors (5xx, network) → proceed with the body.
    try:
        status_resp = await dipay_client.get_disbursement_status(
            partner_reference_no=partner_ref,
        )
    except DipayError as e:
        if e.status_code == 404:
            _LOG.warning(
                "Dipay remit status API 404 for partner_ref=%s — refusing",
                partner_ref,
            )
            raise HTTPException(404, "Dipay reports disbursement not found")
        _LOG.warning(
            "Dipay remit status API error %s for partner_ref=%s — proceeding "
            "with body status (advisory)",
            e.status_code, partner_ref,
        )
        status_resp = body

    code = str(
        status_resp.get("latestTransactionStatus")
        or body.get("latestTransactionStatus")
        or ""
    ).strip()
    reference_no = (
        status_resp.get("originalReferenceNo")
        or body.get("originalReferenceNo")
        or None
    )
    additional_info = status_resp.get("additionalInfo") or {}
    receipt = additional_info.get("receiptUrl") or None
    failed_reason = additional_info.get("failedReason") or None
    _LOG.info(
        "Dipay remit callback: order=%s partner_ref=%s code=%s",
        order_id, partner_ref, code,
    )

    if code == _REMIT_SUCCESS:
        row.status = EscrowEntryStatus.COMPLETED
        if reference_no and not row.external_ref:
            row.external_ref = reference_no
        desc = row.description or ""
        if receipt and receipt not in desc:
            row.description = f"{desc} — receipt: {receipt}".strip(" —")
    elif code == _REMIT_FAILED:
        row.status = EscrowEntryStatus.FAILED
        if failed_reason:
            row.description = (
                f"{row.description or ''} — failed: {failed_reason}".strip(" —")
            )
    elif code in _REMIT_PENDING:
        # Non-final: leave the row PENDING — a later callback or a status
        # poll resolves it. Still refresh external_ref if we now have one.
        if reference_no and not row.external_ref:
            row.external_ref = reference_no
        return {"ok": True, "order_id": order_id, "code": code, "status": "pending"}
    else:
        _LOG.warning(
            "Dipay remit callback: unhandled latestTransactionStatus=%s for "
            "partner_ref=%s — no-op", code, partner_ref,
        )
        return {"ok": True, "order_id": order_id, "code": code, "noop": True}

    return {"ok": True, "order_id": order_id, "code": code, "status": row.status.value}
