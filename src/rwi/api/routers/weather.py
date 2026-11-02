"""Weather endpoints. Observations and forecasts are separate by construction."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from rwi.api.capability import require_attr
from rwi.api.deps import LatQuery, LonQuery, resolve_point
from rwi.config import Settings, get_settings
from rwi.contracts import ProviderCapabilities, WeatherSeries
from rwi.core.provenance import DataKind
from rwi.core.state import StateStore, get_state_store
from rwi.core.timebase import TimeWindow, utc_now

router = APIRouter(prefix="/api/weather", tags=["weather"])

StoreDep = Annotated[StateStore, Depends(get_state_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

DEFAULT_VARIABLES = (
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "pressure_msl",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "cloud_cover",
    "precipitation",
    "precipitation_probability",
    "ghi",
    "dni",
    "dhi",
    "visibility",
)


def _variables(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return DEFAULT_VARIABLES
    return tuple(name.strip() for name in raw.split(",") if name.strip())


async def _fetch(
    *,
    store: StateStore,
    settings: Settings,
    lat: float | None,
    lon: float | None,
    elev: float | None,
    hours: float,
    variables: str | None,
    provider: str | None,
    kind: DataKind,
) -> Any:
    available = require_attr(
        "rwi.providers.registry", "available_providers", capability="weather providers"
    )
    fetch_with_fallback = require_attr(
        "rwi.providers.registry", "fetch_with_fallback", capability="weather providers"
    )
    providers = available(settings)
    if provider:
        providers = {name: p for name, p in providers.items() if name == provider}
    point = resolve_point(lat, lon, elev, store)
    now = utc_now()
    window = (
        TimeWindow(start_utc=now, end_utc=now + timedelta(hours=hours))
        if kind is DataKind.FORECAST
        else TimeWindow(start_utc=now - timedelta(hours=hours), end_utc=now)
    )
    series = await fetch_with_fallback(
        providers, point, window, _variables(variables), kind=kind
    )
    check_series = None
    try:
        check_series = require_attr("rwi.quality", "check_series", capability="quality control")
    except Exception:
        check_series = None
    if check_series is not None:
        series = check_series(series, now=now)
    return series


@router.get("/forecast", response_model=WeatherSeries)
async def read_forecast(
    store: StoreDep,
    settings: SettingsDep,
    lat: Annotated[float | None, LatQuery] = None,
    lon: Annotated[float | None, LonQuery] = None,
    elev: Annotated[float | None, Query()] = None,
    hours: Annotated[float, Query(gt=0.0, le=384.0)] = 48.0,
    variables: Annotated[str | None, Query(description="comma-separated canonical names")] = None,
    provider: Annotated[str | None, Query()] = None,
) -> Any:
    return await _fetch(
        store=store,
        settings=settings,
        lat=lat,
        lon=lon,
        elev=elev,
        hours=hours,
        variables=variables,
        provider=provider,
        kind=DataKind.FORECAST,
    )


@router.get("/observations", response_model=WeatherSeries)
async def read_observations(
    store: StoreDep,
    settings: SettingsDep,
    lat: Annotated[float | None, LatQuery] = None,
    lon: Annotated[float | None, LonQuery] = None,
    elev: Annotated[float | None, Query()] = None,
    hours: Annotated[float, Query(gt=0.0, le=168.0)] = 6.0,
    variables: Annotated[str | None, Query()] = None,
    provider: Annotated[str | None, Query()] = None,
) -> Any:
    """Measured values only. Returns 501 when no configured provider measures them -
    a forecast is never returned in their place."""
    return await _fetch(
        store=store,
        settings=settings,
        lat=lat,
        lon=lon,
        elev=elev,
        hours=hours,
        variables=variables,
        provider=provider,
        kind=DataKind.OBSERVATION,
    )


@router.get("/providers", response_model=list[ProviderCapabilities])
async def read_provider_capabilities(settings: SettingsDep) -> Any:
    """What each configured provider can actually deliver.

    The dashboard needs this to build an observation request that can succeed:
    only a provider with `supports_observations` and the variable in its
    `variables` set will ever return a measurement, and asking for more produces
    a 501 rather than a quiet substitution.
    """
    available: Any = require_attr(
        "rwi.providers.registry", "available_providers", capability="weather providers"
    )
    return [provider.capabilities for provider in available(settings).values()]


@router.get("/observable")
async def read_observable_variables(settings: SettingsDep) -> dict[str, list[str]]:
    """Canonical variables that at least one provider MEASURES, per provider.

    An empty list for every provider is a truthful answer meaning the platform
    currently measures nothing.
    """
    available: Any = require_attr(
        "rwi.providers.registry", "available_providers", capability="weather providers"
    )
    out: dict[str, list[str]] = {}
    for name, provider in available(settings).items():
        capabilities = provider.capabilities
        if capabilities.supports_observations:
            out[name] = sorted(capabilities.variables)
    return out


@router.get("/variables")
async def read_variables() -> dict[str, dict[str, object]]:
    """The canonical variable registry, so the UI never hardcodes a unit."""
    from rwi.core.variables import CANONICAL_VARIABLES

    return {
        name: {
            "unit": spec.unit,
            "minimum": spec.minimum,
            "maximum": spec.maximum,
            "class": spec.variable_class.value,
            "description": spec.description,
        }
        for name, spec in CANONICAL_VARIABLES.items()
    }
