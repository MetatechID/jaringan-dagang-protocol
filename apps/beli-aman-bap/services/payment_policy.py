"""Local-only payment fixtures and fail-closed invoice configuration policy."""
from fastapi import HTTPException

from config import settings

DEMO_PAYMENT_LABEL = "BCA Virtual Account — Demo"


def local_payment_mocks_allowed() -> bool:
    # Explicit allowlist: empty/unknown modes must never enable fixtures.
    return settings.environment.strip().lower() in {"development", "test"}


def credential(value: str | None, fallback: str | None = None) -> str:
    """Match the clients' per-field override rule; blank overrides are invalid.

    An absent/empty override inherits the global value, but whitespace is not
    silently replaced by a different account's credentials.
    """
    return (value or fallback or "").strip()


def provider_credentials(provider: str, brand=None) -> tuple[str, str]:
    if provider == "xendit":
        return credential(settings.xendit_secret_key), credential(getattr(brand, "xendit_sub_account_id", None))
    return (
        credential(getattr(brand, f"{provider}_api_key", None), getattr(settings, f"{provider}_api_key")),
        credential(getattr(brand, f"{provider}_username", None), getattr(settings, f"{provider}_default_username")),
    )


def invoice_mock_mode(provider: str, brand) -> bool:
    """Validate before any mutation or outbound calls, including local paths."""
    key, account = provider_credentials(provider, brand)
    configured = (
        brand is not None
        and (getattr(brand, "payment_provider", None) or "xendit") == provider
        and bool(key and account)
    )
    if configured:
        return False
    if local_payment_mocks_allowed():
        return True
    raise HTTPException(503, "Payment provider is not configured")


def reject_nonlocal_demo_invoice(invoice_id=None, invoice_url=None) -> None:
    if local_payment_mocks_allowed():
        return
    if ((invoice_id or "").startswith(("dev-", "oy-dev-", "sento-dev-"))
            or "/api/mock-checkout/" in (invoice_url or "")):
        raise HTTPException(503, "Development payment invoice is unavailable")


def is_demo_payment_method(pm) -> bool:
    # Match the legacy seeded row without removing or rewriting stored data.
    return (pm.display_label or "").strip().casefold() == DEMO_PAYMENT_LABEL.casefold()
