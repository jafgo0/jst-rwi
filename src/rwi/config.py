"""Environment-driven configuration. No secret is ever written in source."""

from __future__ import annotations

import math
from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RWI_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Providers
    openmeteo_base_url: str = "https://api.open-meteo.com/v1"
    openmeteo_ensemble_url: str = "https://ensemble-api.open-meteo.com/v1"
    openmeteo_archive_url: str = "https://archive-api.open-meteo.com/v1"
    openmeteo_air_quality_url: str = "https://air-quality-api.open-meteo.com/v1"
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
    # Versioned ML post-processing artifacts (ADR 0008 §5). Empty or missing
    # means /api/ml answers "no_model" - never an empty band.
    models_dir: Path = Field(default=Path("data/models"))
    # Built race-week climatology files (ADR 0016), written by scripts/build_climatology.py.
    climatology_dir: Path = Field(default=Path("data/climatology"))

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
    # Archive ECMWF AIFS (native open-data GRIB) next to the Open-Meteo models
    # (ADR 0008: the AIFS verification history must start as early as possible).
    # Off by default: each new 6-hourly run is a download of tens of MB.
    capture_aifs: bool = False
    capture_aifs_margin_deg: float = 1.0

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

    # Meteorologist LLM adapter (Fas 1 free-form endpoint, /api/meteorologist/ask).
    # None keeps the adapter disabled and /ask answers 501 with a clear capability
    # message. The adapter is a CLASSIFIER only: it picks a question kind and a
    # variable from the user's free-text question and hands those to the
    # deterministic composer. No numbers are read from the model's response, so
    # a hallucinating model cannot invent a weather value. Keep secrets in the
    # environment (RWI_ANTHROPIC_API_KEY), never in the repository.
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-haiku-5-5"
    # /ask also requires X-Telemetry-Secret; this caps token spend per process.
    meteorologist_ask_per_minute: int = 6

    # On-board sensors (ADR 0010). Off by default. Calibration and mounting
    # values have NO defaults: rwi.telemetry validates them when the sensors
    # are switched on and refuses to start while any is missing.
    onboard_sensors_enabled: bool = False
    sensor_spool_path: Path = Field(default=Path("data/spool/sensors.sqlite3"))
    sensor_archive_interval_s: int = 60
    sensor_late_tolerance_s: float = 0.5
    # Airmar 200WX via NGX-1 + canboat (ADR 0009).
    airmar_serial: str | None = None
    airmar_mount: str | None = None  # solar_car | support_car
    airmar_height_m: float | None = None  # sensor height above the road, m
    airmar_drive_position: bool = False  # let its GNSS drive live_gps
    airmar_canboat_naive_utc: bool = False  # canboat timestamps without offset are UTC
    airmar_canboat_command: str | None = None  # e.g. "actisense-serial ... | analyzer -json -si"
    # EKO ML-01 through an external ADC.
    ml01_sensitivity_uv_per_wm2: float | None = None  # from the calibration certificate
    ml01_serial: str | None = None
    ml01_calibration_date: date | None = None
    ml01_input_unit: str | None = None  # mv | wm2
    ml01_mount: str | None = None  # solar_car | support_car | shared_with_airmar
    ml01_level_tolerance_deg: float | None = None
    ml01_assume_level: bool = False
    ml01_command: str | None = None  # prints "time_utc,value[,pitch_deg,roll_deg]" lines
    # Ensemble front-passage alerts (ADR 0012): operational choices, no default.
    front_alert_watch_probability: float | None = None
    front_alert_warning_probability: float | None = None
    # CAMS aerosol -> dynamic Linke turbidity (ADR 0014). Angstrom exponent for
    # AOD550 -> AOD700; it depends on aerosol type, so there is no default and
    # unset keeps the monthly climatology.
    aod_angstrom_exponent: float | None = None
    # NASA FIRMS active fires (ADR 0014). The MAP_KEY is personal: .env only.
    firms_map_key: SecretStr | None = None
    firms_base_url: str = "https://firms.modaps.eosdis.nasa.gov"
    firms_source: str | None = None  # e.g. VIIRS_NOAA20_NRT
    firms_day_range: int | None = None  # 1..5 days back (FIRMS API limit)
    firms_route_buffer_km: float | None = None  # hotspots kept within this of the route
    fire_alert_radius_km: float | None = None  # alert when a kept hotspot is this close
    fire_alert_min_confidence: str | None = None  # low | nominal | high
    fire_alert_min_frp_mw: float | None = None
    # Camp-site charging forecast (ADR 0015): operational choices, no default.
    # 10 m gust that flags a raised-array risk
    camp_gust_warning_m_s: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    # array minus dew point at or below this = dew risk
    camp_dew_margin_c: float | None = Field(default=None, allow_inf_nan=False)
    # cloud points along the sunset/sunrise azimuth, km from the stop
    camp_horizon_offsets_km: list[float] | None = None

    @field_validator("camp_horizon_offsets_km")
    @classmethod
    def _positive_offsets(cls, v: list[float] | None) -> list[float] | None:
        if v is not None and not all(math.isfinite(d) and d > 0.0 for d in v):
            raise ValueError(f"camp_horizon_offsets_km must all be positive, got {v!r}")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
