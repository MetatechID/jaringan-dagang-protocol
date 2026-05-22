"""Idempotent super-admin bootstrap for the Beli Aman identity DB.

Reads ``SUPER_ADMIN_BOOTSTRAP_EMAILS`` (comma-separated, lower-cased) and
flips ``profiles.is_super_admin`` to ``True`` for each matching profile.

Beli Aman is the network-wide IdP — every seller-dashboard /api/v1/me
request resolves against this DB, so this is the authoritative place to
mint a super admin.

Profiles that don't exist yet are NOT created; super admin status is only
granted to profiles that have signed in at least once (so we never bake
ghost rows from a typo). Run the seed AFTER the user signs in once. If
you really need to pre-create, use ``--create-missing``.

Usage::

    SUPER_ADMIN_BOOTSTRAP_EMAILS=hallucinogenplus@gmail.com,lwastuargo@gmail.com \\
        python -m scripts.seed_rbac

    # Or to also create profile shells for emails not yet seen:
    SUPER_ADMIN_BOOTSTRAP_EMAILS=... python -m scripts.seed_rbac --create-missing
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import select

from database import async_session
from models.profile import BeliAmanProfile


DEFAULT_EMAILS = "hallucinogenplus@gmail.com,lwastuargo@gmail.com"


def _parse_emails(raw: str | None) -> list[str]:
    return [e.strip().lower() for e in (raw or "").split(",") if e.strip()]


async def run(create_missing: bool) -> int:
    raw = os.environ.get("SUPER_ADMIN_BOOTSTRAP_EMAILS", DEFAULT_EMAILS)
    emails = _parse_emails(raw)
    if not emails:
        print("No emails provided (set SUPER_ADMIN_BOOTSTRAP_EMAILS).", file=sys.stderr)
        return 2

    created = 0
    promoted = 0
    untouched = 0
    skipped: list[str] = []

    async with async_session() as db:
        for email in emails:
            profile = (
                await db.execute(
                    select(BeliAmanProfile).where(BeliAmanProfile.email == email)
                )
            ).scalar_one_or_none()

            if profile is None:
                if create_missing:
                    profile = BeliAmanProfile(email=email, is_super_admin=True)
                    db.add(profile)
                    created += 1
                else:
                    skipped.append(email)
                    continue
            elif not profile.is_super_admin:
                profile.is_super_admin = True
                promoted += 1
            else:
                untouched += 1

        await db.commit()

    print(
        f"super-admin seed complete: created={created} promoted={promoted} "
        f"untouched={untouched} skipped={len(skipped)}"
    )
    if skipped:
        print(f"skipped (profile not found — sign in first or pass --create-missing):")
        for e in skipped:
            print(f"  - {e}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--create-missing",
        action="store_true",
        help="Create a profile shell for emails that haven't signed in yet.",
    )
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args.create_missing)))


if __name__ == "__main__":
    main()
