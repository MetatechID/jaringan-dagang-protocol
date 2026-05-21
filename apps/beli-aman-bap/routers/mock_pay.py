"""BAP-hosted mock-pay page for v1 (until real Xendit is wired).

Customer clicks the bot's "Bayar di sini" link → lands here →
sees the cart total + a "Mark paid (sandbox)" button → POSTs back
to flip cart.payment_state to "paid". Same shape the real Xendit
hosted checkout would have.

We host this on the BAP rather than the seller because the BAP cart
is the canonical record the bot tracks; the seller's order may have
a different beckn_order_id (Beckn protocol round-trip is flaky in
v1), so a seller-keyed URL can 404. BAP cart_id is stable.

Auth: none. Possession of the BAP cart_id UUID = read/pay access
(same model Stripe + Xendit use for hosted checkout pages).
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

from database import async_session
from models.bot_rest import Cart, CartStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mock-pay", tags=["mock-pay"])


def _idr(n: float | int | None) -> str:
    if n is None:
        return "—"
    return "Rp " + format(int(n), ",d").replace(",", ".")


async def _load_cart(cart_id: str) -> Cart:
    async with async_session() as db:
        cart = (
            await db.execute(select(Cart).where(Cart.id == cart_id))
        ).scalar_one_or_none()
    if cart is None:
        raise HTTPException(404, "cart not found")
    return cart


@router.get("/{cart_id}", response_class=HTMLResponse)
async def render_mock_pay(cart_id: str) -> HTMLResponse:
    """Render the sandbox payment page."""
    cart = await _load_cart(cart_id)
    quote = cart.quote_json or {}
    subtotal = int(quote.get("subtotal_idr") or 0)
    ongkir = int(quote.get("shipping_idr") or 0)
    total = int(quote.get("total_idr") or 0)
    items = cart.items_json or []
    paid = cart.payment_state == "paid"
    confirmed = cart.status == CartStatus.CONFIRMED

    item_rows = "".join(
        f'<div class="row"><span>{(it.get("name") or it.get("sku_id") or "Produk")} × {it.get("qty") or 1}</span>'
        f'<span>{_idr(it.get("line_total_idr") or (it.get("price_idr") or 0) * (it.get("qty") or 1))}</span></div>'
        for it in items[:8]
    ) or '<div class="row" style="color:#9a8068;">(keranjang masih kosong)</div>'

    body = f"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bayar pesanan</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #fbf7f1;
         color: #3a2a1a; margin: 0; padding: 24px;
         display: flex; justify-content: center; }}
  main {{ max-width: 480px; width: 100%; background: #fff;
          border-radius: 14px; box-shadow: 0 8px 24px rgba(0,0,0,.05);
          padding: 28px; }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  p.sub {{ margin: 0 0 18px; color: #7a604a; font-size: 13px; }}
  hr {{ border: none; border-top: 1px solid #ece1d0; margin: 14px 0; }}
  .row {{ display: flex; justify-content: space-between;
          padding: 6px 0; font-size: 15px; }}
  .row.total {{ font-size: 20px; font-weight: 700; color: #6b3a1f; }}
  .row .muted {{ color: #7a604a; }}
  .btn {{ display: block; width: 100%; padding: 14px; font-size: 16px;
          background: #6b3a1f; color: #fff; border: none; border-radius: 10px;
          cursor: pointer; font-weight: 600; margin-top: 16px; }}
  .btn[disabled] {{ background: #b09a7c; cursor: not-allowed; }}
  .note {{ background: #fff8e6; color: #8a6418; padding: 10px 12px;
           border-radius: 8px; font-size: 12px; margin-top: 14px; }}
  .ok {{ color: #2c7a4a; font-weight: 600; }}
</style>
</head>
<body>
<main>
  <h1>Bayar pesanan</h1>
  <p class="sub">Sandbox payment — Xendit account belum verified.
    Klik tombol di bawah untuk men-trigger pembayaran berhasil.</p>

  {item_rows}
  <hr>
  <div class="row"><span class="muted">Subtotal</span><span>{_idr(subtotal)}</span></div>
  <div class="row"><span class="muted">Ongkir</span><span>{_idr(ongkir)}</span></div>
  <hr>
  <div class="row total"><span>Total bayar</span><span>{_idr(total)}</span></div>

  <button id="pay" class="btn" {'disabled' if paid else ''}>
    {'✓ Sudah lunas' if paid else 'Mark paid (sandbox)'}
  </button>

  <p class="note">Begitu Xendit account-mu verified, tombol ini akan
  diganti dengan QRIS + halaman Xendit asli. Status pembayaran kamu
  akan otomatis di-update lewat webhook Xendit.</p>
</main>

<script>
document.getElementById('pay').addEventListener('click', async () => {{
  const btn = document.getElementById('pay');
  btn.disabled = true; btn.textContent = 'Memproses…';
  const r = await fetch('/api/v1/mock-pay/{cart_id}/mark-paid', {{ method: 'POST' }});
  if (r.ok) {{
    btn.textContent = '✓ Berhasil — kembali ke chat';
    setTimeout(() => window.history.length > 1 ? window.history.back() : window.close(), 1500);
  }} else {{
    btn.textContent = 'Gagal — coba lagi';
    btn.disabled = false;
  }}
}});
</script>
</body>
</html>"""
    return HTMLResponse(content=body)


@router.post("/{cart_id}/mark-paid")
async def mark_paid(cart_id: str) -> JSONResponse:
    """Flip cart.payment_state to 'paid'."""
    async with async_session() as db:
        cart = (
            await db.execute(select(Cart).where(Cart.id == cart_id))
        ).scalar_one_or_none()
        if cart is None:
            raise HTTPException(404, "cart not found")
        if cart.payment_state == "paid":
            return JSONResponse({"ok": True, "already_paid": True})
        cart.payment_state = "paid"
        await db.commit()
    logger.info("mock-pay: cart %s marked paid", cart_id)
    return JSONResponse({"ok": True, "cart_id": cart_id})
