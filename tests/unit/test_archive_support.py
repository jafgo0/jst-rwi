"""Builders shared by the archive tests. No network, no shared state."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rwi.archive import SqliteArchive
from rwi.contracts import WeatherSeries, WeatherStep
from rwi.core.geo import GeoPoint, destination_point
from rwi.core.provenance import DataKind, Provenance, QualityFlag, measured

T0 = datetime(2027, 8, 20, 12, 0, tzinfo=UTC)
"""A valid time used by most tests."""
DARWIN = GeoPoint(latitude_deg=-12.4634, longitude_deg=130.8456, elevation_m=30.0)
STATION = GeoPoint(latitude_deg=-12.4000, longitude_deg=130.8800, elevation_m=25.0)


def hours(n: float) -> timedelta:
    return timedelta(hours=n)


def _prov(
    kind: DataKind, provider: str, when: datetime, valid: datetime, point: GeoPoint,
    model_name: str | None, init: datetime | None,
) -> Provenance:
    return Provenance(
        provider=provider,
        dataset="forecast" if kind is DataKind.FORECAST else "obs",
        kind=kind,
        retrieved_at_utc=when,
        valid_time_utc=valid if kind is DataKind.FORECAST else None,
        observation_time_utc=valid if kind is DataKind.OBSERVATION else None,
        model_name=model_name,
        model_init_time_utc=init,
        point=point,
    )


def forecast_series(
    *,
    valid_times: Sequence[datetime] = (T0,),
    value: float | Sequence[float] = 10.0,
    variable: str = "wind_speed_10m",
    init: datetime | None = None,
    retrieved: datetime | None = None,
    point: GeoPoint = DARWIN,
    provider: str = "open-meteo",
    model_name: str | None = "ecmwf_ifs025",
    quality: QualityFlag = QualityFlag.OK,
    license_: str | None = "CC-BY-4.0",
) -> WeatherSeries:
    """A forecast series. Defaults: initialised 6 h and retrieved 5 h before the first time."""
    first = valid_times[0]
    init = first - hours(6) if init is None else init
    retrieved = first - hours(5) if retrieved is None else retrieved
    values = [value] * len(valid_times) if isinstance(value, (int, float)) else list(value)
    steps = [
        WeatherStep(
            valid_time_utc=t,
            values={
                variable: measured(
                    variable, v,
                    _prov(DataKind.FORECAST, provider, retrieved, t, point, model_name, init),
                    quality=quality,
                )
            },
        )
        for t, v in zip(valid_times, values, strict=True)
    ]
    return WeatherSeries(
        point=point,
        requested_point=point,
        provider=provider,
        dataset="forecast",
        kind=DataKind.FORECAST,
        model_name=model_name,
        model_version="1",
        model_init_time_utc=init,
        retrieved_at_utc=retrieved,
        spatial_resolution_km=25.0,
        temporal_resolution_s=3600.0,
        license=license_,
        source_url="https://example.test/forecast",
        steps=steps,
    )


def observation_series(
    *,
    times: Sequence[datetime] = (T0,),
    value: float | Sequence[float] = 9.0,
    variable: str = "wind_speed_10m",
    point: GeoPoint = STATION,
    provider: str = "bom",
    retrieved: datetime | None = None,
) -> WeatherSeries:
    retrieved = times[-1] + timedelta(minutes=10) if retrieved is None else retrieved
    values = [value] * len(times) if isinstance(value, (int, float)) else list(value)
    steps = [
        WeatherStep(
            valid_time_utc=t,
            values={
                variable: measured(
                    variable, v,
                    _prov(DataKind.OBSERVATION, provider, retrieved, t, point, None, None),
                )
            },
        )
        for t, v in zip(times, values, strict=True)
    ]
    return WeatherSeries(
        point=point,
        provider=provider,
        dataset="obs",
        kind=DataKind.OBSERVATION,
        retrieved_at_utc=retrieved,
        license="BoM",
        steps=steps,
    )


def offset_point(origin: GeoPoint, distance_m: float, bearing_deg: float = 0.0) -> GeoPoint:
    return destination_point(origin, bearing_deg, distance_m)


def new_archive(tmp_path: Path, name: str = "rwi.sqlite3") -> SqliteArchive:
    return SqliteArchive(tmp_path / name)
