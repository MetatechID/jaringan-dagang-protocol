"""Brand catalog + per-brand payouts/fulfillment admin endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_profile
from models.brand import Brand
from models.profile import BeliAmanProfile
from models.store_membership import StoreMembership
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


class PayoutsOut(BaseModel):
    slug: str
    xendit_sub_account_id: str | None = None
    xendit_disbursement_bank_code: str | None = None
    xendit_disbursement_bank_account_masked: str | None = None
    xendit_disbursement_holder_name: str | None = None
    sento_disbursement_bank_code: str | None = None
    sento_disbursement_bank_account_masked: str | None = None
    sento_disbursement_holder_name: str | None = None
    dipay_client_key_masked: str | None = None
    dipay_client_key_configured: bool
    dipay_client_secret_configured: bool
    dipay_private_key_configured: bool
    dipay_merchant_id: str | None = None
    dipay_disbursement_bank_code: str | None = None
    dipay_disbursement_bank_account_masked: str | None = None
    dipay_disbursement_holder_name: str | None = None
    biteship_origin_address: dict | None = None
    biteship_default_courier: str | None = None
    payment_provider: str
    courier_provider: str
    jubelio_origin_address: dict | None = None


class PayoutsIn(BaseModel):
    xendit_sub_account_id: str | None = None
    xendit_disbursement_bank_code: str | None = None
    xendit_disbursement_bank_account: str | None = None
    xendit_disbursement_holder_name: str | None = None
    sento_disbursement_bank_code: str | None = None
    sento_disbursement_bank_account: str | None = None
    sento_disbursement_holder_name: str | None = None
    # Dipay (SNAP v2.1) — client key/secret + RSA private key (base64 PEM)
    # for request signing, merchant id for QRIS, and the bank account the
    # brand pays out to on escrow release (see services/dipay_*.py).
    dipay_client_key: str | None = None
    dipay_client_secret: str | None = None
    dipay_private_key_b64: str | None = None
    dipay_merchant_id: str | None = None
    dipay_disbursement_bank_code: str | None = None
    dipay_disbursement_bank_account: str | None = None
    dipay_disbursement_holder_name: str | None = None
    biteship_origin_address: dict | None = None
    biteship_default_courier: str | None = None
    payment_provider: str | None = None
    courier_provider: str | None = None
    jubelio_origin_address: dict | None = None


def _payouts_view(brand: Brand) -> dict:
    return {
        "slug": brand.slug,
        "xendit_sub_account_id": brand.xendit_sub_account_id,
        "xendit_disbursement_bank_code": brand.xendit_disbursement_bank_code,
        # Mask the account number — only last 4 digits go back to the client.
        "xendit_disbursement_bank_account_masked": (
            "•••• " + brand.xendit_disbursement_bank_account[-4:]
            if brand.xendit_disbursement_bank_account
            and len(brand.xendit_disbursement_bank_account) > 4
            else brand.xendit_disbursement_bank_account
        ),
        "xendit_disbursement_holder_name": brand.xendit_disbursement_holder_name,
        # Sento disbursement ("remit") target — used when payment_provider ==
        # "sento". Bank code is Sento's NUMERIC code (e.g. "014" BCA).
        "sento_disbursement_bank_code": brand.sento_disbursement_bank_code,
        "sento_disbursement_bank_account_masked": (
            "•••• " + brand.sento_disbursement_bank_account[-4:]
            if brand.sento_disbursement_bank_account
            and len(brand.sento_disbursement_bank_account) > 4
            else brand.sento_disbursement_bank_account
        ),
        "sento_disbursement_holder_name": brand.sento_disbursement_holder_name,
        # Dipay (SNAP) disbursement target — used when payment_provider ==
        # "dipay". Like Sento above: masked account, plain code/holder.
        "dipay_disbursement_bank_code": brand.dipay_disbursement_bank_code,
        "dipay_disbursement_bank_account_masked": (
            "•••• " + brand.dipay_disbursement_bank_account[-4:]
            if brand.dipay_disbursement_bank_account
            and len(brand.dipay_disbursement_bank_account) > 4
            else brand.dipay_disbursement_bank_account
        ),
        "dipay_disbursement_holder_name": brand.dipay_disbursement_holder_name,
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
    return _payouts_view(brand)


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

    if body.xendit_sub_account_id is not None:
        brand.xendit_sub_account_id = _normalize(body.xendit_sub_account_id)
    if body.xendit_disbursement_bank_code is not None:
        brand.xendit_disbursement_bank_code = _normalize(body.xendit_disbursement_bank_code)
    if body.xendit_disbursement_bank_account is not None:
        brand.xendit_disbursement_bank_account = _normalize(body.xendit_disbursement_bank_account)
    if body.xendit_disbursement_holder_name is not None:
        brand.xendit_disbursement_holder_name = _normalize(body.xendit_disbursement_holder_name)
    if body.sento_disbursement_bank_code is not None:
        brand.sento_disbursement_bank_code = _normalize(body.sento_disbursement_bank_code)
    if body.sento_disbursement_bank_account is not None:
        brand.sento_disbursement_bank_account = _normalize(body.sento_disbursement_bank_account)
    if body.sento_disbursement_holder_name is not None:
        brand.sento_disbursement_holder_name = _normalize(body.sento_disbursement_holder_name)
    # Dipay rotation contract: omitted and JSON null preserve; explicit empty
    # strings clear. model_fields_set distinguishes omission from null.
    dipay_fields = (
        "dipay_client_key",
        "dipay_client_secret",
        "dipay_private_key_b64",
        "dipay_merchant_id",
        "dipay_disbursement_bank_code",
        "dipay_disbursement_bank_account",
        "dipay_disbursement_holder_name",
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
            if not (
                brand.dipay_disbursement_bank_code
                and brand.dipay_disbursement_bank_account
                and brand.dipay_disbursement_holder_name
            ):
                raise HTTPException(
                    422, "Complete Dipay payout bank configuration is required"
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
    return _payouts_view(brand)
