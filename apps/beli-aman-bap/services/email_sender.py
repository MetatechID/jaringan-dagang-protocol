"""Send an OTP via SMTP — same shape as karya1's ``apps/app/server/utils/mailer.ts``.

Configured via SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / SMTP_FROM /
SMTP_SECURE env vars (see ``config.Settings``). When SMTP_HOST is empty the
sender logs the code to stdout — keeps dev/staging usable without creds.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from config import settings

logger = logging.getLogger(__name__)


def _build_message(to_email: str, code: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "Kode masuk Beli Aman"
    msg["From"] = settings.smtp_from or "noreply@beliaman.com"
    msg["To"] = to_email
    msg.set_content(
        f"Kode masuk Beli Aman Anda: {code}\n\n"
        f"Berlaku 7 menit. Jangan bagikan kode ini kepada siapa pun."
    )
    msg.add_alternative(
        f"""<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:420px;margin:0 auto;padding:32px">
  <h2 style="color:#6B2C1A;margin:0 0 8px">Kode masuk Anda</h2>
  <p style="color:#555;margin:0 0 24px">Masukkan kode ini untuk masuk ke Beli Aman:</p>
  <div style="background:#FBF6EC;border:1px solid #e5e0d4;border-radius:10px;padding:20px;text-align:center;margin-bottom:24px">
    <span style="font-size:32px;font-weight:700;letter-spacing:8px;color:#2A1810">{code}</span>
  </div>
  <p style="color:#999;font-size:13px">Berlaku 7 menit. Jangan bagikan kode ini.</p>
</div>""",
        subtype="html",
    )
    return msg


def send_otp_sync(to_email: str, code: str) -> bool:
    """Blocking SMTP send. Returns True on success, False on any failure."""
    if not settings.smtp_host:
        logger.warning("[email_sender] DEV MODE — would email %s: %s", to_email, code)
        return True

    msg = _build_message(to_email, code)
    try:
        if settings.smtp_secure:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=ctx, timeout=15) as server:
                if settings.smtp_user:
                    server.login(settings.smtp_user, settings.smtp_pass)
                server.send_message(msg)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
                server.ehlo()
                try:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                except Exception:
                    # Plain-text SMTP for local dev; karya1 default is starttls
                    # on 587 so this path mostly catches MailHog / Mailpit.
                    pass
                if settings.smtp_user:
                    server.login(settings.smtp_user, settings.smtp_pass)
                server.send_message(msg)
        return True
    except Exception:
        logger.exception("[email_sender] SMTP send failed for %s", to_email)
        return False


async def send_otp(to_email: str, code: str) -> bool:
    """Async wrapper — runs the blocking smtplib call in a worker thread."""
    import asyncio
    return await asyncio.to_thread(send_otp_sync, to_email, code)
