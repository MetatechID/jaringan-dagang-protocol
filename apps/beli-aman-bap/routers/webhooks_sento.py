"""Sento payment gateway webhook receiver.

Mirror of ``routers/webhooks_oy.py`` — same /invoice flow, but:

- Auth: NO HMAC signature. Sento's docs do not document HMAC signing, so
  we verify state via the status API instead. The webhook body alone is
  not sufficient to mutate state — we re-check Sento's live status before
  doing anything. The webhook is treated as an advisory notification.

- Body: **Payment Link** shape (flat body, no ``payment_info`` wrapper).
  ``status`` is lowercase but uses **different vocabulary** than the
  Status API: the callback sends ``success | failed | processing``
  while the Status API returns ``created | waiting_payment | expired |
  charge_in_progress | failed | complete | closed``. We normalize both
  in ``_parse_status``. The invoice id is read from ``partner_tx_id``;
  Sento's internal id echoes back as ``tx_ref_number`` and is used as
  the ledger ``external_ref`` when available.

Brand isn't on the path. We resolve it from the body's ``partner_tx_id``
via the same cart/order snapshot lookup used by OY. If Sento says the
invoice is not found (404 from status API), we 404 the webhook — defense
against forged POSTs hitting the receiver.

Reference: https://api-docs.sento.id/docs-page/payment-link
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
from services import order_paid
from services import sento_client
from services.sento_client import SentoError

_LOG = logging.getLogger("beli_aman_bap.webhooks_sento")

router = APIRouter(prefix="/webhooks/sento", tags=["webhooks"])


def _parse_status(body: dict) -> str | None:
    """Payment Link callback uses lowercase ``status``. Returns one of
    ``complete | expired | failed | closed | pending | None``.

    Status vocabulary differs between Sento surfaces:
    - Status API (GET): created / waiting_payment / expired /
      charge_in_progress / failed / complete / closed
    - Callback (POST): success / failed / processing
    We normalize both into a single vocabulary.
    """
    raw = str(body.get("status") or "").lower().strip()
    if not raw:
        return None
    if raw in {"created", "waiting_payment", "charge_in_progress", "processing"}:
        return "pending"
    if raw in {"complete", "expired", "failed", "closed"}:
        return raw
    # Payment Link callback uses "success" instead of "complete".
    if raw == "success":
        return "complete"
    return None


async def _resolve_brand_for_invoice_id(
    db: AsyncSession, partner_tx_id: str
) -> Brand | None:
    """Find the brand that owns ``partner_tx_id`` via cart or order snapshot."""
    # 1. Direct cart lookup: cart.invoice_id + invoice_provider=='sento'.
    cart_q = await db.execute(
        select(Cart).where(
            Cart.invoice_id == partner_tx_id,
            Cart.invoice_provider == "sento",
        )
    )
    cart = cart_q.scalars().first()
    if cart is not None:
        brand_q = await db.execute(
            select(Brand).where(Brand.bpp_id == cart.bpp_id)
        )
        return brand_q.scalars().first()

    # 2. Order snapshot lookup: payment_method_snapshot.partner_tx_id
    # (the value we sent to Sento, which Sento echoes back unchanged).
    # Falls back to invoice_id for older orders created before we stored
    # partner_tx_id explicitly.
    from models.order import Order

    order_q = await db.execute(
        select(Order).where(
            or_(
                Order.payment_method_snapshot["partner_tx_id"].astext == partner_tx_id,
                Order.payment_method_snapshot["invoice_id"].astext == partner_tx_id,
            )
        )
    )
    order = order_q.scalars().first()
    if order is not None:
        brand_q = await db.execute(select(Brand).where(Brand.id == order.brand_id))
        return brand_q.scalars().first()

    return None


@router.post("/invoice")
async def invoice_callback(
    request: Request,
    # ponytail: header accepted for future Sento-side signing; v1 verifies
    # state via the status API instead. Remove when Sento documents signing.
    x_sento_signature: str | None = Header(default=None, alias="x-sento-signature"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Receive Sento Payment Link status callbacks (flat body, lowercase
    ``status``)."""
    body_bytes = await request.body()
    body: dict[str, Any] = {}
    if body_bytes:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "Invalid JSON body")

    partner_tx_id = body.get("partner_tx_id")
    if not partner_tx_id or not isinstance(partner_tx_id, str):
        raise HTTPException(400, "Missing partner_tx_id in callback")

    brand = await _resolve_brand_for_invoice_id(db, partner_tx_id)
    if brand is None:
        _LOG.warning(
            "Sento callback: no Brand for partner_tx_id=%s — refusing",
            partner_tx_id,
        )
        raise HTTPException(404, "Unknown Sento invoice")

    # Verify the live state via Sento's status API — webhook is advisory.
    # If Sento says the invoice is not found (404), bounce — defense against
    # forged webhooks hitting us for partner_tx_ids we never issued. For
    # other Sento errors (5xx, network), we proceed with the body's status:
    # webhooks are best-effort, status API may be transient.
    try:
        await sento_client.get_status(
            partner_tx_id=partner_tx_id,
            api_key=brand.sento_api_key,
            username=brand.sento_username,
        )
    except SentoError as e:
        if e.status_code == 404:
            _LOG.warning(
                "Sento status API returned 404 for partner_tx_id=%s — refusing",
                partner_tx_id,
            )
            raise HTTPException(404, "Sento reports invoice not found")
        _LOG.warning(
            "Sento status API error %s for partner_tx_id=%s — proceeding "
            "with body status (advisory)",
            e.status_code, partner_tx_id,
        )

    status = _parse_status(body)
    if not status:
        return {"ok": True, "ignored_status": body.get("status", "")}
    _LOG.info(
        "Sento invoice callback: brand=%s partner_tx_id=%s status=%s",
        getattr(brand, "slug", None), partner_tx_id, status,
    )

    if status == "complete":
        return await _handle_paid(db, partner_tx_id, body)
    if status == "expired":
        return await _handle_expired(db, partner_tx_id, body)
    if status in ("failed", "closed"):
        return await _handle_failed(db, partner_tx_id)
    return {"ok": True, "ignored_status": status}


async def _handle_paid(db: AsyncSession, partner_tx_id: str, body: dict) -> dict:
    """Process a ``complete`` callback (Payment Link).

    The Payment Link flat body carries ``tx_ref_number`` (Sento's internal
    id) directly. We prefer it as ``invoice_id`` so the ledger row's
    ``external_ref`` matches what Sento echoes in the callback; falls back
    to ``partner_tx_id`` if absent.
    """
    invoice_id = str(body.get("tx_ref_number") or partner_tx_id)
    actor = "system:sento_webhook"

    if partner_tx_id.startswith("order-"):
        order_id = partner_tx_id[len("order-"):]
        order = await order_paid.mark_order_paid(
            db,
            order_id=order_id,
            invoice_id=invoice_id,
            actor=actor,
        )
        return {"ok": True, "order_id": order.id, "state": order.state.value}

    if partner_tx_id.startswith("cart-"):
        cart_id = partner_tx_id[len("cart-"):]
        cart = (
            await db.execute(select(Cart).where(Cart.id == cart_id))
        ).scalar_one_or_none()
        if cart is None:
            raise HTTPException(404, f"No cart for partner_tx_id={partner_tx_id}")
        if cart.payment_state != "paid":
            cart.payment_state = "paid"
        if cart.invoice_id is None:
            cart.invoice_id = partner_tx_id
        cart.invoice_provider = "sento"
        if cart.status != CartStatus.CONFIRMED:
            cart.status = CartStatus.CONFIRMED
        if cart.order_id:
            existing = await db.execute(
                select(EscrowLedger).where(
                    EscrowLedger.order_id == cart.order_id,
                    EscrowLedger.external_ref == partner_tx_id,
                )
            )
            if existing.scalars().first() is None:
                amount = int((cart.quote_json or {}).get("total_idr") or 0)
                db.add(EscrowLedger(
                    order_id=cart.order_id,
                    entry_type=EscrowEntryType.HOLD,
                    amount_idr=amount,
                    description=(
                        f"Bot-cart funds held — sento invoice {invoice_id}"
                    ),
                    external_ref=partner_tx_id,
                    status=EscrowEntryStatus.COMPLETED,
                ))
        return {"ok": True, "cart_id": cart.id, "payment_state": cart.payment_state}

    # ponytail: mock-mode invoice ids look like ``sento-dev-{order_id}`` —
    # no prefix to dispatch on. Recover the order via the snapshot and
    # mark it paid. This keeps the resolver uniform across real + mock.
    from models.order import Order as _Order
    recovered = (
        await db.execute(
            select(_Order).where(
                _Order.payment_method_snapshot["invoice_id"].astext == partner_tx_id
            )
        )
    ).scalars().first()
    if recovered is not None:
        order = await order_paid.mark_order_paid(
            db,
            order_id=recovered.id,
            invoice_id=invoice_id,
            actor=actor,
        )
        return {"ok": True, "order_id": order.id, "state": order.state.value}

    raise HTTPException(400, f"Unrecognized partner_tx_id prefix: {partner_tx_id!r}")


async def _handle_expired(db: AsyncSession, partner_tx_id: str, body: dict) -> dict:
    if partner_tx_id.startswith("cart-"):
        cart_id = partner_tx_id[len("cart-"):]
        cart = (
            await db.execute(select(Cart).where(Cart.id == cart_id))
        ).scalar_one_or_none()
        if cart and cart.payment_state == "pending":
            cart.payment_state = "expired"
            cart.status = CartStatus.EXPIRED
            return {"ok": True, "cart_id": cart.id, "payment_state": cart.payment_state}
    return {"ok": True, "expired_partner_tx_id": partner_tx_id}


async def _handle_failed(db: AsyncSession, partner_tx_id: str) -> dict:
    """``failed`` / ``closed`` callback — mark cart as failed if matched.
    We don't touch order rows here; those flow through ``_handle_paid``
    only.
    """
    cart_q = await db.execute(
        select(Cart).where(
            Cart.invoice_id == partner_tx_id,
            Cart.invoice_provider == "sento",
        )
    )
    cart = cart_q.scalars().first()
    if cart and cart.payment_state in ("pending", None):
        cart.payment_state = "failed"
        return {"ok": True, "cart_id": cart.id, "payment_state": cart.payment_state}
    return {"ok": True, "partner_tx_id": partner_tx_id, "failed": True}


# ---------------------------------------------------------------------------
# Disbursement ("remit") callback. Sento fires this when a disbursement
# finishes — terminal codes 000 (success) / 300 (failed) / 301 (pending).
# The callback URL is dashboard-configured (Settings → Developer Option →
# Callback Configuration → "API Disbursement"), NOT per-request, and Sento
# does not document HMAC signing — so, like the /invoice route, we treat the
# callback as advisory and re-verify via the status API before mutating.
# ---------------------------------------------------------------------------

# Sento disbursement status codes (see sento-docs Fund Disbursement). Final:
_REMIT_SUCCESS = "000"
_REMIT_FAILED = {"300", "206", "225"}  # failed / balance-not-enough / over-limit
_REMIT_PENDING = "301"                  # unclear answer from bank network — stay PENDING


@router.post("/remit")
async def remit_callback(
    request: Request,
    # Accepted for a future Sento-side signing scheme; v1 verifies via the
    # status API instead. Remove when Sento documents signing.
    x_sento_signature: str | None = Header(default=None, alias="x-sento-signature"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Receive Sento disbursement ("remit") status callbacks and flip the
    matching PENDING RELEASE escrow-ledger row to COMPLETED / FAILED."""
    body_bytes = await request.body()
    if not body_bytes:
        raise HTTPException(400, "Empty callback body")
    try:
        body: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "Invalid JSON body")

    partner_tx_id = body.get("partner_tx_id")
    if not partner_tx_id or not isinstance(partner_tx_id, str):
        raise HTTPException(400, "Missing partner_tx_id in callback")
    # Only order-release disbursements use this receiver. Carts have no
    # disbursement leg. ``partner_tx_id`` shape: "order-{id}-release".
    if not partner_tx_id.startswith("order-"):
        raise HTTPException(400, f"Unrecognized partner_tx_id prefix: {partner_tx_id!r}")

    order_id = partner_tx_id[len("order-"):]
    if order_id.endswith("-release"):
        order_id = order_id[: -len("-release")]

    from models.order import Order as _Order

    order = (
        await db.execute(select(_Order).where(_Order.id == order_id))
    ).scalar_one_or_none()
    if order is None:
        _LOG.warning(
            "Sento remit callback: no Order for partner_tx_id=%s — refusing",
            partner_tx_id,
        )
        raise HTTPException(404, "Unknown Sento disbursement")

    # Resolve the brand to source Sento creds for the status-API re-check.
    brand_q = await db.execute(select(Brand).where(Brand.id == order.brand_id))
    brand = brand_q.scalars().first()
    if brand is None:
        _LOG.warning(
            "Sento remit callback: no Brand for order %s — refusing", order_id,
        )
        raise HTTPException(404, "Unknown brand for disbursement")

    # Advisory callback → re-verify the live status. If Sento says the
    # disbursement doesn't exist (204), bounce — defense against forged POSTs.
    # For other Sento errors (5xx, network) proceed with the body's status.
    try:
        status_resp = await sento_client.get_disbursement_status(
            partner_tx_id=partner_tx_id,
            api_key=brand.sento_api_key,
            username=brand.sento_username,
        )
    except SentoError as e:
        if e.status_code == 404:
            _LOG.warning(
                "Sento remit status API 404 for partner_tx_id=%s — refusing",
                partner_tx_id,
            )
            raise HTTPException(404, "Sento reports disbursement not found")
        _LOG.warning(
            "Sento remit status API error %s for partner_tx_id=%s — "
            "proceeding with body status (advisory)",
            e.status_code, partner_tx_id,
        )
        status_resp = body

    code = str((status_resp.get("status") or {}).get("code") or "")
    trx_id = status_resp.get("trx_id") or body.get("trx_id") or None
    _LOG.info(
        "Sento remit callback: order=%s partner_tx_id=%s code=%s",
        order_id, partner_tx_id, code,
    )

    # Find the PENDING RELEASE ledger row for this order. (escrow.release
    # writes exactly one RELEASE row per release; match on order + type +
    # PENDING so we only flip an unsettled row.)
    ledger_q = await db.execute(
        select(EscrowLedger).where(
            EscrowLedger.order_id == order_id,
            EscrowLedger.entry_type == EscrowEntryType.RELEASE,
            EscrowLedger.status == EscrowEntryStatus.PENDING,
        )
    )
    row = ledger_q.scalars().first()
    if row is None:
        _LOG.warning(
            "Sento remit callback: no PENDING RELEASE row for order %s "
            "(already settled or never released?) — no-op", order_id,
        )
        return {"ok": True, "order_id": order_id, "code": code, "noop": True}

    if code == _REMIT_SUCCESS:
        row.status = EscrowEntryStatus.COMPLETED
        if trx_id:
            row.external_ref = trx_id
        desc = row.description or ""
        receipt = status_resp.get("receipt_url") or body.get("receipt_url")
        if receipt and receipt not in desc:
            row.description = f"{desc} — receipt: {receipt}".strip(" —")
    elif code in _REMIT_FAILED:
        row.status = EscrowEntryStatus.FAILED
        reason = (status_resp.get("tx_status_description")
                  or body.get("tx_status_description")
                  or (status_resp.get("status") or {}).get("message"))
        if reason:
            row.description = f"{row.description or ''} — failed: {reason}".strip(" —")
    elif code == _REMIT_PENDING:
        # Non-final: leave the row PENDING — a later callback or a status poll
        # resolves it. Still refresh external_ref if we now have a trx_id.
        if trx_id and not row.external_ref:
            row.external_ref = trx_id
        return {"ok": True, "order_id": order_id, "code": code, "status": "pending"}
    else:
        _LOG.warning(
            "Sento remit callback: unhandled code=%s for order %s — no-op",
            code, order_id,
        )
        return {"ok": True, "order_id": order_id, "code": code, "noop": True}

    return {"ok": True, "order_id": order_id, "code": code, "status": row.status.value}