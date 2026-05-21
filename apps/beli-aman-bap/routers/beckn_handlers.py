"""Beckn on_* handler implementations.

Each `handle_on_<action>(context, message, db)` mutates state in response to an
inbound signed Beckn callback. Returning None falls back to the default ACK.

Phase 2 wires handle_on_search → mirror upsert.
Later phases will wire on_select/on_init/on_confirm/on_status/on_update.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.mirror import (
    MirrorProduct,
    MirrorProductImage,
    MirrorSKU,
    MirrorSKUImage,
    MirrorStore,
)
from models.order import Order

logger = logging.getLogger(__name__)


# Canonical subscriber_id -> storefront slug (Task A3, scheme:
# ``<slug>.jaringan-dagang.id``). Legacy ``bpp.*.local`` entries are kept
# as transitional fallbacks until the live DB is migrated via
# ``jaringan-dagang-seller/scripts/migrate-subscriber-ids.py``; drop the
# .local rows once migration is applied in prod.
_SLUG_OVERRIDES = {
    # Canonical scheme.
    "safiyafood.jaringan-dagang.id": "safiyafood",
    "antarestar.jaringan-dagang.id": "antarestar",
    "gendes.jaringan-dagang.id": "gendes",
    "yourbrand.jaringan-dagang.id": "yourbrand",
    "matchamu.jaringan-dagang.id": "matchamu",
    "optimumnutrition.jaringan-dagang.id": "optimumnutrition",
    "bpp.jaringan-dagang.id": "default",
    # Legacy — transitional fallbacks.
    "bpp.antarestar.local": "antarestar",
    "bpp.gendes.local": "gendes",
    "bpp.yourbrand.local": "yourbrand",
    "bpp.jaringan-dagang.local": "default",
}


def _slug_from_bpp_id(bpp_id: str, name: str | None = None) -> str:
    """Map a Beckn subscriber_id to a storefront slug.

    Tries (in order): override map → ``<slug>.jaringan-dagang.id`` /
    legacy ``<slug>.bpp.<anything>`` pattern → name slugified.
    """
    if bpp_id in _SLUG_OVERRIDES:
        return _SLUG_OVERRIDES[bpp_id]
    parts = bpp_id.split(".")
    # Canonical: <slug>.jaringan-dagang.id (3 parts where the slug is not
    # the network-role tag itself).
    if (
        len(parts) == 3
        and parts[0] not in ("bpp", "bap")
        and parts[-2:] == ["jaringan-dagang", "id"]
    ):
        return parts[0]
    # Legacy: <slug>.bpp.metatech.id (the second segment marks role).
    if len(parts) >= 3 and parts[1] == "bpp":
        return parts[0]
    if name:
        import re
        slugged = re.sub(r"[^a-z0-9]+", "", name.lower())
        if slugged:
            return slugged
    return bpp_id.replace(".", "-")


async def handle_on_search(
    context: dict[str, Any],
    message: dict[str, Any],
    db: AsyncSession,
) -> dict | None:
    """Upsert the catalog payload into mirror_* tables.

    Strategy for v1: full-replace per (bpp_id, provider). Catalogs are small
    (<200 SKUs/toko); delta optimization comes later.
    """
    bpp_id = context.get("bpp_id") or ""
    catalog = message.get("catalog") or {}
    providers = catalog.get("bpp/providers") or catalog.get("providers") or []

    if not providers:
        logger.info("on_search from %s: empty catalog", bpp_id)
        return None

    bpp_uri = context.get("bpp_uri")
    now = datetime.now(timezone.utc)

    for provider in providers:
        provider_id = provider.get("id") or bpp_id
        store_name = (provider.get("descriptor") or {}).get("name") or provider_id
        slug = _slug_from_bpp_id(provider_id, store_name)
        logo = None
        imgs = (provider.get("descriptor") or {}).get("images") or []
        if imgs:
            logo = imgs[0] if isinstance(imgs[0], str) else imgs[0].get("url")

        # Look up by bpp_id first (preferred), then by slug (so a toko that
        # rotated/updated its subscriber_id keeps the same mirror row).
        store = (
            await db.execute(
                select(MirrorStore).where(MirrorStore.bpp_id == provider_id)
            )
        ).scalar_one_or_none()
        if store is None:
            store = (
                await db.execute(
                    select(MirrorStore).where(MirrorStore.slug == slug)
                )
            ).scalar_one_or_none()
            if store is not None and store.bpp_id != provider_id:
                logger.info("MirrorStore slug=%s: bpp_id %s -> %s (rotation/rename)",
                            slug, store.bpp_id, provider_id)
                store.bpp_id = provider_id
        if store is None:
            store = MirrorStore(
                bpp_id=provider_id,
                slug=slug,
                name=store_name,
                logo_url=logo,
                bpp_uri=bpp_uri,
            )
            db.add(store)
            await db.flush()
        else:
            store.name = store_name
            if logo:
                store.logo_url = logo
            if bpp_uri:
                store.bpp_uri = bpp_uri
        store.last_pushed_at = now

        # Full replace of this store's products. Use a single bulk DELETE +
        # bulk INSERT pass instead of per-row deletes/flushes — full-replace on
        # ~40 SKUs was hitting Vercel function-timeout previously.
        from sqlalchemy import delete as _delete
        await db.execute(_delete(MirrorProduct).where(MirrorProduct.store_id == store.id))

        # Group items by parent_item_id so we re-build the Product → SKU hierarchy.
        items = provider.get("items") or []
        by_parent: dict[str, list[dict]] = {}
        for item in items:
            parent = item.get("parent_item_id") or item.get("id")
            by_parent.setdefault(parent, []).append(item)

        # Build all ORM objects in memory with pre-assigned UUIDs so we can
        # bulk-add without intermediate flushes.
        new_products: list[MirrorProduct] = []
        new_skus: list[MirrorSKU] = []
        new_prod_imgs: list[MirrorProductImage] = []
        new_sku_imgs: list[MirrorSKUImage] = []

        for parent_id, group in by_parent.items():
            first = group[0]
            desc = first.get("descriptor") or {}
            name = desc.get("name") or parent_id
            prod_id = str(uuid.uuid4())
            new_products.append(MirrorProduct(
                id=prod_id,
                store_id=store.id,
                bpp_product_id=parent_id,
                sku=parent_id,
                name=name,
                description=desc.get("long_desc") or desc.get("short_desc"),
                status="ACTIVE",
                attributes=first.get("tags") or None,
            ))
            for i, img in enumerate(desc.get("images") or []):
                url = img if isinstance(img, str) else img.get("url")
                if url:
                    new_prod_imgs.append(MirrorProductImage(
                        id=str(uuid.uuid4()), product_id=prod_id,
                        url=url, position=i, is_primary=(i == 0),
                    ))

            for item in group:
                idesc = item.get("descriptor") or {}
                price_obj = item.get("price") or {}
                qty_obj = (item.get("quantity") or {}).get("available") or {}
                variant_name = None
                variant_value = None
                for tag in item.get("tags") or []:
                    if tag.get("code") == "variant":
                        for kv in tag.get("list") or []:
                            code = kv.get("code") or (kv.get("descriptor") or {}).get("code")
                            if code == "name":
                                variant_name = kv.get("value")
                            elif code == "value":
                                variant_value = kv.get("value")
                try:
                    price = float(price_obj.get("value") or 0)
                except (TypeError, ValueError):
                    price = 0.0
                try:
                    original = float(price_obj.get("maximum_value") or price)
                except (TypeError, ValueError):
                    original = price
                try:
                    stock = int(qty_obj.get("count") or 0)
                except (TypeError, ValueError):
                    stock = 0

                sku_id = str(uuid.uuid4())
                new_skus.append(MirrorSKU(
                    id=sku_id, product_id=prod_id,
                    bpp_sku_id=item.get("id") or "",
                    variant_name=variant_name,
                    variant_value=variant_value,
                    sku_code=idesc.get("code") or item.get("id") or "",
                    price=price, original_price=original, stock=stock,
                ))
                for i, img in enumerate(idesc.get("images") or []):
                    url = img if isinstance(img, str) else img.get("url")
                    if url:
                        new_sku_imgs.append(MirrorSKUImage(
                            id=str(uuid.uuid4()), sku_id=sku_id, url=url,
                            position=i, is_primary=(i == 0),
                        ))

        # Bulk add — one batch per kind. SQLAlchemy will issue one INSERT
        # statement per kind (or batched multi-row INSERTs).
        if new_products:
            db.add_all(new_products)
        if new_prod_imgs:
            db.add_all(new_prod_imgs)
        if new_skus:
            db.add_all(new_skus)
        if new_sku_imgs:
            db.add_all(new_sku_imgs)
        # Single flush per provider to surface FK errors early
        await db.flush()

    return None  # default ACK


async def handle_on_status(
    context: dict[str, Any],
    message: dict[str, Any],
    db: AsyncSession,
) -> dict | None:
    """Update local Order with fulfillment state from /on_status."""
    order_msg = message.get("order") or {}
    bpp_order_id = order_msg.get("id")
    if not bpp_order_id:
        return None

    # Match either by seller_order_ref or our local id (UUID string).
    candidate = (await db.execute(
        select(Order).where(Order.seller_order_ref == bpp_order_id)
    )).scalar_one_or_none()
    if candidate is None and len(bpp_order_id) == 36:
        # bpp_order_id might be our local id in some flows
        candidate = (await db.execute(
            select(Order).where(Order.id == bpp_order_id)
        )).scalar_one_or_none()
    if candidate is None:
        logger.info("on_status for unknown order %s — ignoring", bpp_order_id)
        return None

    fulfillments = order_msg.get("fulfillments") or []
    if not fulfillments:
        return None
    f = fulfillments[0]
    state_code = (f.get("state") or {}).get("descriptor", {}).get("code")
    if state_code:
        candidate.fulfillment_status = state_code
    awb = f.get("tracking_id")
    if awb:
        candidate.fulfillment_awb = awb
    track_url = f.get("tracking_url")
    if track_url:
        candidate.fulfillment_tracking_url = track_url
    from datetime import datetime, timezone
    candidate.fulfillment_last_event_at = datetime.now(timezone.utc)
    return None


async def handle_on_confirm(
    context: dict[str, Any],
    message: dict[str, Any],
    db: AsyncSession,
) -> dict | None:
    """Record the seller-assigned order id when /confirm completes.

    Also surfaces the Xendit QR image URL back onto the bot-side ``Cart``
    row (B3a). The MCP bot polls ``GET /api/v1/checkout/{cart_id}/status``
    waiting for ``payment_state="paid"`` + a ``qr_image_url`` to show the
    customer; without this linkage the cart stays ``pending`` forever.

    Both updates are idempotent: Beckn may retry /on_confirm for the same
    transaction_id, and we MUST NOT flip a ``cancelled``/``expired`` cart
    back to ``paid``.
    """
    order_msg = message.get("order") or {}
    bpp_order_id = order_msg.get("id")
    txn_id = context.get("transaction_id")
    if not bpp_order_id or not txn_id:
        return None

    # 1. Storefront Order update (existing behaviour).
    candidate = (await db.execute(
        select(Order).where(Order.seller_order_ref == bpp_order_id)
    )).scalar_one_or_none()
    if candidate is not None:
        candidate.seller_order_ref = bpp_order_id

    # 2. Bot-REST Cart update — only fires when this confirm came from a
    #    bot-driven /checkout/{cart_id}/confirm (matched by transaction_id).
    #    Customers checking out through the Firebase storefront have no Cart
    #    row, so a miss is normal — don't error.
    from models.bot_rest import Cart, CartStatus
    from services.order_flow import extract_qr_image_url

    cart = (await db.execute(
        select(Cart).where(Cart.transaction_id == txn_id)
    )).scalar_one_or_none()
    if cart is not None and cart.status not in (
        CartStatus.EXPIRED,
    ):
        # Re-build the payload shape extract_qr_image_url expects.
        qr = extract_qr_image_url({"message": {"order": order_msg}})
        if qr is not None:
            cart.qr_image_url = qr
        # Guard: never re-flip a cancelled / expired payment_state back
        # to paid. Today CartStatus has no CANCELLED member, but the
        # payment_state column is a free string written elsewhere (e.g.
        # status endpoint flipping pending → expired).
        if cart.payment_state not in ("cancelled", "expired"):
            if qr is not None:
                cart.payment_state = "paid"
        cart.updated_at = datetime.now(timezone.utc)

    return None


async def handle_on_update(
    context: dict[str, Any],
    message: dict[str, Any],
    db: AsyncSession,
) -> dict | None:
    """Track refund lifecycle updates from the BPP.

    Tags we listen for:
      - refund_pending  → record bpp_refund_request_id on the Dispute
      - refund_approved → mark Dispute as brand_responding (Xendit pending)
      - refund_denied   → mark Dispute as resolved (denied)
      - refund_settled  → mark Dispute resolved + flip Order to REFUNDED
    """
    from models.dispute import Dispute, DisputeStatus
    from models.order import OrderState

    order_msg = message.get("order") or {}
    bpp_order_id = order_msg.get("id")
    if not bpp_order_id:
        return None

    # Find the local order
    order = (await db.execute(
        select(Order).where(Order.seller_order_ref == bpp_order_id)
    )).scalar_one_or_none()
    if order is None and len(bpp_order_id) == 36:
        order = (await db.execute(
            select(Order).where(Order.id == bpp_order_id)
        )).scalar_one_or_none()
    if order is None:
        return None

    dispute = (await db.execute(
        select(Dispute).where(Dispute.order_id == order.id).order_by(Dispute.created_at.desc())
    )).scalars().first()

    for tag in order_msg.get("tags") or []:
        code = tag.get("code")
        kv = {x.get("code"): x.get("value") for x in tag.get("list") or []}
        if code == "refund_pending" and dispute is not None:
            dispute.bpp_refund_request_id = kv.get("refund_request_id")
        elif code == "refund_approved" and dispute is not None:
            dispute.bpp_refund_request_id = kv.get("refund_request_id") or dispute.bpp_refund_request_id
            dispute.status = DisputeStatus.BRAND_RESPONDING
        elif code == "refund_denied" and dispute is not None:
            dispute.status = DisputeStatus.RESOLVED
            dispute.resolution = "denied"
            dispute.note = kv.get("seller_note") or dispute.note
        elif code == "refund_settled":
            if dispute is not None:
                dispute.status = DisputeStatus.RESOLVED
                dispute.resolution = "refunded"
            if order.state != OrderState.REFUNDED:
                order.state = OrderState.REFUNDED
    return None


async def handle_on_settle(
    context: dict[str, Any],
    message: dict[str, Any],
    db: AsyncSession,
) -> dict | None:
    """Receive a BPP's /on_settle response and reconcile the local Order.

    Task A6 (ONDC RSP v1, settlement-record scope). The BPP echoes the
    settlement record (id, status, basis, window, reference, counterparties)
    that ``services.settlement.request_settlement`` triggered on /settle.
    We persist ``settlement_status`` + ``settlement_reference`` onto the
    Order so the storefront / admin can see the BPP's published payable.

    v1 doesn't move money — the persisted state is observability only.
    """
    settlement = message.get("settlement") or {}
    bpp_order_id = settlement.get("order_id")
    if not bpp_order_id:
        return None

    # Find the local order (same patterns as handle_on_status).
    candidate = (await db.execute(
        select(Order).where(Order.seller_order_ref == bpp_order_id)
    )).scalar_one_or_none()
    if candidate is None and len(bpp_order_id) == 36:
        candidate = (await db.execute(
            select(Order).where(Order.id == bpp_order_id)
        )).scalar_one_or_none()
    if candidate is None:
        logger.info(
            "on_settle for unknown order %s — ignoring", bpp_order_id
        )
        return None

    status = settlement.get("settlement_status")
    if status:
        candidate.settlement_status = status
    basis = settlement.get("settlement_basis")
    if basis:
        candidate.settlement_basis = basis
    window_obj = settlement.get("settlement_window") or {}
    if isinstance(window_obj, dict):
        duration = window_obj.get("duration")
        if duration:
            candidate.settlement_window = duration
    elif isinstance(window_obj, str):
        candidate.settlement_window = window_obj
    reference = settlement.get("settlement_reference")
    if reference:
        candidate.settlement_reference = reference

    return None


async def handle_on_rating(
    context: dict[str, Any],
    message: dict[str, Any],
    db: AsyncSession,
) -> dict | None:
    """Receive a BPP's /on_rating ack and mark the OrderRating row.

    Task A6 (ONDC /rating, narrow). The BPP echoes a ``feedback_ack``
    boolean back; v1 just flips ``OrderRating.acknowledged`` and stamps
    ``acknowledged_at`` so the storefront can render a confirmation.

    Locating the OrderRating uses ``context.transaction_id`` (which the
    BAP's /rating envelope set to ``str(order.id)``) — that's the most
    reliable correlation key.
    """
    from models.order_rating import OrderRating

    transaction_id = context.get("transaction_id") or ""
    if not transaction_id:
        return None

    # OrderRating.order_id is a VARCHAR(36) FK to orders.id — the
    # transaction_id we sent IS that order id.
    rating_row = (await db.execute(
        select(OrderRating).where(OrderRating.order_id == transaction_id)
    )).scalar_one_or_none()
    if rating_row is None:
        logger.info(
            "on_rating for unknown transaction_id %s — ignoring",
            transaction_id,
        )
        return None

    feedback_ack = message.get("feedback_ack")
    # Truthy ack flips acknowledged; explicit False does too (BPP closed
    # the loop, even if it declined to act on the rating).
    if feedback_ack is not None:
        rating_row.acknowledged = bool(feedback_ack)
        rating_row.acknowledged_at = datetime.now(timezone.utc)

    return None


async def handle_on_issue(
    context: dict[str, Any],
    message: dict[str, Any],
    db: AsyncSession,
) -> dict | None:
    """Receive a BPP's /on_issue response and reconcile the local Dispute.

    Task A5 (ONDC IGM v1, refund-request scope). The BPP echoes the same
    ``issue.id`` we sent at /issue time; we use that to locate the
    Dispute row created in :func:`services.igm.open_issue`. Idempotency
    is handled upstream by the BecknInboundLog dedupe on ``message_id``,
    so this handler is free to re-apply the same update on retries.

    The action is read from ``message.issue.issue_actions.respondent_actions[-1]``
    (the most-recent BPP action). v1 mapping:

    * PROCESSING -> Dispute.status = BRAND_RESPONDING
    * RESOLVED   -> Dispute.status = RESOLVED, resolution = "resolved",
                    Order.state -> REFUNDED if the resolution carries a
                    refund_amount.
    * REJECTED   -> Dispute.status = RESOLVED, resolution = "denied".
    * ESCALATE   -> currently a no-op (v1 doesn't model multi-party).
    """
    from models.dispute import Dispute, DisputeStatus
    from models.order import OrderState

    issue = message.get("issue") or {}
    issue_id = issue.get("id")
    if not issue_id:
        return None

    dispute = (await db.execute(
        select(Dispute).where(Dispute.bpp_issue_id == issue_id)
    )).scalar_one_or_none()
    if dispute is None:
        logger.info(
            "on_issue for unknown issue_id %s — ignoring", issue_id
        )
        return None

    actions = (issue.get("issue_actions") or {}).get(
        "respondent_actions"
    ) or []
    if not actions:
        # Fall back to top-level issue.status if the BPP didn't include
        # the action timeline.
        last_action_code = (issue.get("status") or "").upper()
        action_short = ""
        action_long = ""
    else:
        last_action = actions[-1]
        last_action_code = (
            last_action.get("respondent_action") or ""
        ).upper()
        action_short = last_action.get("short_desc") or ""
        action_long = last_action.get("long_desc") or ""

    resolution = issue.get("resolution") or {}
    resolution_short = resolution.get("short_desc") or action_short
    resolution_long = resolution.get("long_desc") or action_long

    now_iso = datetime.now(timezone.utc).isoformat()

    if last_action_code == "PROCESSING":
        dispute.status = DisputeStatus.BRAND_RESPONDING
        if resolution_short or resolution_long:
            dispute.bpp_resolution_note = (
                resolution_long or resolution_short or None
            )
    elif last_action_code == "RESOLVED":
        dispute.status = DisputeStatus.RESOLVED
        dispute.resolution = "resolved"
        dispute.resolved_at = now_iso
        dispute.bpp_resolution_note = (
            resolution_long or resolution_short or None
        )
        # Optional refund payload — flip the Order to REFUNDED if the
        # BPP says they refunded.
        refund_amt = (resolution.get("refund_amount") or {}).get("value")
        if refund_amt:
            order = await db.get(Order, dispute.order_id)
            if order is not None and order.state != OrderState.REFUNDED:
                order.state = OrderState.REFUNDED
    elif last_action_code == "REJECTED":
        dispute.status = DisputeStatus.RESOLVED
        dispute.resolution = "denied"
        dispute.resolved_at = now_iso
        dispute.bpp_resolution_note = (
            resolution_long or resolution_short or None
        )
    # ESCALATE: explicit no-op in v1 (multi-party IGM is deferred).

    return None
