"""Shared 'order was just paid' handler.

Called from:
- ``routers/webhooks_xendit.py`` when an ``invoice.paid`` callback fires
- (legacy) ``routers/payments.py:confirm-payment`` — being retired

Effects:
1. Transition CART_REVIEWED → ESCROW_HELD (idempotent — no-op if already there).
2. Write the HOLD escrow ledger entry (idempotent on ``external_ref``).
3. Best-effort dispatch to the seller's BPP via Beckn /confirm or the legacy
   seller_bridge. Failures here are non-fatal — the buyer's payment is
   already complete; the seller just sees the order via the next sync.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session
from models.brand import Brand
from models.escrow_ledger import EscrowEntryType, EscrowEntryStatus, EscrowLedger
from models.order import Order, OrderState
from models.profile import BeliAmanProfile
from models.storefront_integration import StorefrontIntegration
from services import escrow as escrow_service
from services import fb_capi
from services.state_machine import (
    StateTransitionError,
    lock_order_for_update,
    transition,
)

try:
    from services import order_flow  # type: ignore
    _ORDER_FLOW_AVAILABLE = True
except Exception:  # noqa: BLE001
    order_flow = None  # type: ignore
    _ORDER_FLOW_AVAILABLE = False

try:
    from services import beckn_orders  # type: ignore
    _BECKN_AVAILABLE = True
except Exception:  # noqa: BLE001
    beckn_orders = None  # type: ignore
    _BECKN_AVAILABLE = False

try:
    from services import seller_bridge  # type: ignore
    _SELLER_BRIDGE_AVAILABLE = True
except Exception:  # noqa: BLE001
    seller_bridge = None  # type: ignore
    _SELLER_BRIDGE_AVAILABLE = False

_LOG = logging.getLogger("beli_aman_bap.order_paid")


async def mark_order_paid(
    db: AsyncSession,
    *,
    order_id: str,
    invoice_id: str,
    actor: str = "system:xendit_webhook",
) -> Order:
    """Idempotently flip ``order_id`` to ESCROW_HELD with a HOLD ledger row.

    Caller is responsible for committing the transaction.
    """
    order = await lock_order_for_update(db, order_id)
    if order is None:
        raise ValueError(f"Order {order_id} not found")

    if order.state == OrderState.ESCROW_HELD:
        # Already paid (duplicate webhook) — make sure the ledger is also idempotent
        existing = await db.execute(
            select(EscrowLedger).where(
                EscrowLedger.order_id == order.id,
                EscrowLedger.external_ref == invoice_id,
                EscrowLedger.entry_type == EscrowEntryType.HOLD,
            )
        )
        if existing.scalars().first() is None:
            db.add(EscrowLedger(
                order_id=order.id,
                entry_type=EscrowEntryType.HOLD,
                amount_idr=order.total_idr,
                description=f"Funds held — xendit invoice {invoice_id}",
                external_ref=invoice_id,
                status=EscrowEntryStatus.COMPLETED,
            ))
        return order

    if order.state != OrderState.CART_REVIEWED:
        _LOG.warning(
            "Order %s in unexpected state %s on Xendit paid webhook — leaving",
            order.id, order.state.value,
        )
        return order

    try:
        await transition(
            db, order, OrderState.ESCROW_HELD,
            actor=actor,
            payload={"invoice_id": invoice_id},
        )
    except StateTransitionError as e:
        _LOG.warning("State transition failed for order %s: %s", order.id, e)
        return order

    db.add(EscrowLedger(
        order_id=order.id,
        entry_type=EscrowEntryType.HOLD,
        amount_idr=order.total_idr,
        description=f"Funds held — xendit invoice {invoice_id}",
        external_ref=invoice_id,
        status=EscrowEntryStatus.COMPLETED,
    ))

    # Best-effort seller dispatch — non-fatal.
    await _dispatch_to_seller(order)

    # Best-effort Meta Conversions API Purchase event. Scheduled as a
    # background task so a Meta outage or slow response can't delay the
    # webhook ACK back to Xendit. Runs in its own DB session because the
    # caller's session may already be committed/closed by the time we
    # actually fire.
    asyncio.create_task(_fire_capi_purchase_bg(order.id))

    return order


async def _fire_capi_purchase_bg(order_id: str) -> None:
    """Background task — opens its own session, sends one Purchase to Meta.

    Decoupled from the request lifecycle on purpose: the Xendit webhook
    handler returns 200 as soon as ESCROW_HELD is committed; the CAPI
    POST happens after that ACK so a slow/down Meta endpoint never causes
    Xendit to retry the webhook.
    """
    try:
        async with async_session() as session:
            order = (
                await session.execute(select(Order).where(Order.id == order_id))
            ).scalar_one_or_none()
            if order is None:
                return
            brand = (
                await session.execute(select(Brand).where(Brand.id == order.brand_id))
            ).scalar_one_or_none()
            if brand is None or not brand.slug:
                return
            integration = (
                await session.execute(
                    select(StorefrontIntegration).where(
                        StorefrontIntegration.tenant_slug == brand.slug
                    )
                )
            ).scalar_one_or_none()
            if integration is None:
                return
            if not (integration.fb_pixel_id and integration.fb_capi_access_token):
                return
            profile = (
                await session.execute(
                    select(BeliAmanProfile).where(BeliAmanProfile.id == order.profile_id)
                )
            ).scalar_one_or_none()
            if profile is None:
                return
            await fb_capi.send_purchase(
                order=order, integration=integration, profile=profile,
            )
    except Exception:  # noqa: BLE001
        _LOG.exception("CAPI Purchase background task failed for order %s", order_id)


async def _dispatch_to_seller(order: Order) -> None:
    flow_mode = (
        order_flow.beckn_order_flow_mode()
        if _ORDER_FLOW_AVAILABLE and order_flow is not None
        else "off"
    )

    order_payload: dict[str, Any] = {
        "order_id": order.id,
        "bap_id": order.bap_id,
        "bpp_id": order.bpp_id,
        "buyer": {"id": order.profile_id},
        "items": order.items,
        "subtotal_idr": order.subtotal_idr,
        "shipping_idr": order.shipping_idr,
        "total_idr": order.total_idr,
        "shipping_address": order.shipping_address,
        "escrow_status": "held",
    }

    if flow_mode in ("shadow", "on") and _ORDER_FLOW_AVAILABLE:
        try:
            await order_flow.confirm_order_v2(order_dict=order_payload)
        except Exception:  # noqa: BLE001
            _LOG.exception("order_flow.confirm_order_v2 failed (non-fatal)")
    elif flow_mode == "off" and _BECKN_AVAILABLE:
        try:
            await beckn_orders.confirm_order(order_dict=order_payload)
        except Exception:  # noqa: BLE001
            _LOG.exception("beckn_orders.confirm_order failed (non-fatal)")

    if flow_mode in ("off", "shadow") and _SELLER_BRIDGE_AVAILABLE:
        try:
            await seller_bridge.post_order(order_dict=order_payload)
        except Exception:  # noqa: BLE001
            _LOG.exception("seller_bridge.post_order failed (non-fatal)")
