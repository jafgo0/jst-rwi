"""Route intelligence: geometry, progress, ETA trajectory and weather on arrival.

The weather endpoint here is the platform's distinctive capability: it does not
report the weather where the car is now, but the weather where the car will be
when it gets there - Weather = f(lat, lon, elevation, future time).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from rwi.api.capability import require_attr
from rwi.api.deps import RouteRepository, get_route_repository
from rwi.api.integration import (
    MAX_TRAJECTORY_SAMPLES,
    ROUTE_WEATHER_VARIABLES,
    trajectory_weather,
)
from rwi.config import Settings, get_settings
from rwi.contracts import (
    Route,
    RouteProjection,
    RouteWeatherForecast,
    Trajectory,
)
from rwi.core.state import StateStore, get_state_store
from rwi.core.timebase import to_local, utc_now
from rwi.core.units import kmh_to_ms

router = APIRouter(prefix="/api/route", tags=["route"])

StoreDep = Annotated[StateStore, Depends(get_state_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
RepoDep = Annotated[RouteRepository, Depends(get_route_repository)]



class RouteSummary(BaseModel):
    id: str
    name: str
    total_distance_m: float
    waypoint_count: int
    control_stop_count: int
    source_description: str
    provenance_label: str


class ProgressResponse(BaseModel):
    route_id: str
    projection: RouteProjection
    distance_remaining_m: float
    next_waypoint_name: str | None
    next_waypoint_distance_m: float | None
    next_control_stop_name: str | None
    next_control_stop_distance_m: float | None


@router.get("", response_model=list[RouteSummary])
async def list_routes(repo: RepoDep) -> Any:
    return [
        RouteSummary(
            id=route_id,
            name=route.name,
            total_distance_m=route.total_distance_m,
            waypoint_count=len(route.waypoints),
            control_stop_count=len(route.control_stops),
            source_description=route.source_description,
            provenance_label=route.provenance.label(),
        )
        for route_id, route in repo.all().items()
    ]


@router.get("/{route_id}", response_model=Route)
async def read_route(route_id: str, repo: RepoDep) -> Any:
    return repo.get(route_id)


@router.get("/{route_id}/geojson")
async def read_route_geojson(route_id: str, repo: RepoDep) -> dict[str, Any]:
    """The route as GeoJSON for the map: one LineString plus waypoint points."""
    route = repo.get(route_id)
    line = {
        "type": "Feature",
        "properties": {
            "route_id": route_id,
            "name": route.name,
            "total_distance_m": route.total_distance_m,
            "source": route.source_description,
        },
        "geometry": {
            "type": "LineString",
            "coordinates": [
                [w.point.longitude_deg, w.point.latitude_deg]
                + ([w.point.elevation_m] if w.point.elevation_m is not None else [])
                for w in route.waypoints
            ],
        },
    }
    markers = [
        {
            "type": "Feature",
            "properties": {
                "name": w.name,
                "kind": w.kind.value,
                "cumulative_distance_m": w.cumulative_distance_m,
                "mandatory_dwell_s": w.mandatory_dwell_s,
            },
            "geometry": {
                "type": "Point",
                "coordinates": [w.point.longitude_deg, w.point.latitude_deg],
            },
        }
        for w in route.waypoints
        if w.kind.value != "waypoint"
    ]
    return {"type": "FeatureCollection", "features": [line, *markers]}


@router.get("/{route_id}/progress", response_model=ProgressResponse)
async def read_progress(route_id: str, repo: RepoDep, store: StoreDep) -> Any:
    project: Any = require_attr(
        "rwi.route.geometry", "project_onto_route", capability="route projection"
    )
    route = repo.get(route_id)
    fix = store.require_fix()
    projection = project(route, fix.point)
    ahead = [w for w in route.waypoints if w.cumulative_distance_m > projection.distance_along_m]
    next_named = next((w for w in ahead if w.name), None)
    next_stop = next((w for w in ahead if w.kind.value == "control_stop"), None)
    return ProgressResponse(
        route_id=route_id,
        projection=projection,
        distance_remaining_m=max(0.0, route.total_distance_m - projection.distance_along_m),
        next_waypoint_name=next_named.name if next_named else None,
        next_waypoint_distance_m=(
            next_named.cumulative_distance_m - projection.distance_along_m if next_named else None
        ),
        next_control_stop_name=next_stop.name if next_stop else None,
        next_control_stop_distance_m=(
            next_stop.cumulative_distance_m - projection.distance_along_m if next_stop else None
        ),
    )


def _build_trajectory(
    route: Route,
    *,
    speed_kmh: float,
    start_distance_m: float | None,
    hours: float,
    store: StateStore,
) -> Any:
    build: Any = require_attr(
        "rwi.route.trajectory", "build_trajectory", capability="trajectory projection"
    )
    start = start_distance_m
    if start is None:
        state = store.state
        if state.route_distance_m is not None:
            start = state.route_distance_m
        elif state.fix is not None:
            project: Any = require_attr(
                "rwi.route.geometry", "project_onto_route", capability="route projection"
            )
            start = project(route, state.fix.point).distance_along_m
        else:
            start = 0.0
    speed_mps = kmh_to_ms(speed_kmh)
    spacing = max(5_000.0, speed_mps * 3600.0)
    return build(
        route,
        start_distance_m=max(0.0, min(start, route.total_distance_m)),
        start_time_utc=store.state.effective_time(utc_now()),
        speed_mps=speed_mps,
        sample_spacing_m=spacing,
    )


@router.get("/{route_id}/trajectory", response_model=Trajectory)
async def read_trajectory(
    route_id: str,
    repo: RepoDep,
    store: StoreDep,
    speed_kmh: Annotated[float, Query(gt=0.0, le=130.0)] = 80.0,
    start_distance_m: Annotated[float | None, Query(ge=0.0)] = None,
    hours: Annotated[float, Query(gt=0.0, le=240.0)] = 24.0,
) -> Any:
    route = repo.get(route_id)
    return _build_trajectory(
        route, speed_kmh=speed_kmh, start_distance_m=start_distance_m, hours=hours, store=store
    )


class RouteWeatherResponse(BaseModel):
    forecast: RouteWeatherForecast
    local_arrival_times: list[str]
    notes: list[str] = Field(default_factory=list)


@router.get("/{route_id}/weather", response_model=RouteWeatherResponse)
async def read_route_weather(
    route_id: str,
    repo: RepoDep,
    store: StoreDep,
    settings: SettingsDep,
    speed_kmh: Annotated[float, Query(gt=0.0, le=130.0)] = 80.0,
    start_distance_m: Annotated[float | None, Query(ge=0.0)] = None,
    hours: Annotated[float, Query(gt=0.0, le=240.0)] = 24.0,
) -> Any:
    """Forecast weather at each predicted position, valid at its predicted ETA.

    Sampling is shared with the strategy engine
    (:func:`rwi.api.integration.trajectory_weather`), so the two views can never
    disagree about what the forecast says along the route.
    """
    available: Any = require_attr(
        "rwi.providers.registry", "available_providers", capability="weather providers"
    )
    route = repo.get(route_id)
    trajectory = _build_trajectory(
        route, speed_kmh=speed_kmh, start_distance_m=start_distance_m, hours=hours, store=store
    )
    now = utc_now()
    horizon = now + timedelta(hours=hours)
    points = [p for p in trajectory.points if p.eta_utc <= horizon]
    if len(points) > MAX_TRAJECTORY_SAMPLES:
        stride = max(1, len(points) // MAX_TRAJECTORY_SAMPLES)
        points = points[::stride][:MAX_TRAJECTORY_SAMPLES]
    trajectory = trajectory.model_copy(update={"points": points})

    providers = available(settings)
    samples, notes = await trajectory_weather(
        trajectory, providers, variables=ROUTE_WEATHER_VARIABLES
    )
    usable = [s for s in samples if s is not None]
    forecast = RouteWeatherForecast(
        route_id=route_id,
        generated_at_utc=now,
        samples=usable,
        provider=",".join(sorted(providers)) or "none",
        assumptions={
            "speed_kmh": speed_kmh,
            "sampled_points": len(usable),
            "requested_points": len(samples),
            "max_samples": MAX_TRAJECTORY_SAMPLES,
            "trajectory_assumptions": str(trajectory.assumptions),
        },
    )
    return RouteWeatherResponse(
        forecast=forecast,
        local_arrival_times=[
            to_local(s.trajectory_point.eta_utc, s.trajectory_point.point).isoformat()
            for s in usable
        ],
        notes=notes,
    )
