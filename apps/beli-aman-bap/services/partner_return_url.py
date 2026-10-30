"""Exact per-partner browser return origins; never an outbound callback policy."""

from urllib.parse import unquote, urlsplit

from config import settings


def _origin(value: str) -> str:
    decoded = value
    for _ in range(3):
        if any(ord(c) < 33 or ord(c) == 127 or c in '\\<>"\'' for c in decoded):
            raise ValueError("Unsafe partner return URL")
        decoded = unquote(decoded)
    try:
        parts = urlsplit(value)
        if not parts.hostname or parts.username is not None or parts.password is not None:
            raise ValueError("Unsafe partner return URL")
        host = parts.hostname
        if not host.isascii() or host.endswith('.') or '%' in parts.netloc:
            raise ValueError("Unsafe partner return URL")
        local = host in {"localhost", "127.0.0.1", "::1"}
        if parts.scheme != "https" and not (
            parts.scheme == "http" and local
            and settings.environment.strip().lower() in {"development", "test"}
        ):
            raise ValueError("HTTPS partner return URL required")
        port = parts.port
        if port == 0 or parts.netloc.endswith(":"):
            raise ValueError("Unsafe partner return URL")
    except (ValueError, UnicodeError):
        raise ValueError("Unsafe partner return URL") from None
    host = f"[{host}]" if ':' in host else host
    default = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")


def validate_return_url(value: str | None, brand_id: str) -> str | None:
    if value is None:
        return None
    if not value or len(value) > 2048:
        raise ValueError("Unsafe partner return URL")
    origin = _origin(value)
    allowed = settings.partner_return_origins.get(str(brand_id), [])
    for entry in allowed:
        try:
            parts = urlsplit(entry)
            if parts.path not in {"", "/"} or parts.query or parts.fragment:
                continue
            if _origin(entry) == origin:
                return value
        except ValueError:
            continue
    raise ValueError("Partner return origin is not allowed")


def stored_return_url(order) -> str | None:
    """Legacy/config-invalid destinations degrade to no external navigation."""
    try:
        return validate_return_url(order.success_url, order.brand_id)
    except ValueError:
        return None
