"""Profile management — addresses, payment methods."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_profile
from models.address import Address
from models.payment_method import PaymentMethod, PaymentMethodType
from models.profile import BeliAmanProfile

router = APIRouter(prefix="/api/v1/me", tags=["profiles"])


@router.get("")
async def get_me(profile: BeliAmanProfile = Depends(get_current_profile)) -> dict:
    return {
        "id": profile.id,
        "email": profile.email,
        "display_name": profile.display_name,
        "photo_url": profile.photo_url,
        "phone_e164": profile.phone_e164,
        "is_super_admin": profile.is_super_admin,
    }


# ---------- Addresses ----------


class AddressIn(BaseModel):
    recipient_name: str
    phone_e164: str
    line1: str
    line2: str | None = None
    kelurahan: str | None = None
    kecamatan: str | None = None
    kota: str
    provinsi: str
    postal_code: str
    is_default: bool = False


def _serialize_address(a: Address) -> dict:
    return {
        "id": a.id,
        "recipient_name": a.recipient_name,
        "phone_e164": a.phone_e164,
        "line1": a.line1,
        "line2": a.line2,
        "kelurahan": a.kelurahan,
        "kecamatan": a.kecamatan,
        "kota": a.kota,
        "provinsi": a.provinsi,
        "postal_code": a.postal_code,
        "is_default": a.is_default,
    }


@router.get("/addresses")
async def list_addresses(
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    result = await db.execute(select(Address).where(Address.profile_id == profile.id))
    return [_serialize_address(a) for a in result.scalars().all()]


@router.post("/addresses")
async def create_address(
    body: AddressIn,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if body.is_default:
        # Demote prior defaults
        result = await db.execute(
            select(Address).where(Address.profile_id == profile.id, Address.is_default == True)
        )
        for a in result.scalars().all():
            a.is_default = False

    addr = Address(profile_id=profile.id, **body.model_dump())
    db.add(addr)
    await db.flush()
    return _serialize_address(addr)


# ---------- Payment methods ----------


class PaymentMethodIn(BaseModel):
    type: PaymentMethodType
    display_label: str
    is_default: bool = False


def _serialize_pm(pm: PaymentMethod) -> dict:
    return {
        "id": pm.id,
        "type": pm.type.value,
        "display_label": pm.display_label,
        "is_default": pm.is_default,
    }


@router.get("/payment-methods")
async def list_payment_methods(
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    result = await db.execute(
        select(PaymentMethod).where(PaymentMethod.profile_id == profile.id)
    )
    pms = list(result.scalars().all())
    if not pms:
        # Auto-seed the v1 default mock PM so the demo flow always has something.
        seed = PaymentMethod(
            profile_id=profile.id,
            type=PaymentMethodType.VIRTUAL_ACCOUNT,
            display_label="BCA Virtual Account — Demo",
            is_default=True,
        )
        db.add(seed)
        await db.flush()
        pms = [seed]
    return [_serialize_pm(pm) for pm in pms]


@router.post("/payment-methods")
async def create_payment_method(
    body: PaymentMethodIn,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict:
    pm = PaymentMethod(profile_id=profile.id, **body.model_dump())
    db.add(pm)
    await db.flush()
    return _serialize_pm(pm)
