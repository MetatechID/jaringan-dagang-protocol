"""Public QRIS PNG renderer.

Dipay's QRIS MPM generate returns ``qrContent`` (the raw EMVCo payload) —
not a hosted image. We persist that payload (``cart.qris_content`` or the
order's ``payment_method_snapshot["qris_content"]``) and render the PNG
ourselves so buyers get a stable, cacheable URL:

    GET /api/v1/qris/{partner_ref}.png

``partner_ref`` is the SNAP ``partnerReferenceNo`` we minted
(``q-{…}``, ≤32 chars) — resolved via the order snapshot's
``partner_ref`` / ``invoice_id`` keys, or the cart's ``invoice_id`` when
``invoice_provider == "dipay"``.

Rendering is done on demand with ``qrcode`` (no image storage); responses
carry a 1-hour ``Cache-Control`` since a QRIS payload never changes for a
given ref. Mock refs (``dipay-dev-*``) never resolve — they point at the
mock-checkout page instead, and this endpoint 404s for them.

Docs: https://api-docs.dipay.id/ (QRIS MPM).
"""

from __future__ import annotations

import io
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.bot_rest import Cart

_LOG = logging.getLogger("beli_aman_bap.qris")

router = APIRouter(prefix="/api/v1/qris", tags=["qris"])


def _render_png(content: str) -> bytes:
    """Encode ``content`` (an EMVCo QRIS payload) as a PNG. Kept as a
    function so the ``qrcode`` import failure degrades to a 503 at request
    time instead of breaking app boot."""
    try:
        import qrcode
    except Exception as exc:  # noqa: BLE001 — any import failure (missing dep)
        _LOG.error("qrcode library unavailable: %s", exc)
        raise HTTPException(
            503, "QRIS rendering unavailable (qrcode dependency missing)"
        ) from exc
    img = qrcode.make(content)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


async def _resolve_qr_content(db: AsyncSession, invoice_ref: str) -> str | None:
    """Find the QRIS payload for ``invoice_ref`` — orders first (snapshot
    ``partner_ref`` / ``invoice_id``), then carts (``invoice_id`` +
    ``invoice_provider == "dipay"``)."""
    # 1. Order snapshot lookup: partner_ref (the value we sent to Dipay,
    # echoed back in the callback) or invoice_id for parity with the other
    # providers' receivers.
    from models.order import Order

    order_q = await db.execute(
        select(Order).where(
            or_(
                Order.payment_method_snapshot["partner_ref"].astext == invoice_ref,
                Order.payment_method_snapshot["invoice_id"].astext == invoice_ref,
            )
        )
    )
    order = order_q.scalars().first()
    if order is not None:
        snap = order.payment_method_snapshot or {}
        return snap.get("qris_content")

    # 2. Cart lookup: bot-flow QRIS invoices live on the cart columns.
    cart_q = await db.execute(
        select(Cart).where(
            Cart.invoice_id == invoice_ref,
            Cart.invoice_provider == "dipay",
        )
    )
    cart = cart_q.scalars().first()
    if cart is not None:
        return cart.qris_content

    return None


@router.get("/{invoice_ref}.png")
async def qris_png(invoice_ref: str, db: AsyncSession = Depends(get_db)) -> Response:
    """Render the QRIS QR PNG for ``invoice_ref``. 404 for unknown refs and
    mock refs (``dipay-dev-*`` — those are mock-checkout pages, not QRIS)."""
    if invoice_ref.startswith("dipay-dev-"):
        raise HTTPException(404, "Not found")
    qr_content = await _resolve_qr_content(db, invoice_ref)
    if not qr_content:
        raise HTTPException(404, "Not found")
    png = _render_png(qr_content)
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )
