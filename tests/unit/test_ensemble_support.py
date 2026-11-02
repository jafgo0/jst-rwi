"""Shared builders for the ensemble tests (not a test module)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from rwi.contracts import EnsembleForecast, WeatherSeries, WeatherStep
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Measured, Provenance, measured

POINT = GeoPoint(latitude_deg=-12.4634, longitude_deg=130.8456, elevation_m=30.0)
T0 = datetime(2027, 8, 24, 0, 0, tzinfo=UTC)
RETRIEVED = datetime(2027, 8, 23, 20, 0, tzinfo=UTC)


def hour(n: int) -> datetime:
    return T0 + timedelta(hours=n)


def prov(valid: datetime) -> Provenance:
    return Provenance(
        provider="open-meteo",
        dataset="ensemble/test",
        kind=DataKind.FORECAST,
        retrieved_at_utc=RETRIEVED,
        valid_time_utc=valid,
        model_name="ecmwf_ifs025",
    )


def step(when: datetime, **values: float | None) -> WeatherStep:
    return WeatherStep(
        valid_time_utc=when,
        values={k: measured(k, v, prov(when)) for k, v in values.items()},
    )


def member(
    label: str,
    variable: str,
    values_by_hour: Mapping[int, float | None],
    *,
    point: GeoPoint = POINT,
) -> WeatherSeries:
    return WeatherSeries(
        point=point,
        provider="open-meteo",
        dataset=f"ensemble/{label}",
        kind=DataKind.FORECAST,
        model_name="ecmwf_ifs025",
        retrieved_at_utc=RETRIEVED,
        spatial_resolution_km=25.0,
        temporal_resolution_s=3600.0,
        steps=[step(hour(h), **{variable: v}) for h, v in sorted(values_by_hour.items())],
    )


def ensemble(
    variable: str,
    columns: Sequence[Mapping[int, float | None]],
    *,
    model_name: str = "ecmwf_ifs025",
) -> EnsembleForecast:
    """One member per mapping ``{hour offset: value}``."""
    return EnsembleForecast(
        point=POINT,
        provider="open-meteo",
        model_name=model_name,
        retrieved_at_utc=RETRIEVED,
        members=[member(f"member{i:02d}", variable, col) for i, col in enumerate(columns)],
    )


def at_one_time(variable: str, values: Sequence[float], *, h: int = 0) -> EnsembleForecast:
    """Ensemble with one member per value, all at hour offset ``h``."""
    return ensemble(variable, [{h: v} for v in values])


def deterministic(variable: str, values: Mapping[int, float | None]) -> WeatherSeries:
    return member("deterministic", variable, values)


def all_measured(series: WeatherSeries) -> list[tuple[datetime, str, Measured]]:
    return [(s.valid_time_utc, k, m) for s in series.steps for k, m in s.values.items()]
