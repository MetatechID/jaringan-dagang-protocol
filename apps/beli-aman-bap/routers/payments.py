"""POST /api/v1/orders/{id}/confirm-payment — the heart of the demo.

This is where 'mock paid' happens: CART_REVIEWED → ESCROW_HELD. We write the
HOLD ledger row and best-effort POST the order to the seller.

The seller-side dispatch is gated by env ``BECKN_ORDER_FLOW`` (Task A4):

  - ``off`` (default) — legacy ``seller_bridge.post_order`` AND a Beckn
    ``/confirm`` via ``services.beckn_orders.confirm_order``. The seller's
    ``/internal/escrow-orders`` is idempotent on ``beckn_order_id`` so the
    duplicate write is safe. Pre-A4 behaviour parity.
  - ``shadow`` — same bridge POST as ``off``; additionally fires
    ``services.order_flow.confirm_order_v2`` (the new ONDC
    select→init→confirm flow with ``quote_token``). The bridge remains
    authoritative; the new path's outcome is logged for diff via
    ``beckn_outbound_log``. Recommended dual-write rollout posture.
  - ``on`` — only ``order_flow.confirm_order_v2`` fires; the legacy bridge
    is skipped and the seller's ``/internal/escrow-orders`` returns 410.

In all modes, seller dispatch failures are non-fatal for the user's payment
(the order still flips to ESCROW_HELD locally).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_profile
from models.order import Order, OrderState
from models.profile import BeliAmanProfile
from services import escrow as escrow_service
from services.state_machine import StateTransitionError, lock_order_for_update, transition

# beckn_orders pulls in pynacl-backed signer code; import lazily so that a
# missing dep can't crash the entire BAP at module load (kept whole rest of
# the API alive when pynacl was missing from requirements).
try:
    from services import beckn_orders  # type: ignore
    _BECKN_AVAILABLE = True
except Exception as _beckn_err:  # noqa: BLE001
    beckn_orders = None  # type: ignore
    _BECKN_AVAILABLE = False
    _BECKN_IMPORT_ERR = _beckn_err

try:
    from services import order_flow  # type: ignore
    _ORDER_FLOW_AVAILABLE = True
except Exception as _of_err:  # noqa: BLE001
    order_flow = None  # type: ignore
    _ORDER_FLOW_AVAILABLE = False

try:
    from services import seller_bridge  # type: ignore
    _SELLER_BRIDGE_AVAILABLE = True
except Exception:  # noqa: BLE001
    seller_bridge = None  # type: ignore
    _SELLER_BRIDGE_AVAILABLE = False

router = APIRouter(prefix="/api/v1/orders", tags=["payments"])


@router.post("/{order_id}/confirm-payment")
async def confirm_payment(
    order_id: str,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Mock 'user paid' — flips CART_REVIEWED → ESCROW_HELD. Idempotent."""
    order = await lock_order_for_update(db, order_id)
    if not order or order.profile_id != profile.id:
        raise HTTPException(404, "Order not found")

    if order.state == OrderState.ESCROW_HELD:
        # idempotent — return the existing order
        return _short(order, message="Already in ESCROW_HELD")

    try:
        await transition(
            db, order, OrderState.ESCROW_HELD,
            actor=f"buyer:{profile.id}",
            payload={"mock_paid_via": "Beli Aman Xendit-style mock"},
        )
    except StateTransitionError as e:
        raise HTTPException(409, str(e))

    await escrow_service.hold(
        db, order_id=order.id, amount_idr=order.total_idr,
        description="Funds held by Beli Aman pending receipt",
    )

    # Seller dispatch — see BECKN_ORDER_FLOW gating in module docstring.
    # Failures are NEVER fatal for the user's payment.
    flow_mode = (
        order_flow.beckn_order_flow_mode()
        if _ORDER_FLOW_AVAILABLE and order_flow is not None
        else "off"
    )

    order_payload = {
        "order_id": order.id,
        "bap_id": order.bap_id,
        "bpp_id": order.bpp_id,
        "buyer": {
            "id": profile.id,
            "email": profile.email,
            "display_name": profile.display_name,
            "photo_url": profile.photo_url,
        },
        "items": order.items,
        "subtotal_idr": order.subtotal_idr,
        "shipping_idr": order.shipping_idr,
        "total_idr": order.total_idr,
        "shipping_address": order.shipping_address,
        "escrow_status": "held",
    }

    # Beckn /confirm — fired in shadow + on. In off mode the legacy
    # beckn_orders.confirm_order is still called (pre-A4 behaviour kept
    # for backwards-compat until the seller's BPP is fully migrated).
    if flow_mode in ("shadow", "on") and _ORDER_FLOW_AVAILABLE and order_flow is not None:
        try:
            await order_flow.confirm_order_v2(order_dict=order_payload)
        except Exception:
            pass
    elif flow_mode == "off" and _BECKN_AVAILABLE and beckn_orders is not None:
        try:
            await beckn_orders.confirm_order(order_dict=order_payload)
        except Exception:
            pass

    # Legacy seller_bridge POST — fired in off + shadow. Skipped in on.
    if flow_mode in ("off", "shadow") and _SELLER_BRIDGE_AVAILABLE and seller_bridge is not None:
        try:
            await seller_bridge.post_order(order_dict=order_payload)
        except Exception:
            pass

    return _short(order, message="ESCROW_HELD")


def _short(order: Order, *, message: str) -> dict:
    return {
        "id": order.id,
        "state": order.state.value,
        "total_idr": order.total_idr,
        "message": message,
    }
