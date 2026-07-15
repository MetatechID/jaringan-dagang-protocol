"""Beli Aman BAP service configuration.

Loads settings from environment variables with sensible defaults for
local development. The Beli Aman BAP runs on port 8003 by default to
avoid colliding with the JD BAP (8002).
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = {"env_prefix": "", "env_file": ".env", "extra": "ignore"}

    # --- Service identity ---
    # Canonical Beckn subscriber_id (Task A3): ``*.jaringan-dagang.id`` is
    # the network-wide source of truth. Override at runtime via env var
    # SUBSCRIBER_ID for non-prod environments (the deployed BAP runs at
    # api.beli-aman.metatech.id but the *network identity* is the canonical
    # value below — DNS host != subscriber_id).
    subscriber_id: str = "beli-aman.bap.jaringan-dagang.id"
    # Public URL where the BAP receives signed Beckn callbacks. Deployed
    # value is api.beli-aman.metatech.id/api/v1/beckn; the local dev
    # default below mirrors how the seller fans out /on_search etc.
    subscriber_url: str = "http://localhost:8003/api/v1/beckn"
    unique_key_id: str = "key-1"
    service_name: str = "beli-aman-bap"

    # --- Beckn network (kept for future Beckn round-trip; unused in v1) ---
    # registry / gateway URLs: env-overridable. The canonical deployed
    # hosts are registry.jaringan-dagang.id / gateway.jaringan-dagang.id
    # but those aren't reachable locally — keep localhost defaults and
    # let the deployed env set REGISTRY_URL / GATEWAY_URL.
    registry_url: str = "http://localhost:3030"
    gateway_url: str = "http://localhost:4030"
    # ``domain`` is the legacy Beckn-base label kept for backward compat;
    # outbound envelopes use python.domain_resolver.resolve_ondc_domain
    # (A1/A2b) and emit per-BPP ONDC:RET* codes instead of this string.
    domain: str = "retail"
    core_version: str = "1.1.0"
    # Canonical Beckn city code per network-extension/cities.yaml (Jakarta).
    city_code: str = "std:021"
    country_code: str = "IDN"

    # --- Database ---
    database_url: str = (
        "postgresql+asyncpg://postgres:secret@localhost:5432/beli_aman"
    )

    # --- Firebase Admin ---
    # Paste the entire service-account JSON as a single env var. NEVER commit.
    firebase_service_account_json: str = ""

    # --- Demo / admin ---
    # Random shared secret. Gates the /api/v1/internal-mock/* endpoints used
    # by the admin cockpit at /admin?token=.... MUST match the storefront's
    # NEXT_PUBLIC_ADMIN_DEMO_TOKEN value.
    admin_token: str = "dev-admin-token"

    # --- CORS ---
    # Comma-separated list of allowed origins.
    allowed_origins: str = (
        "http://localhost:3000,http://localhost:3002,http://localhost:3003,"
        "https://beli-aman.metatech.id"
    )

    # --- Seller bridge (best-effort POST when an order moves to ESCROW_HELD) ---
    seller_bridge_url: str = "http://localhost:8001"
    seller_bridge_token: str = "dev-seller-bridge-token"
    seller_bridge_enabled: bool = True

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8003
    debug: bool = True

    # --- Auto-release window for escrow (days after delivered) ---
    auto_release_days: int = 3

    # --- Xendit (XenPlatform sub-account routing) ---
    # Platform secret key. Used as Basic auth username. Per-invoice / per-
    # disbursement routing is via the ``for-user-id`` header set from the
    # brand's ``xendit_sub_account_id`` — funds custody stays with Xendit.
    xendit_secret_key: str = ""
    # Token Xendit signs callbacks with (set in Xendit dashboard → Settings →
    # Callbacks). Verified against header ``x-callback-token``.
    xendit_webhook_token: str = ""
    # Public BAP base URL Xendit posts callbacks to. Used to construct
    # invoice success/failure redirect URLs.
    xendit_callback_base_url: str = "https://api.beli-aman.metatech.id"
    # How long a Xendit Invoice stays valid before EXPIRED webhook fires.
    xendit_invoice_duration_seconds: int = 86400  # 24h

    # --- OY Indonesia ---
    # Master API key (per-Brand override lives in Brand.oy_api_key). Send
    # in the ``x-api-key`` header. See https://api-docs.oyindonesia.com/.
    oy_api_key: str = ""
    # Default username; per-Brand override in Brand.oy_username. Send in
    # the ``x-oy-username`` header.
    oy_default_username: str = ""
    # Public BAP base URL OY posts callbacks to. Constructs invoice
    # success/failure redirect URLs the same way xendit_callback_base_url
    # does for Xendit.
    oy_callback_base_url: str = "https://api.beli-aman.metatech.id"
    # Optional override for the mock-checkout URL OY carts hit while real
    # keys aren't configured. Defaults to settings.mock_checkout_public_base
    # in oy_invoices.py (single mock-checkout page serves both providers).
    oy_invoice_duration_seconds: int = 86400  # 24h
    # Base URL for the mock-checkout page served by seller-bpp during local
    # dev (when no real OY API key is set). Must point at the seller-bpp
    # host, not prod. Read by oy_invoices._mock_mode() fallback.
    mock_checkout_public_base: str = ""

    # --- Sento (https://api-docs.sento.id/) ---
    # Base URL for Sento's HTTP API. Default is the sandbox
    # (https://api-demo.sento.id); production overrides via
    # SENTO_BASE_URL=https://partner.sento.id in .env. Read by
    # services.sento_client._base_url().
    sento_base_url: str = "https://api-demo.sento.id"
    # Master API key (per-Brand override lives in Brand.sento_api_key). Send
    # in the ``x-api-key`` header. Empty value → mock-mode (synthetic URL,
    # same pattern as OY).
    sento_api_key: str = ""
    # Default username; per-Brand override in Brand.sento_username. Send in
    # the ``x-username`` header.
    sento_default_username: str = ""
    # Public BAP base URL Sento posts callbacks to. Constructs the invoice
    # success/failure redirect URLs the same way oy_callback_base_url does.
    sento_callback_base_url: str = "https://api.beli-aman.metatech.id"
    # How long a Sento payment link stays valid before Sento marks it
    # EXPIRED. 24h matches OY's default.
    sento_invoice_duration_seconds: int = 86400  # 24h

    # --- Biteship (live courier API) ---
    biteship_api_base: str = "https://api.biteship.com"
    biteship_api_key: str = ""
    # Static token Biteship posts in the ``Authorization`` header on tracking
    # webhooks (set in Biteship dashboard → Integrations → Webhooks).
    biteship_webhook_token: str = ""

    # --- Jubelio Shipment (live courier API) ---
    # Sandbox base: https://api-shipment.sandbox.jubelio.com
    # Production:   https://api-shipment.jubelio.com
    jubelio_api_base: str = "https://api-shipment.sandbox.jubelio.com"
    jubelio_client_id: str = ""
    jubelio_client_secret: str = ""
    # Shared secret Jubelio sends in the ``x-jubelio-signature`` header on
    # webhook calls (set in Jubelio dashboard → Setting → Developer → Webhook).
    jubelio_webhook_token: str = ""
    # Which carrier drives rates + booking by default. Per-brand override via
    # ``brand.jubelio_enabled``. Values: "jubelio" | "biteship".
    default_carrier: str = "jubelio"

    # --- Environment flag (controls test-only fallbacks like shipping mock) ---
    environment: str = "development"  # production | staging | development | test

    # --- Passwordless OTP login (WA + email) ---
    # whatsmeow sidecar HTTP base URL (jd-wa-whatsmeow). Empty in dev → OTPs
    # are logged to stdout instead of sent. Production: http://127.0.0.1:7820.
    wa_sidecar_url: str = ""
    # Bearer secret that gates the sidecar's HTTP API. Must match the sidecar's
    # ``WA_SIDECAR_SHARED_SECRET`` env var.
    wa_sidecar_shared_secret: str = ""
    # Inbox id used to send system OTPs through the multi-tenant sidecar.
    # Pair this inbox to a dedicated WA number once (QR scan), then forget.
    wa_sidecar_otp_inbox_id: str = "system-otp"

    # SMTP for email OTP. When ``smtp_host`` is empty, the email_sender logs
    # the OTP to stdout (dev mode). Karya1's defaults are:
    #   SMTP_HOST=smtp.gmail.com  SMTP_PORT=587  SMTP_SECURE=false (STARTTLS)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_secure: bool = False  # True → implicit TLS (port 465); False → STARTTLS on 587
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_from: str = "Beli Aman <noreply@beliaman.com>"


settings = Settings()
