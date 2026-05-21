"""Send an OTP via WhatsApp through the jd-wa-whatsmeow sidecar.

The sidecar (``apps/channels/whatsapp`` in the private jaringan-dagang
monorepo) is a multi-tenant whatsmeow process. We piggyback a dedicated
"system-otp" inbox: provisioned once via QR scan, then this code POSTs
the OTP text to ``/sessions/system-otp/send``.

Failures are NEVER surfaced to the SDK caller — see ``routers/auth.py``
where this is wrapped: we log the real reason server-side and return a
generic 200 so we don't leak enumeration signal.

When ``settings.wa_sidecar_url`` is empty (dev default), the OTP is
printed to stdout instead — same shape as the email-sender stub.
"""

from __future__ import annotations

import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)


def _format_message(code: str) -> str:
    return (
        f"Kode masuk Beli Aman Anda: *{code}*\n"
        f"Berlaku 7 menit. Jangan bagikan kode ini kepada siapa pun."
    )


async def send_otp(phone_e164: str, code: str) -> bool:
    """Send an OTP code via the WA sidecar. Returns True on 2xx, False otherwise."""
    if not settings.wa_sidecar_url or not settings.wa_sidecar_shared_secret:
        # Dev fallback — log the code so engineers can complete the flow.
        logger.warning("[wa_sender] DEV MODE — would send to %s: %s", phone_e164, code)
        return True

    inbox = settings.wa_sidecar_otp_inbox_id
    url = f"{settings.wa_sidecar_url.rstrip('/')}/sessions/{inbox}/send"
    # The sidecar expects the international number without leading "+".
    to = phone_e164.lstrip("+")
    payload = {"to": to, "kind": "text", "text": _format_message(code)}
    headers = {
        "Authorization": f"Bearer {settings.wa_sidecar_shared_secret}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, json=payload, headers=headers)
        if r.status_code >= 300:
            logger.error(
                "[wa_sender] sidecar %s returned %s: %s",
                url, r.status_code, r.text[:300],
            )
            return False
        return True
    except Exception:
        logger.exception("[wa_sender] sidecar request failed")
        return False
