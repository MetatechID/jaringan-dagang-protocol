"""Jardag-branded hosted payment page for partner orders.

  GET /pay/{id}          — pay page (amount, countdown, "Bayar Sekarang")
  GET /pay/{id}/done     — Xendit success-redirect fallback
  GET /pay/{id}/failed   — Xendit failure-redirect fallback
  GET /pay/{id}/status   — JSON poller the page JS uses (alias of the
                           public partner status endpoint)

The page is a holding surface: "Bayar Sekarang" hands the buyer to the
Xendit invoice page (QRIS / VA / e-wallet — Xendit stays the licensed
PJP). JS polls the public status endpoint; once the order flips to
``paid`` the page shows success and bounces to the partner's
``success_url``.

HTML pattern follows seller-bpp's ``app/api/mock_checkout.py`` but this
page is production surface (no env gate) — there is no mark-paid button;
payment happens on Xendit (or the signed mock-checkout sandbox in dev).
"""

from __future__ import annotations

import html
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import get_db
from models.brand import Brand
from services import xendit_client
from services.partner_orders import get_partner_order

router = APIRouter(prefix="/pay", tags=["partner-pay-page"])


def _idr(amount: int) -> str:
    return "Rp " + format(int(amount), ",d").replace(",", ".")


def _render_shell(title: str, body_html: str) -> str:
    return f"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, sans-serif; background: #0e1b2c;
         color: #1d2733; margin: 0; padding: 24px;
         display: flex; justify-content: center; align-items: flex-start;
         min-height: 90vh; }}
  main {{ max-width: 460px; width: 100%; background: #ffffff;
          border-radius: 16px; box-shadow: 0 12px 32px rgba(0,0,0,.18);
          padding: 30px 28px; margin-top: 3vh; }}
  .brand {{ font-size: 13px; font-weight: 700; letter-spacing: .12em;
            text-transform: uppercase; color: #2f6fed; margin-bottom: 18px; }}
  h1 {{ font-size: 19px; margin: 0 0 4px; }}
  p.desc {{ margin: 0 0 18px; color: #5c6b7c; font-size: 14px; }}
  hr {{ border: none; border-top: 1px solid #e7ecf2; margin: 16px 0; }}
  .row {{ display: flex; justify-content: space-between; padding: 6px 0;
          font-size: 14px; }}
  .row .label {{ color: #5c6b7c; }}
  .total {{ font-size: 26px; font-weight: 700; color: #10233f; }}
  .btn {{ display: block; width: 100%; padding: 15px; font-size: 16px;
          background: #2f6fed; color: #fff; border: none; border-radius: 10px;
          cursor: pointer; font-weight: 600; text-align: center;
          text-decoration: none; margin-top: 18px; }}
  .btn[disabled] {{ background: #9fb4d4; cursor: not-allowed; }}
  .paid {{ color: #1c7c46; font-weight: 700; }}
  .note {{ color: #8194a9; font-size: 12px; margin-top: 14px; line-height: 1.5; }}
  .spin {{ display: inline-block; width: 14px; height: 14px;
           border: 2px solid #c9d6e6; border-top-color: #2f6fed;
           border-radius: 50%; animation: r 0.8s linear infinite;
           vertical-align: -2px; margin-right: 6px; }}
  @keyframes r {{ to {{ transform: rotate(360deg); }} }}
</style>
</head>
<body>
<main>
  <div class="brand">Jaringan Dagang &middot; Oito</div>
  {body_html}
</main>
</body>
</html>"""


async def _load_order(db: AsyncSession, order_id: str):
    order = await get_partner_order(db, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    return order


@router.get("/{order_id}", response_class=HTMLResponse, include_in_schema=False)
async def pay_page(order_id: str, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    order = await _load_order(db, order_id)
    safe_desc = html.escape(order.description[:120])
    safe_amount = html.escape(_idr(order.amount_idr))
    order_js = json.dumps(order.id)
    paid = order.status == "paid"

    if paid:
        body = f"""
  <h1>Pembayaran berhasil</h1>
  <p class="desc">{safe_desc}</p>
  <div class="row"><span class="label">Total</span><span class="total paid">{safe_amount} &#10003;</span></div>
  <p class="note">Bila Anda tidak dialihkan otomatis, silakan kembali ke halaman merchant.</p>
  <script>
    var su = sessionStorage.getItem('oito_success_url_' + {order_js});
    if (su) setTimeout(function () {{ window.location.href = su; }}, 1500);
  </script>"""
    else:
        body = f"""
  <h1>Bayar pesanan</h1>
  <p class="desc">{safe_desc}</p>
  <div class="row"><span class="label">Total</span><span class="total">{safe_amount}</span></div>
  <div class="row"><span class="label">Order</span><span>{html.escape(order.external_order_id[:24])}</span></div>
  <a id="payBtn" class="btn" href="#">Bayar Sekarang</a>
  <p id="state" class="note"><span class="spin"></span>Menunggu pembayaran&hellip; halaman ini akan berpindah otomatis setelah pembayaran berhasil.</p>
  <p class="note">Pembayaran diproses oleh Xendit (PJP berlisensi) atas nama Jaringan Dagang (Oito).
     Jangan tutup halaman ini sebelum pembayaran selesai.</p>
  <script>
    var orderId = {order_js};
    var payBtn = document.getElementById('payBtn');
    var state = document.getElementById('state');
    var paying = false;

    function startPay() {{
      if (paying) return;
      paying = true;
      payBtn.disabled = true;
      payBtn.textContent = 'Membuka halaman pembayaran…';
      fetch('/api/v1/partner/public/orders/' + orderId + '/pay-target')
        .then(function (r) {{ if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }})
        .then(function (d) {{ window.location.href = d.pay_url; }})
        .catch(function () {{
          payBtn.disabled = false; paying = false;
          payBtn.textContent = 'Coba lagi';
        }});
    }}
    payBtn.addEventListener('click', function (e) {{ e.preventDefault(); startPay(); }});

    setInterval(function () {{
      fetch('/pay/' + orderId + '/status')
        .then(function (r) {{ return r.ok ? r.json() : null; }})
        .then(function (d) {{
          if (!d) return;
          if (d.status === 'paid') {{
            state.innerHTML = '<span class="paid">&#10003; Pembayaran diterima.</span>';
            payBtn.style.display = 'none';
            if (d.success_url) setTimeout(function () {{ window.location.href = d.success_url; }}, 1500);
          }} else if (d.status === 'expired') {{
            state.textContent = 'Pesanan ini telah kedaluwarsa. Silakan buat pesanan baru.';
            payBtn.style.display = 'none';
          }}
        }})
        .catch(function () {{}});
    }}, 4000);
  </script>"""
    return HTMLResponse(content=_render_shell("Bayar pesanan · Jaringan Dagang", body))


@router.get("/{order_id}/pay-target", include_in_schema=False)
async def pay_target(order_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """The URL "Bayar Sekarang" opens — the Xendit invoice page, or the
    signed mock-checkout sandbox in mock mode. Indirection keeps the mock
    invoice URL out of the static page HTML."""
    order = await _load_order(db, order_id)
    if not order.invoice_id:
        raise HTTPException(409, "Invoice not ready — retry shortly")
    if order.status == "expired":
        raise HTTPException(410, "Order expired")

    if (order.invoice_id or "").startswith("dev-partner-"):
        base = (
            getattr(settings, "mock_checkout_public_base", None)
            or "https://jaringan-dagang-seller-api.metatech.id"
        ).rstrip("/")
        return {"pay_url": f"{base}/api/mock-checkout/{order.invoice_id}"}

    invoice = await _get_invoice_safe(db, order)
    if invoice and invoice.get("invoice_url"):
        return {"pay_url": invoice["invoice_url"]}
    raise HTTPException(502, "Invoice URL unavailable")


async def _get_invoice_safe(db: AsyncSession, order) -> dict | None:
    """Fetch the invoice URL from Xendit. Returns None on any error — the
    page degrades to "retry" instead of 500ing the buyer."""
    try:
        brand = (
            await db.execute(select(Brand).where(Brand.id == order.brand_id))
        ).scalar_one_or_none()
        if brand is None or not brand.xendit_sub_account_id:
            return None
        return await xendit_client.get_invoice(
            for_user_id=brand.xendit_sub_account_id, invoice_id=order.invoice_id,
        )
    except Exception:  # noqa: BLE001
        return None


@router.get("/{order_id}/status", include_in_schema=False)
async def pay_page_status(order_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Compact poller for the page JS — same data as the public partner
    status endpoint, mounted under /pay so the page has a same-origin URL."""
    order = await _load_order(db, order_id)
    return {
        "status": order.status,
        "amount_idr": order.amount_idr,
        "paid_at": order.paid_at.isoformat() if order.paid_at else None,
        "success_url": order.success_url,
    }


@router.get("/{order_id}/done", response_class=HTMLResponse, include_in_schema=False)
async def pay_done(order_id: str, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    order = await _load_order(db, order_id)
    if order.status == "paid" and order.success_url:
        return HTMLResponse(content=_render_shell(
            "Pembayaran berhasil",
            f"""<h1>Pembayaran berhasil</h1>
  <p class="desc">Mengalihkan kembali&hellip;</p>
  <script>window.location.href = {json.dumps(order.success_url)};</script>""",
        ))
    return await pay_page(order_id, db)


@router.get("/{order_id}/failed", response_class=HTMLResponse, include_in_schema=False)
async def pay_failed(order_id: str, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    order = await _load_order(db, order_id)
    back = f"/pay/{html.escape(order.id)}"
    return HTMLResponse(content=_render_shell(
        "Pembayaran belum selesai",
        f"""<h1>Pembayaran belum selesai</h1>
  <p class="desc">Pembayaran untuk pesanan ini belum berhasil. Silakan coba lagi.</p>
  <a class="btn" href="{back}">Kembali ke halaman pembayaran</a>""",
    ))
