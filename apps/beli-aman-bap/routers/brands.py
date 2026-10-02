"""Brand catalog + per-brand payouts/fulfillment admin endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import get_db
from deps import get_current_profile
from models.brand import Brand
from models.payout_bank_account import PayoutBankAccount
from models.profile import BeliAmanProfile
from models.store_membership import StoreMembership
from services import bank_codes
from services import catalog as catalog_service

router = APIRouter(prefix="/api/v1/brands", tags=["brands"])


@router.get("")
async def list_brands(db: AsyncSession = Depends(get_db)) -> list[dict]:
    result = await db.execute(select(Brand).order_by(Brand.slug))
    return [
        {
            "id": b.id,
            "slug": b.slug,
            "name": b.name,
            "bpp_id": b.bpp_id,
        }
        for b in result.scalars().all()
    ]


@router.get("/{slug}")
async def get_brand(slug: str, db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(select(Brand).where(Brand.slug == slug))
    brand = result.scalar_one_or_none()
    if not brand:
        raise HTTPException(404, f"Brand '{slug}' not found")
    return {
        "id": brand.id,
        "slug": brand.slug,
        "name": brand.name,
        "bpp_id": brand.bpp_id,
        "default_warehouse_address": brand.default_warehouse_address,
    }


@router.get("/{slug}/products")
async def list_products(slug: str) -> list[dict]:
    try:
        return await catalog_service.list_products(slug)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@router.get("/{slug}/products/{product_slug}")
async def get_product(slug: str, product_slug: str) -> dict:
    try:
        product = await catalog_service.get_product(slug, product_slug)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    if not product:
        raise HTTPException(404, f"Product '{product_slug}' not found in brand '{slug}'")
    return product


# ----- Payouts & Fulfillment (vibe-admin) -----


ALLOWED_PAYMENT_PROVIDERS = {"xendit", "sento", "dipay", "oy"}


class BankAccountIn(BaseModel):
    """One entry in ``PayoutsIn.bank_accounts``.

    - With ``id``: edit that row. ``account_number`` None/omitted keeps the
      stored number (it's write-only — GET returns only the masked form);
      explicit empty string clears... nothing: the field is NOT NULL, so an
      empty string is normalized to None = keep. Use delete to remove.
    - Without ``id``: create a new row (``account_number`` required).
    - ``bank_code`` is the CANONICAL BI numeric code ("014" = BCA) —
      translated per payment gateway at disbursement time
      (services/bank_codes.py).
    """

    id: str | None = None
    bank_code: str | None = None
    account_number: str | None = None
    holder_name: str | None = None


class PayoutsOut(BaseModel):
    slug: str
    bank_accounts: list[dict]
    # Shared bank picker for the UI: canonical codes + which gateways
    # accept each (services/bank_codes.py).
    bank_keys: list[dict]
    xendit_sub_account_id: str | None = None
    dipay_client_key_masked: str | None = None
    dipay_client_key_configured: bool
    dipay_client_secret_configured: bool
    dipay_private_key_configured: bool
    dipay_merchant_id: str | None = None
    biteship_origin_address: dict | None = None
    biteship_default_courier: str | None = None
    payment_provider: str
    courier_provider: str
    jubelio_origin_address: dict | None = None


class PayoutsIn(BaseModel):
    # Bank accounts: create (no id) / edit (with id). The active row is
    # chosen via ``active_bank_account_id``; rows to remove go in
    # ``delete_bank_account_ids``.
    bank_accounts: list[BankAccountIn] | None = None
    delete_bank_account_ids: list[str] | None = None
    active_bank_account_id: str | None = None
    xendit_sub_account_id: str | None = None
    # Dipay (SNAP v2.1) — client key/secret + RSA private key (base64 PEM)
    # for request signing, merchant id for QRIS. Credentials stay on Brand;
    # only the bank account moved to payout_bank_accounts.
    dipay_client_key: str | None = None
    dipay_client_secret: str | None = None
    dipay_private_key_b64: str | None = None
    dipay_merchant_id: str | None = None
    biteship_origin_address: dict | None = None
    biteship_default_courier: str | None = None
    payment_provider: str | None = None
    courier_provider: str | None = None
    jubelio_origin_address: dict | None = None


def _masked(account_number: str | None) -> str | None:
    """Mask an account number — only the last 4 digits go to the client."""
    if not account_number:
        return None
    return f"•••• {account_number[-4:]}" if len(account_number) > 4 else account_number


def _bank_accounts_view(brand: Brand, accounts: list[PayoutBankAccount]) -> list[dict]:
    label_by_code = {b["code"]: b["label"] for b in bank_codes.BANKS}
    return [
        {
            "id": a.id,
            "bank_code": a.bank_code,
            "bank_label": label_by_code.get(a.bank_code, a.bank_code),
            "account_number_masked": _masked(a.account_number),
            "holder_name": a.holder_name,
            "is_active": a.is_active,
            # Which gateways can pay out to this bank (for UI badges).
            "supported_providers": bank_codes.supported_providers(a.bank_code),
        }
        for a in accounts
    ]


async def _brand_accounts(db: AsyncSession, brand_id: str) -> list[PayoutBankAccount]:
    q = await db.execute(
        select(PayoutBankAccount)
        .where(PayoutBankAccount.brand_id == brand_id)
        .order_by(PayoutBankAccount.created_at)
    )
    return list(q.scalars().all())


def _payouts_view(brand: Brand, accounts: list[PayoutBankAccount]) -> dict:
    return {
        "slug": brand.slug,
        "bank_accounts": _bank_accounts_view(brand, accounts),
        "bank_keys": [
            {
                "code": b["code"],
                "label": b["label"],
                "supported": bank_codes.supported_providers(b["code"]),
            }
            for b in bank_codes.BANKS
        ],
        "xendit_sub_account_id": brand.xendit_sub_account_id,
        # Dipay identifiers — never echo the secret or the RSA private key.
        # ``dipay_client_key`` is the public X-CLIENT-KEY; only the last 4
        # chars come back so admins can tell which key is configured.
        "dipay_merchant_id": brand.dipay_merchant_id,
        "dipay_client_key_masked": (
            "•••• " + brand.dipay_client_key[-4:]
            if brand.dipay_client_key and len(brand.dipay_client_key) > 4
            else brand.dipay_client_key
        ),
        "dipay_client_key_configured": bool(brand.dipay_client_key),
        "dipay_client_secret_configured": bool(brand.dipay_client_secret),
        "dipay_private_key_configured": bool(brand.dipay_private_key_b64),
        "biteship_origin_address": brand.biteship_origin_address,
        "biteship_default_courier": brand.biteship_default_courier,
        "payment_provider": (brand.payment_provider or "xendit")
        if (brand.payment_provider or "xendit") in ALLOWED_PAYMENT_PROVIDERS
        else "xendit",
        "courier_provider": "jubelio" if brand.jubelio_enabled else "biteship",
        "jubelio_origin_address": brand.jubelio_origin_address,
    }


_JUBELIO_ORIGIN_KEYS = {
    "name", "phone", "email", "address", "area_id", "zipcode", "coordinate",
}


def _validate_jubelio_origin(value: dict | None) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise HTTPException(422, "jubelio_origin_address must be an object")
    extra = set(value.keys()) - _JUBELIO_ORIGIN_KEYS
    if extra:
        raise HTTPException(422, f"jubelio_origin_address has unknown keys: {sorted(extra)}")
    coord = value.get("coordinate")
    if coord is not None:
        if not isinstance(coord, list) or len(coord) != 2:
            raise HTTPException(422, "jubelio_origin_address.coordinate must be a 2-element list")
        try:
            lat = float(coord[0])
            lng = float(coord[1])
        except (TypeError, ValueError):
            raise HTTPException(422, "jubelio_origin_address.coordinate must be numeric")
        # math.isfinite catches "NaN" and "Infinity" / "-Infinity" strings,
        # which float() otherwise accepts silently.
        import math
        if not (math.isfinite(lat) and math.isfinite(lng)):
            raise HTTPException(422, "jubelio_origin_address.coordinate must be finite")
    return value


async def _resolve_brand_for_edit(
    slug: str, profile: BeliAmanProfile, db: AsyncSession
) -> Brand:
    brand = (
        await db.execute(select(Brand).where(Brand.slug == slug))
    ).scalar_one_or_none()
    if brand is None:
        raise HTTPException(404, f"Brand '{slug}' not found")
    if not profile.is_super_admin:
        membership = (
            await db.execute(
                select(StoreMembership)
                .where(StoreMembership.profile_id == profile.id)
                .where(StoreMembership.store_slug == slug)
            )
        ).scalar_one_or_none()
        if membership is None:
            raise HTTPException(403, f"Not a member of store '{slug}'")
    return brand


@router.get("/{slug}/payouts", response_model=PayoutsOut)
async def get_payouts(
    slug: str,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict:
    brand = await _resolve_brand_for_edit(slug, profile, db)
    return _payouts_view(brand, await _brand_accounts(db, brand.id))


@router.put("/{slug}/payouts", response_model=PayoutsOut)
async def put_payouts(
    slug: str,
    body: PayoutsIn,
    profile: BeliAmanProfile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> dict:
    brand = await _resolve_brand_for_edit(slug, profile, db)

    def _normalize(v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    # --- payout bank accounts: delete → create/update → set active ---
    existing = {a.id: a for a in await _brand_accounts(db, brand.id)}

    if body.delete_bank_account_ids:
        for account_id in body.delete_bank_account_ids:
            account = existing.get(account_id)
            if account is None:
                raise HTTPException(404, f"Bank account '{account_id}' not found")
            await db.delete(account)
            del existing[account_id]
        # Flush so the partial unique index sees deleted rows gone before
        # anything else activates in the same transaction.
        await db.flush()

    if body.bank_accounts:
        for entry in body.bank_accounts:
            bank_code = _normalize(entry.bank_code)
            if bank_code is not None and bank_code not in {
                b["code"] for b in bank_codes.BANKS
            }:
                raise HTTPException(422, f"Unknown bank_code '{bank_code}'")
            if entry.id is not None:
                account = existing.get(entry.id)
                if account is None:
                    raise HTTPException(404, f"Bank account '{entry.id}' not found")
                if bank_code is not None:
                    account.bank_code = bank_code
                if entry.holder_name is not None:
                    account.holder_name = _normalize(entry.holder_name) or account.holder_name
                # account_number: write-only. None/omitted keeps the stored
                # number; a non-empty string replaces it.
                new_number = _normalize(entry.account_number)
                if new_number:
                    account.account_number = new_number
            else:
                account_number = _normalize(entry.account_number)
                if not account_number:
                    raise HTTPException(
                        422, "account_number is required when adding a bank account"
                    )
                if bank_code is None:
                    raise HTTPException(
                        422, "bank_code is required when adding a bank account"
                    )
                account = PayoutBankAccount(
                    brand_id=brand.id,
                    bank_code=bank_code,
                    account_number=account_number,
                    holder_name=(
                        _normalize(entry.holder_name) or brand.name or brand.slug
                    ),
                    is_active=False,
                )
                db.add(account)
                await db.flush()
                existing[account.id] = account

    if body.active_bank_account_id is not None:
        if body.active_bank_account_id != "":
            account = existing.get(body.active_bank_account_id)
            if account is None:
                raise HTTPException(
                    404, f"Bank account '{body.active_bank_account_id}' not found"
                )
            # Deactivate others FIRST, then flush, then activate — the
            # partial unique index (brand_id) WHERE is_active rejects two
            # active rows in one flush otherwise.
            for other in existing.values():
                if other.id != account.id:
                    other.is_active = False
            await db.flush()
            account.is_active = True
            await db.flush()

    if body.xendit_sub_account_id is not None:
        brand.xendit_sub_account_id = _normalize(body.xendit_sub_account_id)
    # Dipay rotation contract: omitted and JSON null preserve; explicit empty
    # strings clear. model_fields_set distinguishes omission from null.
    dipay_fields = (
        "dipay_client_key",
        "dipay_client_secret",
        "dipay_private_key_b64",
        "dipay_merchant_id",
    )
    for field in dipay_fields:
        if field in body.model_fields_set:
            value = getattr(body, field)
            if value is not None:
                setattr(brand, field, _normalize(value))
    if body.biteship_origin_address is not None:
        brand.biteship_origin_address = body.biteship_origin_address or None
    if body.biteship_default_courier is not None:
        brand.biteship_default_courier = _normalize(body.biteship_default_courier)
    if body.payment_provider is not None:
        normalized_pp = _normalize(body.payment_provider)
        if normalized_pp not in ALLOWED_PAYMENT_PROVIDERS:
            raise HTTPException(422, "Unsupported payment_provider")
        if normalized_pp == "dipay":
            from services.dipay_client import DipayError, resolve_config
            try:
                resolve_config(brand)
            except DipayError as exc:
                raise HTTPException(
                    422, "Complete Dipay credentials are required"
                ) from exc
            # The gateway pays out to the ACTIVE unified bank account.
            accounts = await _brand_accounts(db, brand.id)
            if not any(a.is_active for a in accounts):
                raise HTTPException(
                    422, "An active payout bank account is required"
                )
        brand.payment_provider = normalized_pp
    if body.courier_provider is not None:
        brand.jubelio_enabled = _normalize(body.courier_provider) == "jubelio"
    # Preserve an omitted origin, while an explicit JSON null or empty object
    # clears it. ``model_fields_set`` is the authoritative omission signal.
    if "jubelio_origin_address" in body.model_fields_set:
        brand.jubelio_origin_address = _validate_jubelio_origin(
            body.jubelio_origin_address if body.jubelio_origin_address
            else None
        )

    await db.flush()
    return _payouts_view(brand, await _brand_accounts(db, brand.id))
