"""Verified callback identity and persisted resource correlation.

Only the signature-verifying router creates this context. JSON fields cannot
assert a verified identity. Catalog delegation and subscriber rotation are not
inferred from provider IDs, display names, or slug collisions.
"""
from fastapi import HTTPException
from sqlalchemy import select

from models.order import Order


class VerifiedContext(dict):
    def __init__(self, context: dict, signer: str):
        super().__init__(context)
        self.signer = signer
        self.resources = None


def verified_signer(context) -> str:
    if not isinstance(context, VerifiedContext) or context.get("bpp_id") != context.signer:
        raise HTTPException(403, "Verified BPP identity required")
    return context.signer


async def owned_order(db, context, order_id=None):
    signer = verified_signer(context)
    txn = context.get("transaction_id")
    order = await db.get(Order, order_id or txn)
    if order is None:
        raise HTTPException(404, "Callback order not found")
    if order.bpp_id != signer or str(order.id) != txn:
        raise HTTPException(403, "Callback order ownership or transaction mismatch")
    return order


def match_order_reference(order, reference):
    if not reference or reference not in {str(order.id), order.seller_order_ref}:
        raise HTTPException(403, "Callback order reference mismatch")


async def authorize_resources(action, context, message, db):
    signer = verified_signer(context)
    if context.resources is not None:
        return context.resources
    resources = {}
    if action == "on_search":
        from models.mirror import MirrorStore
        catalog = message.get("catalog") or {}
        # Validate both aliases, even if only one is consumed by the handler.
        for providers in (catalog.get("bpp/providers") or [], catalog.get("providers") or []):
            if any(p.get("id") != signer for p in providers):
                raise HTTPException(403, "Catalog provider does not belong to signer")
        providers = catalog.get("bpp/providers") or catalog.get("providers") or []
        if len(providers) > 1:
            raise HTTPException(400, "Duplicate catalog provider")
        if providers:
            from routers.beckn_handlers import _slug_from_bpp_id
            provider = providers[0]
            slug = _slug_from_bpp_id(signer, (provider.get("descriptor") or {}).get("name"))
            collision = (await db.execute(select(MirrorStore).where(MirrorStore.slug == slug))).scalar_one_or_none()
            if collision is not None and collision.bpp_id != signer:
                raise HTTPException(403, "Catalog slug belongs to another subscriber")
    elif action in {"on_issue", "on_issue_status"}:
        from models.dispute import Dispute
        issue = message.get("issue") or {}
        issue_id = issue.get("id")
        if not issue_id:
            raise HTTPException(400, "Missing issue reference")
        dispute = (await db.execute(select(Dispute).where(Dispute.bpp_issue_id == issue_id))).scalar_one_or_none()
        if dispute is None:
            raise HTTPException(404, "Callback issue not found")
        order = await owned_order(db, context, dispute.order_id)
        issue_order = (issue.get("order_details") or {}).get("id")
        if issue_order:
            match_order_reference(order, issue_order)
        resources.update(order=order, dispute=dispute)
    elif action == "on_rating":
        resources["order"] = await owned_order(db, context)
    elif action in {"on_confirm", "on_status", "on_track", "on_cancel"}:
        from models.bot_rest import Cart
        txn = context.get("transaction_id")
        order = await db.get(Order, txn)
        if order is not None:
            order = await owned_order(db, context)
            match_order_reference(order, (message.get("order") or {}).get("id"))
            resources["order"] = order
        else:
            cart = (await db.execute(select(Cart).where(Cart.transaction_id == txn))).scalar_one_or_none()
            if cart is None:
                raise HTTPException(404, "Callback cart not found")
            if (cart.transaction_id != txn or cart.bpp_id != signer
                    or not cart.order_id or cart.order_id != (message.get("order") or {}).get("id")):
                raise HTTPException(403, "Callback cart ownership or order mismatch")
            resources["cart"] = cart
    elif action in {"on_update", "on_settle"}:
        order = await owned_order(db, context)
        reference = ((message.get("settlement") or {}).get("order_id") if action == "on_settle"
                     else (message.get("order") or {}).get("id"))
        match_order_reference(order, reference)
        resources["order"] = order
    context.resources = resources
    return resources
