"""
Configuration Management
========================
All settings are read from environment variables (12-factor app pattern).
Secrets are never hardcoded — use a .env file locally, vault/secrets manager in production.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Service metadata ───────────────────────────────────────────────────
    VERSION: str = "1.0.0"
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    LOG_LEVEL: str = "INFO"

    # ── API Keys (injected via env, never committed) ───────────────────────
    ANTHROPIC_API_KEY: str = ""

    # ── Extraction settings ────────────────────────────────────────────────
    # Claude model to use for vision extraction
    AI_MODEL: str = "claude-sonnet-4-6"
    # Confidence threshold below which a field is flagged for human review
    CONFIDENCE_THRESHOLD: float = 0.75
    # Maximum PDF size accepted (bytes)
    MAX_FILE_SIZE_MB: int = 20
    # Supported MIME types
    ALLOWED_MIME_TYPES: list[str] = ["application/pdf", "image/jpeg", "image/png", "image/tiff"]

    # ── Redis (job queue) ──────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── Database ───────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://fiscal:fiscal@localhost:5432/fiscal_docs"

    # ── Security ───────────────────────────────────────────────────────────
    API_KEY_HEADER: str = "X-API-Key"
    # Comma-separated list of valid API keys (use proper secret management in prod)
    API_KEYS: str = "dev-key-change-me"
    ALLOWED_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:8080"]

    @property
    def valid_api_keys(self) -> set[str]:
        return {k.strip() for k in self.API_KEYS.split(",") if k.strip()}

    @property
    def max_file_size_bytes(self) -> int:
        return self.MAX_FILE_SIZE_MB * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton — reads env once at startup."""
    return Settings()


settings = get_settings()
