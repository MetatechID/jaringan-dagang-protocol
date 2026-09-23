"""E2E smoke test — Dipay mock-mode flow, webhook-driven order-paid path.

Mirrors `e2e-sento-mock-webhook.py`. Verifies:
  1. Brand row with payment_provider='dipay' resolves correctly.
  2. ``dipay_invoices.create_invoice_for_order()`` mints a ``dipay-dev-{id}``
     mock invoice (no env key, no per-Brand key → mock-mode branch).
  3. POSTing a synthetic Dipay QRIS notification
     (latestTransactionStatus='00') flips the order state to ESCROW_HELD
     via ``mark_order_paid``.

Pre-reqs (must already be running):
  - Postgres on :5432 (db=beli_aman, user=postgres, pwd=secret)
  - BAP on :8003 with DIPAY_CLIENT_KEY='' (mock-mode)

Run:
  cd ~/code/jaringan-dagang-protocol/apps/beli-aman-bap
  .venv/bin/python scripts/e2e-dipay-mock-webhook.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_BAP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BAP_DIR not in sys.path:
    sys.path.insert(0, _BAP_DIR)

from models.brand import Brand  # noqa: E402
from models.order import Order, OrderState  # noqa: E402
from services import dipay_invoices  # noqa: E402

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:secret@localhost:5432/beli_aman",
)
BAP_BASE = os.environ.get("BAP_BASE", "http://localhost:8003")
# Ponytail: locally the seller-bridge is usually down; flip it off in the
# BAP's process env so the webhook response doesn't hang for the full
# 30s bridge timeout. Order still reaches ESCROW_HELD — the bridge POST
# is best-effort and not on the payment-state transition path.
os.environ.setdefault("SELLER_BRIDGE_ENABLED", "false")
# Unique slug per run — keeps the script idempotent across re-runs
# (each run is a fresh Brand row, so webhook→order lookups don't cross-pollute).
BRAND_SLUG = f"safiyafood-dipay-e2e-{uuid.uuid4().hex[:8]}"


async def _pick_profile_id(session: AsyncSession) -> str:
    """Reuse an existing profile_id (orders.profile_id is FK-constrained).

    If no profile exists yet, insert a minimal stub. Real flow uses Firebase
    auth to mint these; this script is just driving the webhook→order-paid
    path which doesn't care about the buyer's identity.
    """
    import sqlalchemy as sa
    from sqlalchemy import text
    row = await session.execute(text("SELECT id FROM profiles LIMIT 1"))
    pid = row.scalar_one_or_none()
    if pid is not None:
        return pid

    # Insert a stub profile so the orders FK passes.
    new_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO profiles (id, firebase_uid, display_name, created_at, updated_at) "
            "VALUES (:id, :uid, :name, now(), now())"
        ),
        {"id": new_id, "uid": f"e2e-{uuid.uuid4().hex[:12]}", "name": "E2E Buyer"},
    )
    await session.commit()
    return new_id


async def _seed_brand(session: AsyncSession) -> Brand:
    """Insert a fresh Dipay-configured brand for this E2E run."""
    brand = Brand(
        id=str(uuid.uuid4()),
        slug=BRAND_SLUG,
        name="Safiya Food (Dipay E2E)",
        bpp_id="safiyafood-dipay-e2e.jaringan-dagang.id",
        fee_pct_bp=0,
        payment_provider="dipay",
        # Bank fields the release leg (dipay_disbursements) needs — 014 is
        # BCA in Dipay's disbursement bank-code table. Dummy account: the
        # disbursement call itself is only exercised in real mode.
        dipay_disbursement_bank_code="014",
        dipay_disbursement_bank_account="123456789",
        jubelio_enabled=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session.add(brand)
    await session.commit()
    await session.refresh(brand)
    return brand


async def _seed_order(session: AsyncSession, brand_id: str) -> Order:
    """Insert a minimal pending order against the brand."""
    profile_id = await _pick_profile_id(session)
    order = Order(
        id=str(uuid.uuid4()),
        profile_id=profile_id,
        brand_id=brand_id,
        state=OrderState.CART_REVIEWED,
        items=[{"product_id": "e2e-prod", "qty": 1, "unit_price_idr": 100_000}],
        subtotal_idr=100_000,
        shipping_idr=10_000,
        fee_idr=0,
        total_idr=110_000,
        payment_method_snapshot={"type": "dipay_qris"},
        bap_id="e2e-bap",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order


async def _mint_dipay_invoice(session: AsyncSession, order: Order) -> None:
    """Call the production service — in mock-mode this writes dipay-dev-* id."""
    await dipay_invoices.create_invoice_for_order(session, order)
    await session.commit()
    await session.refresh(order)


async def _post_dipay_qris_webhook(invoice_id: str, total_idr: int) -> httpx.Response:
    """POST a synthetic Dipay-shaped QRIS notification to BAP.

    Body mirrors the SNAP QRIS MPM callback real Dipay fires
    (``latestTransactionStatus`` '00' = success). Timeout is generous
    because ``mark_order_paid`` fires a best-effort POST to seller-bridge
    (often unreachable on local VMs) and the BAP may wait up to 30s before
    responding. The order row is committed regardless of that bridge
    call's success.

    NOTE the path: /webhooks/dipay/qris — the Dipay webhook routers mount
    WITHOUT the /api/v1 prefix (the /api/v1/webhooks/... paths in the
    older OY/Sento runbooks are stale and 404).
    """
    return httpx.post(
        f"{BAP_BASE}/webhooks/dipay/qris",
        json={
            "originalPartnerReferenceNo": invoice_id,
            "latestTransactionStatus": "00",
            "transactionStatusDesc": "SUCCESS",
            "originalExternalId": invoice_id,
            "amount": {"value": f"{int(total_idr)}.00", "currency": "IDR"},
        },
        timeout=60.0,
    )


async def _confirm_escrow_held(session: AsyncSession, order_id: str) -> OrderState:
    """Re-read the order from the DB, bypassing any identity-map cache.

    The webhook runs in BAP's session, not ours — our session still holds
    the original CART_REVIEWED snapshot. Expire before re-reading.
    """
    session.expire_all()
    order = await session.get(Order, order_id)
    if order is None:
        raise RuntimeError(f"order {order_id} disappeared")
    return order.state


async def main() -> int:
    engine = create_async_engine(DB_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as session:
        print(f"[1/4] Seeding brand slug={BRAND_SLUG} payment_provider=dipay")
        brand = await _seed_brand(session)

        print(f"[2/4] Seeding pending order for brand_id={brand.id}")
        order = await _seed_order(session, brand.id)
        print(f"      order_id={order.id} state={order.state.value}")

        print("[3/4] Minting Dipay mock invoice via create_invoice_for_order()")
        await _mint_dipay_invoice(session, order)
        snap = order.payment_method_snapshot or {}
        print(
            f"      invoice_provider={snap.get('payment_provider')!r} "
            f"invoice_id={snap.get('invoice_id')!r}"
        )
        assert snap.get("type") == "dipay_qris", (
            f"snapshot type should be 'dipay_qris', got {snap.get('type')!r}"
        )
        assert snap.get("payment_provider") == "dipay", "provider should be 'dipay'"
        assert (snap.get("invoice_id") or "").startswith("dipay-dev-"), (
            f"mock invoice id should start with 'dipay-dev-', got {snap.get('invoice_id')!r}"
        )

        # Mock-mode stores the dipay-dev-* id as invoice_id; the seller-bpp
        # mock-checkout "Mark paid" forwards that as
        # originalPartnerReferenceNo in its Dipay-shaped notification. The
        # webhook resolver matches snapshot invoice_id/partner_ref — no
        # order-/cart- prefix parsing like the Sento receiver.
        invoice_id = snap["invoice_id"]
        print(f"[4/4] POSTing Dipay QRIS notification originalPartnerReferenceNo={invoice_id}")
        resp = await _post_dipay_qris_webhook(invoice_id, order.total_idr)
        print(f"      HTTP {resp.status_code}: {resp.text[:200]}")
        assert resp.status_code == 200, f"webhook returned {resp.status_code}"

        # Re-read order — state should be ESCROW_HELD now.
        await asyncio.sleep(0.5)  # let the webhook's DB writes settle
        new_state = await _confirm_escrow_held(session, order.id)
        print(f"      order.state = {new_state.value}")
        assert new_state == OrderState.ESCROW_HELD, (
            f"expected ESCROW_HELD, got {new_state.value}"
        )

    await engine.dispose()
    print("\n✅ E2E PASS — Dipay mock invoice → webhook → ESCROW_HELD")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
