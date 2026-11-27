"""Camp-site charging forecast for candidate overnight stops (ADR 0015).

INTERNAL tier: no prefix in `rwi.api.product` matches ``/api/camp``. The route
evaluates each candidate distance the caller gives; it never chooses a stop.
"""

import math
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from rwi.api.deps import RouteRepository, get_route_repository
from rwi.config import Settings, get_settings
from rwi.contracts import GeoPoint, Route, WeatherProvider, WeatherSeries
from rwi.core.errors import ProviderError
from rwi.core.timebase import TimeWindow, utc_now
from rwi.energy.camp import (
    CampForecast,
    camp_forecast,
    horizon_points,
    sun_azimuth_deg,
    sun_events,
)
from rwi.providers import registry
from rwi.regulations import EVENT_TIMEZONE, SCHEDULE
from rwi.route.geometry import point_at_distance

router = APIRouter(prefix="/api/camp", tags=["camp"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
RepoDep = Annotated[RouteRepository, Depends(get_route_repository)]

MAX_CANDIDATES = 6
_STOP_VARS = (
    "ghi",
    "dni",
    "wind_gusts_10m",
    "temperature_2m",
    "dew_point_2m",
    "wind_speed_10m",
    "cloud_cover",
)
_CLOUD_VARS = ("cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high")
_HOUR = timedelta(hours=1)


class CampCandidate(BaseModel):
    """One candidate stop: the outlook, or why it could not be computed."""

    model_config = ConfigDict(frozen=True)

    distance_km: float
    forecast_available: bool = Field(description="False: clear-sky only, see message")
    message: str | None = None
    camp: CampForecast


class CampResponse(BaseModel):
    """Camp charging outlook for every requested candidate distance."""

    model_config = ConfigDict(frozen=True)

    route_id: str
    stop_date_event: date
    candidates: list[CampCandidate]
    notes: list[str] = Field(default_factory=list)


def _route(repo: RouteRepository, route_id: str | None) -> Route:
    if route_id:
        return repo.get(route_id)
    routes = list(repo.all().values())
    if len(routes) != 1:
        raise HTTPException(422, f"route_id is required: {len(routes)} routes are loaded")
    return routes[0]


async def _fetch(
    provider: WeatherProvider,
    point: GeoPoint,
    start: datetime,
    end: datetime,
    variables: tuple[str, ...],
) -> tuple[WeatherSeries | None, str | None]:
    try:
        series = await provider.fetch_forecast(
            point, TimeWindow(start_utc=start, end_utc=end), list(variables)
        )
    except ProviderError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return series, None


async def _candidate(
    provider: WeatherProvider | None,
    settings: Settings,
    route: Route,
    distance_km: float,
    stop_date: date,
) -> CampCandidate:
    point, _ = point_at_distance(route, distance_km * 1000.0)
    sunset, sunrise = sun_events(point, stop_date)
    ev_start = datetime.combine(stop_date, SCHEDULE.daily_stop, tzinfo=EVENT_TIMEZONE).astimezone(
        UTC
    )
    mo_end = datetime.combine(
        stop_date + timedelta(days=1), SCHEDULE.daily_start, tzinfo=EVENT_TIMEZONE
    ).astimezone(UTC)
    offsets = settings.camp_horizon_offsets_km or []
    messages: list[str] = []
    stop_series = None
    west: list[tuple[float, GeoPoint, WeatherSeries | None]] = []
    east: list[tuple[float, GeoPoint, WeatherSeries | None]] = []
    hour_mean = getattr(provider, "radiation_mode", None) == "preceding_hour_mean"
    if provider is None:
        messages.append("open-meteo provider unavailable: clear-sky only")
    else:
        # TimeWindow is [start, end): +1 h keeps the step at daily_start (covers the hour before).
        stop_series, err = await _fetch(
            provider, point, ev_start - _HOUR, mo_end + _HOUR, _STOP_VARS
        )
        if err:
            messages.append(f"stop forecast unavailable ({err}): clear-sky only")
        for event, side_list in ((sunset, west), (sunrise, east)):
            if event is None:
                continue
            az = sun_azimuth_deg(point, event)
            for d, p in horizon_points(point, az, offsets):
                s, err = await _fetch(provider, p, event - _HOUR, event + _HOUR, _CLOUD_VARS)
                if err:
                    messages.append(f"horizon cloud at {d:g} km unavailable ({err})")
                side_list.append((d, p, s))
    camp = camp_forecast(
        point,
        stop_date,
        now=utc_now(),
        stop_series=stop_series,
        horizon_west=west,
        horizon_east=east,
        gust_warning_m_s=settings.camp_gust_warning_m_s,
        dew_margin_c=settings.camp_dew_margin_c,
        forecast_is_hour_mean=hour_mean,
        route_id=route.id,
        distance_along_m=distance_km * 1000.0,
    )
    return CampCandidate(
        distance_km=distance_km,
        forecast_available=stop_series is not None,
        message="; ".join(messages) or None,
        camp=camp,
    )


@router.get("/forecast", response_model=CampResponse)
async def read_camp_forecast(
    settings: SettingsDep,
    repo: RepoDep,
    stop_date: Annotated[date, Query(alias="date", description="event-time (ACST) date")],
    distance_km: Annotated[list[float], Query(description="candidate stop distances, repeatable")],
    route_id: Annotated[str | None, Query()] = None,
) -> CampResponse:
    """Charging windows, low-sun energy, horizon cloud, gusts and dew per candidate stop."""
    route = _route(repo, route_id)
    if not 1 <= len(distance_km) <= MAX_CANDIDATES:
        raise HTTPException(422, f"give 1 to {MAX_CANDIDATES} distance_km values")
    total_km = route.total_distance_m / 1000.0
    for d in distance_km:
        if not math.isfinite(d) or not 0.0 <= d <= total_km:
            raise HTTPException(422, f"distance_km {d!r} is outside the route (0..{total_km:.1f})")
    notes = []
    if settings.camp_gust_warning_m_s is None:
        notes.append("RWI_CAMP_GUST_WARNING_M_S not set: no gust warning flags")
    if settings.camp_dew_margin_c is None:
        notes.append("RWI_CAMP_DEW_MARGIN_C not set: no dew risk flags")
    if not settings.camp_horizon_offsets_km:
        notes.append("RWI_CAMP_HORIZON_OFFSETS_KM not set: no horizon cloud")
    providers = registry.available_providers(settings)
    try:
        provider = providers.get("open-meteo")
        candidates = [
            await _candidate(provider, settings, route, d, stop_date) for d in distance_km
        ]
    finally:
        await registry.close_providers(providers)
    return CampResponse(
        route_id=route.id, stop_date_event=stop_date, candidates=candidates, notes=notes
    )
