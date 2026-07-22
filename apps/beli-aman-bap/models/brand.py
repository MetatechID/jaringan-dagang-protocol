"""Brand — a partner brand (BPP) using Beli Aman.

In v1 we seed three brands: antarestar, gendes, yourbrand.
"""

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Brand(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A partner brand with its own storefront and (eventually) its own BPP."""

    __tablename__ = "brands"

    slug: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    bpp_id: Mapped[str] = mapped_column(String(255), nullable=False)
    bpp_uri: Mapped[str | None] = mapped_column(String(255), nullable=True)
    default_warehouse_address: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    fee_pct_bp: Mapped[int] = mapped_column(default=0, nullable=False)  # 0 in v1

    # Which payment gateway mints the buyer-facing invoice. Defaults to
    # "xendit"; flip to "oy" after seeding the ``oy_*`` columns below. The
    # dispatcher in routers/orders.py + routers/checkout.py reads this at
    # invoice creation time.
    payment_provider: Mapped[str] = mapped_column(
        String(16), nullable=False, default="xendit", index=True,
    )

    # Xendit XenPlatform sub-account this brand's funds route through.
    # Funds custody stays with Xendit — we emit this as ``for-user-id``
    # header so each invoice settles into the brand's Xendit balance, never
    # ours. Null = brand not yet onboarded; checkout will refuse.
    xendit_sub_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Bank account funds are disbursed to on release. Code = Xendit bank
    # code (e.g. "BCA", "MANDIRI"). Account number is the seller's bank
    # account number (digits only). Holder name is required by Xendit's
    # disbursement payload.
    xendit_disbursement_bank_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    xendit_disbursement_bank_account: Mapped[str | None] = mapped_column(String(64), nullable=True)
    xendit_disbursement_holder_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # OY Indonesia credentials. Per-Brand so each tenant can have its own
    # OY sub-account / API key; ``oy_default_username`` in settings acts as
    # the global fallback username when this column is unset (mirrors how
    # the Xendit ``xendit_secret_key`` master sits in env + per-Brand
    # routing sits in this table). Plaintext v1 — encrypt at rest when the
    # KMS / Vault-of-record lands.
    oy_api_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    oy_username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    oy_callback_secret: Mapped[str | None] = mapped_column(String(128), nullable=True)
    oy_store_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Sento credentials. Per-Brand — same pattern as the OY block above.
    # Sento's API uses ``x-api-key`` + ``x-username`` headers (no HMAC);
    # ``sento_callback_secret`` is reserved for a future shared-secret
    # scheme (Sento's docs don't currently document one). Plaintext v1.
    sento_api_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sento_username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sento_callback_secret: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Biteship pickup origin used as the ``origin`` payload when creating
    # shipment orders. Shape: {contact_name, contact_phone, contact_email,
    # address, postal_code, latitude, longitude}.
    biteship_origin_address: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Default courier preselected on the seller dashboard book-shipment
    # picker. Free-text Biteship courier_code + service_code joined by ":".
    biteship_default_courier: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Per-brand carrier override. When True, rates + booking go through
    # Jubelio instead of the global settings.default_carrier.
    jubelio_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    # Jubelio pickup origin used as the ``origin`` payload on /shipments/create
    # and the origin zipcode on /rates. Shape: {name, phone, email, address,
    # area_id, zipcode, coordinate}.
    jubelio_origin_address: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
