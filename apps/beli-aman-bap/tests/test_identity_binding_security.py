"""Identity linking and invitations require current, verified contact proof."""
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from deps import _get_or_create_profile, _get_or_create_profile_by_contact
from models.profile import BeliAmanProfile
from models.store_membership import StoreMembership
from test_otp_login import db_factory


async def sign_in(db, subject="subject-1", email="member@example.com", verified=True):
    return await _get_or_create_profile(
        db, google_sub=subject, email=email, email_verified=verified,
        display_name="Member", photo_url=None,
    )


@pytest.mark.asyncio
async def test_verified_email_cannot_resolve_another_bound_subject(db_factory):
    engine, Session = await db_factory()
    try:
        async with Session() as db:
            original = BeliAmanProfile(
                google_sub="original-subject", email="member@example.com", is_super_admin=True,
            )
            db.add(original)
            await db.commit()
            with pytest.raises(HTTPException) as denied:
                await sign_in(db)
            assert denied.value.status_code == 409
            assert original.google_sub == "original-subject"
            assert original.display_name is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,claimed", [
    ("verified", True), ("unverified", False), ("email-otp", True), ("wa-otp", False),
])
async def test_invites_require_current_email_proof(db_factory, method, claimed):
    engine, Session = await db_factory()
    try:
        async with Session() as db:
            profile = BeliAmanProfile(
                google_sub="subject-1", email="member@example.com", phone_e164="+6281234567890",
            )
            invite = StoreMembership(invited_email="member@example.com", store_id="store-1")
            db.add_all([profile, invite])
            await db.commit()
            if method in {"verified", "unverified"}:
                resolved = await sign_in(db, verified=method == "verified")
            else:
                channel = "email" if method == "email-otp" else "wa"
                resolved = await _get_or_create_profile_by_contact(
                    db, channel=channel,
                    contact="member@example.com" if channel == "email" else "+6281234567890",
                )
            assert resolved.id == profile.id
            assert (invite.profile_id == profile.id) is claimed
            assert (invite.accepted_at is not None) is claimed
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_verified_changed_email_does_not_claim_old_address(db_factory):
    engine, Session = await db_factory()
    try:
        async with Session() as db:
            profile = BeliAmanProfile(google_sub="subject-1", email="old@example.com")
            old = StoreMembership(invited_email="old@example.com", store_id="old-store")
            current = StoreMembership(invited_email="member@example.com", store_id="new-store")
            db.add_all([profile, old, current])
            await db.commit()
            resolved = await sign_in(db)
            assert resolved.id == profile.id
            assert resolved.email == "member@example.com"
            assert old.profile_id is None
            assert current.profile_id == profile.id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_identity_and_invitation_changes_rollback_with_caller(db_factory):
    engine, Session = await db_factory()
    try:
        async with Session() as db:
            profile = BeliAmanProfile(email="member@example.com")
            invite = StoreMembership(invited_email="member@example.com", store_id="store-1")
            db.add_all([profile, invite])
            await db.commit()
            profile_id = profile.id
            await sign_in(db)
            await db.flush()
            assert invite.profile_id == profile_id
            await db.rollback()
        async with Session() as db:
            stored = await db.get(BeliAmanProfile, profile_id)
            invite = (await db.execute(select(StoreMembership))).scalar_one()
            assert stored.google_sub is None
            assert stored.display_name is None
            assert invite.profile_id is None
            assert invite.accepted_at is None
    finally:
        await engine.dispose()
