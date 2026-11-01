"""Race strategy endpoints.

What this is: decision support. The platform evaluates candidate driving
scenarios against the forecast, the vehicle model, the battery reserve and the
regulations, and explains the trade-offs.

What this is not: a controller. No endpoint here commands the vehicle, and every
recommendation states its assumptions, its uncertainty and the clauses that
constrain it. A scenario with a blocking regulatory violation can never be
recommended.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from rwi.alerts import sort_alerts, strategy_alerts
from rwi.api.capability import require_attr
from rwi.api.deps import RouteRepository, get_route_repository
from rwi.api.integration import (
    MAX_TRAJECTORY_SAMPLES,
    power_estimates_along_trajectory,
    trajectory_weather,
)
from rwi.config import Settings, get_settings
from rwi.contracts import Alert, Recommendation, ScenarioResult, VehicleConfig
from rwi.core.state import StateStore, get_state_store
from rwi.core.timebase import utc_now
from rwi.core.units import kmh_to_ms
from rwi.regulations import EventClass, check_energy_storage

router = APIRouter(prefix="/api/strategy", tags=["strategy"])

StoreDep = Annotated[StateStore, Depends(get_state_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
RepoDep = Annotated[RouteRepository, Depends(get_route_repository)]


class ScenarioResponse(BaseModel):
    """Scenario comparison plus everything a strategist needs to judge it."""

    scenarios: list[ScenarioResult]
    recommendation: Recommendation
    optimal_constant_speed_kmh: float | None
    optimal_is_feasible: bool | None
    vehicle: VehicleConfig
    alerts: list[Alert]
    assumptions: dict[str, Any] = Field(default_factory=dict)
    data_gaps: list[str] = Field(default_factory=list)
    regulatory_notes: list[str] = Field(default_factory=list)
    human_review_required: bool = True


@router.get("/vehicle", response_model=VehicleConfig)
async def read_vehicle() -> Any:
    """The vehicle parameters in use. `parameter_source` says whether they are
    measured; the placeholder configuration must never be quoted as the team's
    car."""
    return require_attr("rwi.vehicle.dynamics", "DEFAULT_VEHICLE", capability="vehicle model")


@router.get("/scenarios", response_model=ScenarioResponse)
async def read_scenarios(
    store: StoreDep,
    settings: SettingsDep,
    repo: RepoDep,
    route_id: Annotated[str | None, Query(description="default: the active route")] = None,
    speed_kmh: Annotated[float, Query(gt=0.0, le=130.0)] = 80.0,
    start_soc: Annotated[float, Query(ge=0.0, le=1.0)] = 0.8,
    hours: Annotated[float, Query(gt=0.0, le=72.0)] = 10.0,
    start_distance_m: Annotated[float | None, Query(ge=0.0)] = None,
    posted_limit_kmh: Annotated[
        float | None, Query(gt=0.0, description="posted limit of the segments ahead, if known")
    ] = None,
    uncertainty: Annotated[
        float, Query(ge=0.0, le=0.9, description="fractional irradiance/wind uncertainty")
    ] = 0.2,
) -> Any:
    build_trajectory: Any = require_attr(
        "rwi.route.trajectory", "build_trajectory", capability="trajectory projection"
    )
    project: Any = require_attr(
        "rwi.route.geometry", "project_onto_route", capability="route projection"
    )
    segments_from_trajectory: Any = require_attr(
        "rwi.strategy.segments", "segments_from_trajectory", capability="strategy engine"
    )
    standard_candidates: Any = require_attr(
        "rwi.strategy.scenarios", "standard_candidates", capability="strategy engine"
    )
    compare_scenarios: Any = require_attr(
        "rwi.strategy.scenarios", "compare_scenarios", capability="strategy engine"
    )
    recommend: Any = require_attr(
        "rwi.strategy.scenarios", "recommend", capability="strategy engine"
    )
    optimal_constant_speed: Any = require_attr(
        "rwi.strategy.scenarios", "optimal_constant_speed", capability="strategy engine"
    )
    vehicle: Any = require_attr(
        "rwi.vehicle.dynamics", "DEFAULT_VEHICLE", capability="vehicle model"
    )
    available: Any = require_attr(
        "rwi.providers.registry", "available_providers", capability="weather providers"
    )

    state = store.state
    chosen_id = route_id or state.route_id
    if chosen_id is None:
        routes = repo.all()
        if not routes:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="no route is loaded"
            )
        chosen_id = next(iter(routes))
    route = repo.get(chosen_id)

    start = start_distance_m
    if start is None:
        if state.route_distance_m is not None:
            start = state.route_distance_m
        elif state.fix is not None:
            start = project(route, state.fix.point).distance_along_m
        else:
            start = 0.0
    start = max(0.0, min(start, route.total_distance_m))

    speed_mps = kmh_to_ms(speed_kmh)
    # Spacing is chosen so that every trajectory point gets its own forecast and
    # the point and segment series stay aligned one to one.
    reachable_m = min(speed_mps * hours * 3600.0, route.total_distance_m - start)
    if reachable_m <= 0.0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="the vehicle is already at or past the end of this route",
        )
    spacing = max(1000.0, reachable_m / (MAX_TRAJECTORY_SAMPLES - 1))
    now = utc_now()
    trajectory = build_trajectory(
        route,
        start_distance_m=start,
        start_time_utc=state.effective_time(now),
        speed_mps=speed_mps,
        sample_spacing_m=spacing,
    )
    points = [p for p in trajectory.points if p.distance_m <= start + reachable_m]
    if len(points) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="the requested horizon covers less than one route segment",
        )
    trajectory = trajectory.model_copy(update={"points": points})

    samples, weather_notes = await trajectory_weather(trajectory, available(settings))
    estimates, generation_notes = power_estimates_along_trajectory(trajectory, samples)
    gaps = [*weather_notes, *generation_notes]

    def _series(variable: str) -> list[float] | None:
        values: list[float] = []
        for sample in samples:
            if sample is None:
                return None
            item = sample.values.get(variable)
            if item is None or item.value is None:
                return None
            values.append(item.value)
        return values

    wind_speed = _series("wind_speed_10m")
    wind_from = _series("wind_direction_10m")
    temperature = _series("temperature_2m")
    pressure = _series("pressure_msl")
    if wind_speed is None or wind_from is None:
        gaps.append("wind forecast incomplete along the horizon: wind effects not applied")

    segments = segments_from_trajectory(
        trajectory,
        generation=estimates or None,
        wind_speed_mps=wind_speed,
        wind_from_deg=wind_from,
        temperature_c=temperature,
        pressure_pa=pressure,
        speed_limit_kmh=posted_limit_kmh,
    )

    specs = standard_candidates(
        vehicle=vehicle,
        segments=segments,
        baseline_speed_kmh=speed_kmh,
        speed_limit_kmh=posted_limit_kmh,
    )
    scenarios = compare_scenarios(
        specs,
        vehicle=vehicle,
        start_soc_fraction=start_soc,
        segments=segments,
        now_utc=now,
    )
    recommendation = recommend(scenarios, now_utc=now)

    optimum = None
    try:
        optimum = optimal_constant_speed(
            vehicle=vehicle,
            start_soc_fraction=start_soc,
            segments=segments,
            speed_limit_kmh=posted_limit_kmh,
            now_utc=now,
        )
    except Exception as exc:
        gaps.append(f"constant-speed optimiser unavailable: {type(exc).__name__}: {exc}")

    regulatory: list[str] = []
    storage_finding = check_energy_storage(vehicle.battery_capacity_wh, EventClass.CHALLENGER)
    if storage_finding is not None:
        regulatory.append(f"clause {storage_finding.clause}: {storage_finding.detail}")
    if posted_limit_kmh is None:
        regulatory.append(
            "posted speed limits for the segments ahead are unknown, so no upper speed "
            "bound was enforced beyond the vehicle ceiling; the 2027 regulations do not "
            "publish per-segment limits (see /api/regulations)"
        )
    if vehicle.parameter_source != "measured":
        regulatory.append(
            f"vehicle parameters are '{vehicle.parameter_source}', not measured: these "
            "results are illustrative and must not be quoted as predictions for the car"
        )

    alerts = sort_alerts(strategy_alerts(list(scenarios), now=now))

    return ScenarioResponse(
        scenarios=list(scenarios),
        recommendation=recommendation,
        optimal_constant_speed_kmh=getattr(optimum, "speed_kmh", None) if optimum else None,
        optimal_is_feasible=getattr(optimum, "feasible", None) if optimum else None,
        vehicle=vehicle,
        alerts=alerts,
        assumptions={
            "route_id": chosen_id,
            "start_distance_m": start,
            "baseline_speed_kmh": speed_kmh,
            "horizon_hours": hours,
            "start_soc_fraction": start_soc,
            "segment_count": len(segments),
            "forecast_points": sum(1 for s in samples if s is not None),
            "generation_points": len(estimates),
            "uncertainty_fraction": uncertainty,
            "posted_limit_kmh": posted_limit_kmh,
            "trajectory_assumptions": dict(trajectory.assumptions),
        },
        data_gaps=sorted(set(gaps)),
        regulatory_notes=regulatory,
        human_review_required=True,
    )
