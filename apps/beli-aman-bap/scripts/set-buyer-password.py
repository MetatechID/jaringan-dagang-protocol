"""Create a buyer account with an email + password, or reset its password.

Buyers sign in at checkout with these; there is no self-service sign-up or
reset. The password is read from the terminal (never from argv, which lands in
shell history and ``ps``), or from ``NEW_PASSWORD`` for non-interactive use.

    DATABASE_URL=postgresql+asyncpg://... \\
        python apps/beli-aman-bap/scripts/set-buyer-password.py buyer@example.com --name "Demo Buyer"
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def run(email: str, password: str, name: str | None) -> str:
    from sqlalchemy import select

    from auth.passwords import hash_password
    from database import async_session
    from models.profile import BeliAmanProfile

    async with async_session() as db:
        profile = (
            await db.execute(select(BeliAmanProfile).where(BeliAmanProfile.email == email))
        ).scalar_one_or_none()
        if profile is None:
            profile = BeliAmanProfile(email=email, display_name=name, is_super_admin=False)
            db.add(profile)
            action = "created"
        else:
            action = "password reset"
            if name:
                profile.display_name = name
        profile.password_hash = hash_password(password)
        await db.commit()
    return action


def main() -> int:
    from auth.passwords import MIN_PASSWORD_LENGTH

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("email")
    parser.add_argument("--name", help="display name for a new account")
    args = parser.parse_args()
    email = args.email.strip().lower()
    if "@" not in email:
        print("not an email address", file=sys.stderr)
        return 2
    password = os.environ.get("NEW_PASSWORD") or getpass.getpass("New password: ")
    if not os.environ.get("NEW_PASSWORD") and getpass.getpass("Repeat: ") != password:
        print("passwords do not match", file=sys.stderr)
        return 2
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"use at least {MIN_PASSWORD_LENGTH} characters", file=sys.stderr)
        return 2
    print(f"{email}: {asyncio.run(run(email, password, args.name))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
