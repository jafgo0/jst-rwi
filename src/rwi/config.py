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

    # Route elevation enrichment. Points at an operator-local copy of
    # JU Solar Team's wsc_race_data.csv. The platform does not bundle it;
    # leaving the path at None means the loaded route carries no elevation,
    # which rwi.route.geometry.grade_at_distance already handles honestly
    # (returns 0.0 and says "unknown", not "flat").
    team_elevation_csv: Path | None = None
    #: How far a waypoint may be from the nearest team row before enrichment
    #: refuses to attach a bad elevation. The known Adelaide approach
    #: disagreement is about 2 km, which 3000 m clears.
    team_elevation_max_match_m: float = 3000.0

    # Per-cell angular response matrix from the team's shadow-analysis.
    # Points at a local copy of their efficiency_matrix.mat; leaving it at
    # None means DEFAULT_ARRAY stays flat-plane (which overestimates DC power
    # by ~2.5x at full sun on a cambered vehicle body, see
    # docs/validation/first-pv-validation.md).
    team_shadow_matrix: Path | None = None
    cache_ttl_forecast_s: float = 900.0
    cache_ttl_observation_s: float = 300.0

    # Forecast archive (ADR 0007). A file, not a service.
    archive_path: Path = Field(default=Path("data/archive/rwi.sqlite3"))
    archive_max_bytes: int = 4 * 1024 * 1024 * 1024
    archive_retention_days: float = 400.0

    # Capture worker: builds the verification history. Off by default, because a
    # process that silently makes provider calls on a metered connection in the
    # outback must be switched on deliberately.
    capture_enabled: bool = False
    capture_interval_s: float = 3600.0
    capture_forecast_hours: float = 48.0
    capture_jitter_s: float = 60.0
    capture_max_failures: int = 10

    # Operations
    log_level: str = "INFO"
    allow_network: bool = True
    """Set false in offline drills: providers then serve cache or fail explicitly."""

    # Trusted-source token for POST /api/state/position with mode=live_gps.
    # Set via RWI_TELEMETRY_SHARED_SECRET on a trusted private LAN. When
    # None (the default) POST with live_gps is rejected and the platform
    # only accepts manual/sim fixes over HTTP. A real on-car telemetry
    # pipeline should go through rwi.telemetry.TelemetryCoordinator, which
    # applies monotonicity and jump gates the single-shot HTTP endpoint
    # cannot.
    telemetry_shared_secret: str | None = None

    # Jump gate for the HTTP position endpoint, applied to live_gps fixes
    # only. A jump that implies ground speed over this many m/s against the
    # previous fix tags the new fix as suspect (fix_quality=UNKNOWN) and
    # logs it; the fix is still accepted because dropping it silently
    # freezes the dashboard where the previous fix was.
    http_live_jump_speed_mps: float = 110.0  # ~400 km/h

    # Future-time grace for inbound fixes. A GPS receiver may stamp a fix a
    # second or two ahead of server clock; anything further is a sign of
    # clock corruption and is rejected.
    telemetry_future_grace_s: float = 5.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
