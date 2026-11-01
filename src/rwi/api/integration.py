"""Glue between the weather, solar and energy modules.

This is where a provider's forecast becomes the input of a physical model, and
it is the one place allowed to make that translation. Two rules govern it:

  * a forecast stays a forecast - the irradiance handed to the PV model keeps
    ``DataKind.FORECAST`` and the provider's provenance, so nothing downstream
    can mistake it for a measurement or for a clear-sky bound;
  * geometry wins over the provider - if the Sun is below the horizon at the
    active position, irradiance is zero no matter what a grid-interpolated
    forecast says.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from rwi.api.capability import require_attr
from rwi.contracts import IrradianceComponents, SolarPosition, WeatherSeries
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import utc_now
from rwi.core.wind import relative_wind

IRRADIANCE_VARIABLES = ("ghi", "dni", "dhi")
ENERGY_VARIABLES = (
    "ghi",
    "dni",
    "dhi",
    "temperature_2m",
    "wind_speed_10m",
    "wind_direction_10m",
    "cloud_cover",
)


def solar_positions_for(point: GeoPoint, times: list[datetime]) -> list[SolarPosition]:
    solar_positions: Any = require_attr(
        "rwi.solar.geometry", "solar_positions", capability="solar geometry"
    )
    return list(solar_positions(point, times))


def irradiance_from_series(
    series: WeatherSeries,
    solar: list[SolarPosition],
    *,
    point: GeoPoint,
) -> list[IrradianceComponents | None]:
    """Turn a provider series into irradiance components, one per solar position.

    Returns ``None`` for a step where the provider gave no usable irradiance, so
    the caller can skip it rather than model a fabricated zero.
    """
    decompose: Any | None
    try:
        decompose = require_attr(
            "rwi.solar.decomposition", "decompose_ghi", capability="irradiance decomposition"
        )
    except Exception:
        decompose = None

    out: list[IrradianceComponents | None] = []
    for position in solar:
        step = series.nearest_step(position.time_utc, tolerance_s=1800.0)
        if step is None:
            out.append(None)
            continue
        ghi = step.value("ghi")
        dni = step.value("dni")
        dhi = step.value("dhi")
        if ghi is None:
            out.append(None)
            continue

        source = step.get("ghi")
        base = source.provenance if source is not None else series.base_provenance()
        night = position.apparent_elevation_deg <= 0.0

        if night:
            ghi = dni = dhi = 0.0
        elif (dni is None or dhi is None) and decompose is not None:
            decomposed = decompose(ghi, position, provenance=base)
            dni = decomposed.dni_w_m2 if dni is None else dni
            dhi = decomposed.dhi_w_m2 if dhi is None else dhi
        if dni is None or dhi is None:
            out.append(None)
            continue

        provenance = Provenance(
            provider=base.provider,
            dataset=base.dataset,
            kind=base.kind,
            retrieved_at_utc=base.retrieved_at_utc,
            valid_time_utc=position.time_utc if base.kind is DataKind.FORECAST else None,
            observation_time_utc=(
                position.time_utc if base.kind is DataKind.OBSERVATION else None
            ),
            model_name=base.model_name,
            model_version=base.model_version,
            model_init_time_utc=base.model_init_time_utc,
            point=series.point,
            requested_point=point,
            spatial_resolution_km=base.spatial_resolution_km,
            temporal_resolution_s=base.temporal_resolution_s,
            processing="provider_irradiance"
            + ("+night_clipped" if night else "")
            + ("+decomposed" if (source is not None and step.value("dni") is None) else ""),
            license=base.license,
            source_url=base.source_url,
        )
        out.append(
            IrradianceComponents(
                time_utc=position.time_utc,
                point=point,
                ghi_w_m2=max(0.0, ghi),
                dni_w_m2=max(0.0, dni),
                dhi_w_m2=max(0.0, dhi),
                kind=base.kind,
                provenance=provenance,
                solar_elevation_deg=position.apparent_elevation_deg,
            )
        )
    return out


def power_estimates_from_forecast(
    *,
    series: WeatherSeries,
    point: GeoPoint,
    heading_deg: float | None,
    array: Any | None = None,
) -> tuple[list[Any], list[str]]:
    """Build a PV power series from a weather forecast at one point.

    Returns the estimates and a list of human-readable notes about anything that
    had to be skipped or assumed - the API passes those notes to the UI rather
    than hiding them.
    """
    power_series: Any = require_attr("rwi.energy.series", "power_series", capability="PV model")
    default_array: Any = require_attr("rwi.energy.array", "DEFAULT_ARRAY", capability="PV model")
    chosen = array if array is not None else default_array

    times = [step.valid_time_utc for step in series.steps]
    if not times:
        return [], ["weather series contained no steps"]
    solar = solar_positions_for(point, times)
    irradiance = irradiance_from_series(series, solar, point=point)

    notes: list[str] = []
    samples: list[tuple[Any, ...]] = []
    skipped = 0
    for position, components in zip(solar, irradiance, strict=True):
        if components is None:
            skipped += 1
            continue
        step = series.nearest_step(position.time_utc, tolerance_s=1800.0)
        air = step.value("temperature_2m") if step else None
        wind = step.value("wind_speed_10m") if step else None
        if air is None:
            notes.append("air temperature missing: PV model used its documented assumption")
        if wind is None:
            notes.append("wind speed missing: PV model used its documented assumption")
        samples.append((position.time_utc, position, components, air, wind, heading_deg))
    if skipped:
        notes.append(f"{skipped} step(s) skipped: provider gave no usable irradiance")
    if not samples:
        return [], notes or ["no step had usable irradiance"]
    estimates = power_series(samples, chosen)
    return list(estimates), sorted(set(notes))


def energy_from_estimates(estimates: list[Any]) -> Any | None:
    if len(estimates) < 2:
        return None
    integrate: Any = require_attr(
        "rwi.energy.series", "integrate_energy", capability="energy integration"
    )
    return integrate(estimates)


def now_utc() -> datetime:
    return utc_now()


ROUTE_WEATHER_VARIABLES = (
    "temperature_2m",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "cloud_cover",
    "precipitation",
    "precipitation_probability",
    "pressure_msl",
    "ghi",
    "dni",
    "dhi",
)
MAX_TRAJECTORY_SAMPLES = 12
"""Upper bound on provider calls per request, to respect provider rate limits."""


async def trajectory_weather(
    trajectory: Any,
    providers: dict[str, Any],
    *,
    variables: Sequence[str] = ROUTE_WEATHER_VARIABLES,
    tolerance_s: float = 5400.0,
) -> tuple[list[Any | None], list[str]]:
    """Forecast at each trajectory point, valid at that point's own ETA.

    Returns one entry per trajectory point, aligned 1:1 and ``None`` where the
    provider gave nothing usable, plus notes naming every gap. Alignment is what
    lets the strategy engine use these as per-segment conditions; silently
    dropping a point would shift the whole segment series.
    """
    from rwi.contracts import RouteWeatherSample
    from rwi.core.timebase import TimeWindow

    fetch_with_fallback: Any = require_attr(
        "rwi.providers.registry", "fetch_with_fallback", capability="weather providers"
    )
    notes: list[str] = []

    async def _one(point: Any) -> Any | None:
        window = TimeWindow(
            start_utc=min(utc_now(), point.eta_utc - timedelta(hours=1)),
            end_utc=point.eta_utc + timedelta(hours=1),
        )
        try:
            series = await fetch_with_fallback(
                providers, point.point, window, variables, kind=DataKind.FORECAST
            )
        except Exception as exc:
            notes.append(f"{point.distance_m / 1000:.0f} km: {type(exc).__name__}: {exc}")
            return None
        step = series.nearest_step(point.eta_utc, tolerance_s=tolerance_s)
        if step is None:
            notes.append(f"{point.distance_m / 1000:.0f} km: no forecast step near the ETA")
            return None
        wind_speed = step.value("wind_speed_10m")
        wind_from = step.value("wind_direction_10m")
        rel = None
        if wind_speed is not None and wind_from is not None:
            rel = relative_wind(
                vehicle_speed_mps=point.speed_mps,
                vehicle_heading_deg=point.heading_deg,
                wind_speed_mps=wind_speed,
                wind_from_deg=wind_from,
            )
        base = series.base_provenance()
        return RouteWeatherSample(
            trajectory_point=point,
            series_point=series.point,
            values=dict(step.values),
            lead_time_s=(point.eta_utc - base.retrieved_at_utc).total_seconds(),
            relative_wind=rel,
        )

    results = await asyncio.gather(*(_one(p) for p in trajectory.points))
    return list(results), sorted(set(notes))


def power_estimates_along_trajectory(
    trajectory: Any,
    samples: Sequence[Any | None],
    *,
    array: Any | None = None,
) -> tuple[list[Any], list[str]]:
    """PV power at each trajectory point, from that point's own forecast.

    The solar geometry uses each point's own coordinates and arrival time, so
    the generation estimate follows the vehicle rather than a fixed location.
    """
    power_series: Any = require_attr("rwi.energy.series", "power_series", capability="PV model")
    default_array: Any = require_attr("rwi.energy.array", "DEFAULT_ARRAY", capability="PV model")
    chosen = array if array is not None else default_array

    notes: list[str] = []
    inputs: list[tuple[Any, ...]] = []
    for point, sample in zip(trajectory.points, samples, strict=True):
        solar = solar_positions_for(point.point, [point.eta_utc])[0]
        if sample is None:
            notes.append(f"{point.distance_m / 1000:.0f} km: no forecast, generation unknown")
            continue
        ghi = sample.values.get("ghi")
        if ghi is None or ghi.value is None:
            notes.append(f"{point.distance_m / 1000:.0f} km: provider gave no GHI")
            continue
        series_like = _single_step_series(sample, point)
        components = irradiance_from_series(series_like, [solar], point=point.point)[0]
        if components is None:
            notes.append(f"{point.distance_m / 1000:.0f} km: irradiance unusable")
            continue
        air = sample.values.get("temperature_2m")
        wind = sample.values.get("wind_speed_10m")
        inputs.append(
            (
                point.eta_utc,
                solar,
                components,
                air.value if air is not None else None,
                wind.value if wind is not None else None,
                point.heading_deg,
            )
        )
    if not inputs:
        return [], notes or ["no trajectory point had usable irradiance"]
    return list(power_series(inputs, chosen)), sorted(set(notes))


def _single_step_series(sample: Any, point: Any) -> Any:
    """Wrap one RouteWeatherSample as a one-step WeatherSeries.

    Reuses :func:`irradiance_from_series`, so the night-clipping and
    decomposition rules apply identically on the route path and the
    single-point path. One rule, one implementation.
    """
    from rwi.contracts import WeatherSeries, WeatherStep

    first = next(iter(sample.values.values()))
    base = first.provenance
    return WeatherSeries(
        point=sample.series_point,
        requested_point=point.point,
        provider=base.provider,
        dataset=base.dataset,
        kind=base.kind,
        model_name=base.model_name,
        model_version=base.model_version,
        model_init_time_utc=base.model_init_time_utc,
        retrieved_at_utc=base.retrieved_at_utc,
        spatial_resolution_km=base.spatial_resolution_km,
        temporal_resolution_s=base.temporal_resolution_s,
        license=base.license,
        source_url=base.source_url,
        steps=[WeatherStep(valid_time_utc=point.eta_utc, values=dict(sample.values))],
    )
