"""Gateway service configuration.

Loaded from environment variables with sensible defaults for local development.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Gateway service settings."""

    model_config = {"env_prefix": "", "case_sensitive": False}

    # Server
    port: int = 4030
    host: str = "0.0.0.0"
    debug: bool = False

    # Registry
    registry_url: str = "http://localhost:3030"

    # Redis
    redis_url: str = "redis://localhost:6379/2"

    # Timeout for multicast requests to BPPs (seconds)
    bpp_timeout: float = 10.0

    # Cache TTL for registry lookups (seconds)
    cache_ttl: int = 120


settings = Settings()
