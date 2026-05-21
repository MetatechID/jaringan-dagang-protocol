"""Auto-release clock pinned to Asia/Jakarta (WIB, UTC+7).

The customer-journey spec is explicit about this: D+3 is calendar days in
Jakarta time, not 72 wall-clock hours. A "delivered" event at 23:30 WIB on
Monday should release 00:00 WIB Friday — not 23:30 WIB Thursday.

The standard approach: take "delivered_at" → convert to Jakarta-local-day →
add N days → that day's end-of-day (or beginning-of-day per business choice).
We use end-of-day so the buyer always has the full final calendar day to
confirm receipt or open a dispute.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

# Asia/Jakarta is UTC+7, no DST.
JAKARTA = timezone(timedelta(hours=7), name="Asia/Jakarta")


def compute_auto_release_at(delivered_at: datetime, days: int) -> datetime:
    """Return the UTC datetime when funds auto-release.

    The release fires at the end of the Nth calendar day after delivered_at,
    measured in Asia/Jakarta time. Stored back in UTC.
    """
    # Move to Jakarta-local time for the date math
    if delivered_at.tzinfo is None:
        delivered_at = delivered_at.replace(tzinfo=timezone.utc)
    local = delivered_at.astimezone(JAKARTA)

    # End of (delivered local-date + N days) = midnight at the start of day N+1
    target_date = local.date() + timedelta(days=days)
    # 23:59:59 on the target Jakarta day
    release_local = datetime.combine(target_date, time(23, 59, 59), tzinfo=JAKARTA)

    # Store in UTC
    return release_local.astimezone(timezone.utc)
