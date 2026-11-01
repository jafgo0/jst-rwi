"""Environment-driven configuration. No secret is ever written in source."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RWI_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Providers
    openmeteo_base_url: str = "https://api.open-meteo.com/v1"
    openmeteo_ensemble_url: str = "https://ensemble-api.open-meteo.com/v1"
    openmeteo_archive_url: str = "https://archive-api.open-meteo.com/v1"
    openmeteo_api_key: str | None = None
    bom_ftp_base: str = "http://www.bom.gov.au/fwo"

    # Freshness policy (seconds). An older value is flagged STALE, never shown as fresh.
    max_observation_age_s: float = 1800.0
    max_forecast_age_s: float = 10800.0
    max_gps_age_s: float = 30.0

    # HTTP behaviour
    http_timeout_s: float = 15.0
    http_retries: int = 3
    http_backoff_s: float = 0.5

    # Caching
    cache_dir: Path = Field(default=Path("data/cache"))
    cache_ttl_forecast_s: float = 900.0
    cache_ttl_observation_s: float = 300.0

    # Operations
    log_level: str = "INFO"
    allow_network: bool = True
    """Set false in offline drills: providers then serve cache or fail explicitly."""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
