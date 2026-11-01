"""Solar geometry and clear-sky irradiance endpoints.

Every response is explicitly labelled as physical-model output. A clear-sky
value is an upper bound under a cloudless sky, not a measurement.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from rwi.api.capability import require_attr
from rwi.api.deps import LatQuery, LonQuery, resolve_point, resolve_time
from rwi.contracts import IrradianceComponents, SolarDayEvents, SolarPosition
from rwi.core.state import StateStore, get_state_store
from rwi.core.timebase import timezone_for_point

router = APIRouter(prefix="/api/solar", tags=["solar"])

StoreDep = Annotated[StateStore, Depends(get_state_store)]


@router.get("/position", response_model=SolarPosition)
async def read_solar_position(
    store: StoreDep,
    lat: Annotated[float | None, LatQuery] = None,
    lon: Annotated[float | None, LonQuery] = None,
    elev: Annotated[float | None, Query(description="metres above sea level")] = None,
    time: Annotated[
        datetime | None, Query(description="UTC instant; default = the active time")
    ] = None,
) -> Any:
    solar_position = require_attr(
        "rwi.solar.geometry", "solar_position", capability="solar geometry"
    )
    point = resolve_point(lat, lon, elev, store)
    return solar_position(point, resolve_time(time, store))


@router.get("/day", response_model=SolarDayEvents)
async def read_solar_day(
    store: StoreDep,
    lat: Annotated[float | None, LatQuery] = None,
    lon: Annotated[float | None, LonQuery] = None,
    elev: Annotated[float | None, Query()] = None,
    time: Annotated[datetime | None, Query(description="any instant in the local day")] = None,
) -> Any:
    solar_day_events = require_attr(
        "rwi.solar.geometry", "solar_day_events", capability="solar day events"
    )
    point = resolve_point(lat, lon, elev, store)
    return solar_day_events(point, resolve_time(time, store))


@router.get("/clearsky", response_model=IrradianceComponents)
async def read_clear_sky(
    store: StoreDep,
    lat: Annotated[float | None, LatQuery] = None,
    lon: Annotated[float | None, LonQuery] = None,
    elev: Annotated[float | None, Query()] = None,
    time: Annotated[datetime | None, Query()] = None,
) -> Any:
    clear_sky_irradiance = require_attr(
        "rwi.solar.clearsky", "clear_sky_irradiance", capability="clear-sky irradiance"
    )
    point = resolve_point(lat, lon, elev, store)
    return clear_sky_irradiance(point, resolve_time(time, store))


@router.get("/clearsky/day", response_model=list[IrradianceComponents])
async def read_clear_sky_day(
    store: StoreDep,
    lat: Annotated[float | None, LatQuery] = None,
    lon: Annotated[float | None, LonQuery] = None,
    elev: Annotated[float | None, Query()] = None,
    time: Annotated[datetime | None, Query()] = None,
    step_s: Annotated[int, Query(ge=60, le=10800)] = 1800,
) -> Any:
    clear_sky_series = require_attr(
        "rwi.solar.clearsky", "clear_sky_series", capability="clear-sky irradiance"
    )
    point = resolve_point(lat, lon, elev, store)
    reference = resolve_time(time, store)
    local_midnight = reference.astimezone(timezone_for_point(point)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start = local_midnight.astimezone(reference.tzinfo)
    times = [start + timedelta(seconds=step_s * i) for i in range(int(86400 / step_s) + 1)]
    return clear_sky_series(point, times)
