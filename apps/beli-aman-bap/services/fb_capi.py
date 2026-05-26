"""Meta Conversions API — server-side Purchase event sender.

Why we need this on top of the browser Pixel:
  - iOS Safari ITP strips third-party cookies → Pixel can't match the user.
  - Ad blockers prevent ``fbevents.js`` from ever loading.
  - Users close the tab before the post-payment page renders the Pixel
    Purchase event.

By POSTing the same Purchase event server-side with hashed user data and
the same ``event_id`` the browser used, Meta dedupes the two and we
recover ~20–40% of lost attribution. ROAS in Ads Manager becomes accurate
instead of optimistically-low.

Reference: https://developers.facebook.com/docs/marketing-api/conversions-api
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Optional

import httpx

from models.order import Order
from models.profile import BeliAmanProfile
from models.storefront_integration import StorefrontIntegration


_LOG = logging.getLogger("beli_aman_bap.fb_capi")

# Pinned API version. Bump when Meta deprecates this one (they keep ~2 years
# of compatibility per version). Last reviewed 2026-05-26.
_GRAPH_VERSION = "v19.0"

# We POST best-effort with a tight timeout — if FB is slow, the buyer's
# payment confirmation must not block.
_TIMEOUT_SECONDS = 5.0


def _sha256(s: str | None) -> str | None:
    """Meta requires lowercased, trimmed, SHA-256 of email/phone."""
    if not s:
        return None
    norm = s.strip().lower()
    if not norm:
        return None
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _normalize_phone(phone_e164: str | None) -> str | None:
    """Strip non-digits per Meta's matching rules."""
    if not phone_e164:
        return None
    digits = "".join(c for c in phone_e164 if c.isdigit())
    return digits or None


def _build_user_data(
    profile: BeliAmanProfile,
    attribution: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble Meta's user_data block.

    Hash all PII; pass fbc/fbp/UA/IP raw (Meta doesn't want those hashed).
    Every present field independently improves match-quality. Even one
    high-quality signal (em or fbc) is usually enough to attribute back to
    an ad click.
    """
    user_data: dict[str, Any] = {}
    if profile.email:
        em = _sha256(profile.email)
        if em:
            user_data["em"] = [em]
    if profile.phone_e164:
        ph = _sha256(_normalize_phone(profile.phone_e164))
        if ph:
            user_data["ph"] = [ph]
    # external_id helps match across devices for known users.
    if profile.id:
        user_data["external_id"] = [_sha256(profile.id)]

    if attribution:
        fbc = attribution.get("fbc")
        fbp = attribution.get("fbp")
        ua = attribution.get("user_agent")
        ip = attribution.get("ip")
        if fbc:
            user_data["fbc"] = fbc
        if fbp:
            user_data["fbp"] = fbp
        if ua:
            user_data["client_user_agent"] = ua
        if ip:
            user_data["client_ip_address"] = ip
        # Click-to-WhatsApp click id — Meta uses this to attribute a
        # purchase back to a WA ad. Only present when the order came from
        # a WA conversation that started via a Click-to-WA ad.
        ctwa = attribution.get("ctwa_clid")
        if ctwa:
            user_data["ctwa_clid"] = ctwa

    return user_data


def _build_custom_data(order: Order) -> dict[str, Any]:
    """Meta's custom_data block — value/currency/contents drive ROAS math."""
    contents = [
        {
            "id": item.get("sku"),
            "quantity": int(item.get("qty") or 1),
            "item_price": float(item.get("unit_price_idr") or 0),
        }
        for item in (order.items or [])
        if item.get("sku")
    ]
    content_ids = [c["id"] for c in contents if c.get("id")]
    return {
        "currency": "IDR",
        "value": float(order.total_idr or 0),
        "content_type": "product",
        "content_ids": content_ids,
        "contents": contents,
        "num_items": sum(c["quantity"] for c in contents) or len(contents),
        "order_id": order.id,
    }


async def send_purchase(
    *,
    order: Order,
    integration: StorefrontIntegration,
    profile: BeliAmanProfile,
) -> Optional[dict[str, Any]]:
    """Fire one Purchase event to Meta's Conversions API.

    Returns the Meta response on success (useful for logs), ``None`` if we
    bailed because the integration isn't fully configured, or raises in
    pathological cases (network error). Callers should always wrap in
    try/except so a Meta outage never breaks the payment-success path.
    """
    if not integration.fb_pixel_id or not integration.fb_capi_access_token:
        return None

    event_id = order.id  # MUST match the eventID the browser Pixel uses.
    event_time = int(time.time())

    payload: dict[str, Any] = {
        "data": [
            {
                "event_name": "Purchase",
                "event_time": event_time,
                "event_id": event_id,
                "action_source": "website",
                "event_source_url": (order.attribution or {}).get("landing_url"),
                "user_data": _build_user_data(profile, order.attribution),
                "custom_data": _build_custom_data(order),
            }
        ],
    }
    # Drop event_source_url if it was None so Meta doesn't complain about null.
    if not payload["data"][0]["event_source_url"]:
        payload["data"][0].pop("event_source_url", None)

    if integration.fb_capi_test_event_code:
        payload["test_event_code"] = integration.fb_capi_test_event_code

    url = (
        f"https://graph.facebook.com/{_GRAPH_VERSION}"
        f"/{integration.fb_pixel_id}/events"
    )
    params = {"access_token": integration.fb_capi_access_token}

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.post(url, params=params, json=payload)
    if resp.status_code // 100 != 2:
        _LOG.warning(
            "FB CAPI Purchase failed: %s %s — order=%s pixel=%s",
            resp.status_code, resp.text[:300], order.id, integration.fb_pixel_id,
        )
        return None

    data = resp.json()
    _LOG.info(
        "FB CAPI Purchase OK: order=%s events_received=%s fbtrace_id=%s",
        order.id, data.get("events_received"), data.get("fbtrace_id"),
    )
    return data
