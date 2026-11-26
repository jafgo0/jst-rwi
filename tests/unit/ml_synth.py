"""Synthetic forecast/observation pairs for the rwi.ml tests. Not a test module.

The "truth" is pvlib clear sky times a random clear-sky index; the "forecast"
is the truth scaled by a known bias plus noise, so a post-processor has
something real to learn and its skill over the raw forecast is checkable.
"""

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from rwi.contracts import ForecastObservationPair
from rwi.core.geo import GeoPoint
from rwi.ml.features import solar_context

# Test fixture only: a Stuart Highway location (Tennant Creek).
POINT = GeoPoint(latitude_deg=-19.65, longitude_deg=134.19, elevation_m=375.0)
START = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
#: Upstream GHI overshoots the truth by this factor.
FORECAST_BIAS = 1.25


def ghi_pairs(
    days: int = 30,
    *,
    seed: int = 7,
    provider: str = "open-meteo",
    model_name: str | None = "best_match",
    start: datetime = START,
) -> list[ForecastObservationPair]:
    """Hourly GHI pairs from one 00Z run per day, recorded 30 min after init."""
    rng = np.random.default_rng(seed)
    out: list[ForecastObservationPair] = []
    for d in range(days):
        init = start + timedelta(days=d)
        recorded = init + timedelta(minutes=30)
        times = [init + timedelta(hours=h) for h in range(1, 25)]
        cs = solar_context([POINT] * len(times), times).clear_sky_for("ghi")
        k_true = rng.uniform(0.4, 1.0, size=len(times))
        for t, c, k in zip(times, cs, k_true, strict=True):
            obs = float(c * k)
            fc = max(0.0, obs * FORECAST_BIAS + float(rng.normal(0.0, 15.0)))
            out.append(
                ForecastObservationPair(
                    variable="ghi",
                    valid_time_utc=t,
                    forecast_value=fc,
                    observed_value=obs,
                    lead_time_s=(t - init).total_seconds(),
                    provider=provider,
                    model_name=model_name,
                    forecast_point=POINT,
                    observation_point=POINT,
                    separation_m=0.0,
                    time_offset_s=0.0,
                    forecast_recorded_at_utc=recorded,
                )
            )
    return out


def wind_pairs(days: int = 20, *, seed: int = 3) -> list[ForecastObservationPair]:
    """Hourly 10 m wind speed pairs; forecast = truth + 2 m/s + noise."""
    rng = np.random.default_rng(seed)
    out: list[ForecastObservationPair] = []
    for d in range(days):
        init = START + timedelta(days=d)
        for h in range(1, 25):
            t = init + timedelta(hours=h)
            obs = float(rng.uniform(1.0, 12.0))
            out.append(
                ForecastObservationPair(
                    variable="wind_speed_10m",
                    valid_time_utc=t,
                    forecast_value=obs + 2.0 + float(rng.normal(0.0, 0.5)),
                    observed_value=obs,
                    lead_time_s=h * 3600.0,
                    provider="open-meteo",
                    model_name="best_match",
                    forecast_point=POINT,
                    observation_point=POINT,
                    separation_m=0.0,
                    time_offset_s=0.0,
                    forecast_recorded_at_utc=init + timedelta(minutes=30),
                )
            )
    return out


def require_lightgbm() -> None:
    """Skip the calling test module when lightgbm (or its native libomp) cannot load."""
    try:
        import lightgbm  # noqa: F401
    except (ImportError, OSError) as exc:
        pytest.skip(f"lightgbm unavailable: {exc}", allow_module_level=True)
