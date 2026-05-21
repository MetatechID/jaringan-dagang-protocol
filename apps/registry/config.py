"""Registry service configuration.

Loaded from environment variables with sensible defaults for local development.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Registry service settings."""

    model_config = {"env_prefix": "", "case_sensitive": False}

    # Server
    port: int = 3030
    host: str = "0.0.0.0"
    debug: bool = False

    # Database
    database_url: str = "postgresql+asyncpg://jaringan:jaringan_dev@localhost:5433/jaringan_dagang"

    # Redis
    redis_url: str = "redis://localhost:6379/1"

    # Cache TTL in seconds for registry lookups
    cache_ttl: int = 300

    @property
    def async_database_url(self) -> str:
        """Ensure the database URL uses the asyncpg driver."""
        url = self.database_url
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url


settings = Settings()
