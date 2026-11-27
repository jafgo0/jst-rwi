"""Stable v1 product surface: ML P10/P50/P90 along the route, and their verification.

``/api/v1/forecast/route`` gives, at each trajectory point and its own ETA, a
`QuantileBand` per variable from the latest ML model, or the reason there is
none (`rwi.ml.bands.BandReason`). A missing band is never filled with the raw
upstream value. Head- and crosswind are box projections of the u/v bands onto
the route heading (ADR 0011 §4), so their ``interval_probability`` is ``None``.

``/api/v1/verification`` scores each variable's latest model on archive rows
issued after its training cutoff, against the truth hierarchy of ADR 0011 §3,
and names which truth source supplied how many rows.

``/api/ml/*`` stays EXPERIMENTAL; this prefix is STABLE (docs/CONTRACTS.md).
"""

import asyncio
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from rwi.alerts.fronts import (
    DEFAULT_FRONT_STOPS,
    FRONT_LOOKBACK_H,
    FrontOutlook,
    front_alerts,
    stop_front_passage,
)
from rwi.api.capability import require_attr
from rwi.api.integration import MAX_TRAJECTORY_SAMPLES
from rwi.api.routers.ml import MlLoadedModel
from rwi.api.routers.route import RepoDep, SettingsDep, StoreDep, _build_trajectory
from rwi.contracts import QuantileBand
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind
from rwi.core.timebase import TimeWindow, to_local, utc_now

router = APIRouter(prefix="/api/v1", tags=["v1"])

_CAPABILITY = "ML post-processing"
HEADWIND, CROSSWIND = "headwind_10m", "crosswind_10m"
ROUTE_RELATIVE: tuple[str, str] = (HEADWIND, CROSSWIND)
UV: tuple[str, str] = ("wind_u_10m", "wind_v_10m")
_WIND_INPUTS: tuple[str, str] = ("wind_speed_10m", "wind_direction_10m")
#: Model variables of ADR 0008/0011 (`rwi.ml.constraints.ML_VARIABLES`).
MODEL_VARIABLES: tuple[str, ...] = (
    "ghi",
    "dni",
    "cloud_cover",
    "wind_speed_10m",
    "temperature_2m",
    *UV,
)
FORECAST_VARIABLES: tuple[str, ...] = (*MODEL_VARIABLES, *ROUTE_RELATIVE)
DEFAULT_FORECAST_VARIABLES: tuple[str, ...] = (
    "ghi",
    "dni",
    "cloud_cover",
    "temperature_2m",
    HEADWIND,
    CROSSWIND,
)

Reason = Literal["no_model", "no_upstream", "not_applicable", "outside_training_range"]


class V1PointForecast(BaseModel):
    """Bands at one predicted position, valid at its ETA, and why any variable has none."""

    model_config = ConfigDict(frozen=True)

    distance_m: float
    eta_utc: datetime
    eta_local: str
    point: GeoPoint
    heading_deg: float
    #: provider/model of the upstream forecast the bands post-process; None if none fetched
    upstream_source: str | None
    bands: dict[str, QuantileBand] = Field(default_factory=dict)
    unavailable: dict[str, Reason] = Field(default_factory=dict)


class V1RouteForecast(BaseModel):
    """ML quantile bands along the predicted trajectory (provenance per band)."""

    model_config = ConfigDict(frozen=True)

    route_id: str
    generated_at_utc: datetime
    variables: tuple[str, ...]
    loaded: list[MlLoadedModel] = Field(default_factory=list)
    #: model variable -> why no model was loaded for it
    missing: dict[str, str] = Field(default_factory=dict)
    points: list[V1PointForecast]
    notes: list[str] = Field(default_factory=list)


class V1Score(BaseModel):
    """Holdout scores of one system (``ml`` or an upstream source) over all rows."""

    model_config = ConfigDict(frozen=True)

    system: str
    n: int
    mae: float | None
    rmse: float | None
    bias: float | None
    crps: float | None
    coverage_80: float | None = None


class V1VariableVerification(BaseModel):
    """Verification of one variable's latest model, or the honest reason there is none."""

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    variable: str
    status: Literal["ok", "no_truth", "no_model", "no_holdout"]
    reason: str | None = None
    model_version: str | None = None
    #: truth provider -> archive rows it supplied (ADR 0011 §3)
    truth_sources: dict[str, int] = Field(default_factory=dict)
    target_kind: str | None = None
    n_holdout: int = 0
    scores: list[V1Score] = Field(default_factory=list)


class V1Verification(BaseModel):
    """Per-variable verification against archived truth."""

    model_config = ConfigDict(frozen=True)

    generated_at_utc: datetime
    variables: list[V1VariableVerification]


def _parse(
    raw: str | None, *, allowed: tuple[str, ...], default: tuple[str, ...]
) -> tuple[str, ...]:
    if not raw:
        return default
    wanted = tuple(dict.fromkeys(v.strip() for v in raw.split(",") if v.strip()))
    unknown = [v for v in wanted if v not in allowed]
    if unknown or not wanted:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown variables {unknown}; choose from {list(allowed)}",
        )
    return wanted


def _model_variables(wanted: tuple[str, ...]) -> tuple[str, ...]:
    base = [v for v in wanted if v not in ROUTE_RELATIVE]
    if any(v in ROUTE_RELATIVE for v in wanted):
        base.extend(UV)
    return tuple(dict.fromkeys(base))


def _fetch_variables(models: tuple[str, ...]) -> tuple[str, ...]:
    out = [v for v in models if v not in UV]
    if any(v in UV for v in models):
        out.extend(_WIND_INPUTS)
    return tuple(dict.fromkeys(out))


def _load_models(
    settings: Any, variables: tuple[str, ...]
) -> tuple[dict[str, Any], dict[str, str]]:
    load_model = require_attr("rwi.ml.artifacts", "load_model", capability=_CAPABILITY)
    unavailable = require_attr("rwi.ml.quantile", "MlUnavailableError", capability=_CAPABILITY)
    models: dict[str, Any] = {}
    missing: dict[str, str] = {}
    for variable in variables:
        try:
            models[variable] = load_model(settings.models_dir, variable)
        except unavailable as exc:
            missing[variable] = str(exc)
    return models, missing


@router.get("/forecast/route", response_model=V1RouteForecast)
async def read_route_forecast(
    repo: RepoDep,
    store: StoreDep,
    settings: SettingsDep,
    route_id: Annotated[str, Query(min_length=1)],
    speed_kmh: Annotated[float, Query(gt=0.0, le=130.0)] = 80.0,
    start_distance_m: Annotated[float | None, Query(ge=0.0)] = None,
    hours: Annotated[float, Query(gt=0.0, le=240.0)] = 24.0,
    variables: Annotated[str | None, Query(description="comma-separated names")] = None,
) -> Any:
    """P10/P50/P90 bands at each trajectory point and ETA; ``unavailable`` says why not."""
    wanted = _parse(variables, allowed=FORECAST_VARIABLES, default=DEFAULT_FORECAST_VARIABLES)
    route = repo.get(route_id)
    trajectory = _build_trajectory(
        route, speed_kmh=speed_kmh, start_distance_m=start_distance_m, hours=hours, store=store
    )
    now = utc_now()
    horizon = store.state.effective_time(now) + timedelta(hours=hours)
    points = [p for p in trajectory.points if p.eta_utc <= horizon] or trajectory.points[:1]
    if len(points) > MAX_TRAJECTORY_SAMPLES:
        stride = max(1, len(points) // MAX_TRAJECTORY_SAMPLES)
        points = points[::stride][:MAX_TRAJECTORY_SAMPLES]

    model_vars = _model_variables(wanted)
    models, missing = _load_models(settings, model_vars)
    loaded = [
        MlLoadedModel(
            variable=v,
            model_id=manifest.model_id,
            model_version=manifest.model_version,
            calibrated=manifest.calibrated,
        )
        for v, (_, manifest) in sorted(models.items())
    ]
    route_point_bands = require_attr("rwi.ml.bands", "route_point_bands", capability=_CAPABILITY)
    notes: list[str] = []
    fetch_vars = _fetch_variables(tuple(sorted(models)))
    if models:
        available = require_attr(
            "rwi.providers.registry", "available_providers", capability="weather providers"
        )
        fetch_with_fallback = require_attr(
            "rwi.providers.registry", "fetch_with_fallback", capability="weather providers"
        )
        postprocessor = require_attr(
            "rwi.ml.postprocess", "MlPostprocessor", capability=_CAPABILITY
        )(models)
        source_id = require_attr("rwi.ml.features", "source_id", capability=_CAPABILITY)
        providers = available(settings)
    else:
        notes.append("no ML model could be loaded for the requested variables")

    out: list[V1PointForecast] = []
    for p in points:
        upstream = ml = None
        if models:
            window = TimeWindow(
                start_utc=min(now, p.eta_utc - timedelta(hours=1)),
                end_utc=p.eta_utc + timedelta(hours=1),
            )
            try:
                upstream = await fetch_with_fallback(
                    providers, p.point, window, fetch_vars, kind=DataKind.FORECAST
                )
            except Exception as exc:
                notes.append(f"{p.distance_m / 1000:.0f} km: {type(exc).__name__}: {exc}")
            if upstream is not None:
                # No training path fills route_distance_km yet, so every model's
                # envelope has it all-NaN; passing it would mark every point
                # outside_training_range. Same as /api/ml: leave it out.
                ml = postprocessor.apply(upstream, now_utc=now)
        result = route_point_bands(
            upstream,
            ml,
            models=models,
            eta_utc=p.eta_utc,
            heading_deg=p.heading_deg,
            variables=wanted,
        )
        out.append(
            V1PointForecast(
                distance_m=p.distance_m,
                eta_utc=p.eta_utc,
                eta_local=to_local(p.eta_utc, p.point).isoformat(),
                point=p.point,
                heading_deg=p.heading_deg,
                upstream_source=(
                    None if upstream is None else source_id(upstream.provider, upstream.model_name)
                ),
                bands=result.bands,
                unavailable=result.reasons,
            )
        )
    return V1RouteForecast(
        route_id=route_id,
        generated_at_utc=now,
        variables=wanted,
        loaded=loaded,
        missing=missing,
        points=out,
        notes=sorted(set(notes)),
    )


def _verify(archive: Any, variable: str, settings: Any) -> V1VariableVerification:
    truth_pairs = require_attr("rwi.ml.targets", "truth_pairs", capability=_CAPABILITY)
    evaluate = require_attr("rwi.ml.__main__", "evaluate", capability=_CAPABILITY)
    unavailable = require_attr("rwi.ml.quantile", "MlUnavailableError", capability=_CAPABILITY)
    truth = truth_pairs(archive, variable)
    sources = truth.sources
    if not truth.pairs:
        return V1VariableVerification(
            variable=variable,
            status="no_truth",
            reason="the archive holds no forecast/observation pairs from an accepted truth source",
        )
    try:
        report = evaluate(list(truth.pairs), variable=variable, models_dir=settings.models_dir)
    except unavailable as exc:
        return V1VariableVerification(
            variable=variable,
            status="no_model",
            reason=str(exc),
            truth_sources=sources,
            target_kind=truth.target_kind,
        )
    scores = [
        V1Score(
            system=r.system,
            n=r.n,
            mae=r.mae,
            rmse=r.rmse,
            bias=r.bias,
            crps=r.crps,
            coverage_80=r.coverage_80,
        )
        for r in report.rows
        if r.group_kind == "all"
    ]
    return V1VariableVerification(
        variable=variable,
        status="ok" if report.n_holdout else "no_holdout",
        reason=None if report.n_holdout else "no archive rows after the training cutoff yet",
        model_version=report.model_version,
        truth_sources=sources,
        target_kind=truth.target_kind,
        n_holdout=report.n_holdout,
        scores=scores,
    )


@router.get("/verification", response_model=V1Verification)
async def read_verification(
    settings: SettingsDep,
    variables: Annotated[str | None, Query(description="comma-separated names")] = None,
) -> Any:
    """Holdout scores per variable against the ADR 0011 truth hierarchy, or why none."""
    wanted = _parse(variables, allowed=MODEL_VARIABLES, default=MODEL_VARIABLES)
    open_archive = require_attr("rwi.archive", "open_archive", capability="forecast archive")
    try:
        archive = open_archive(settings)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"forecast archive unavailable: {type(exc).__name__}: {exc}",
        ) from exc
    archive_error = require_attr(
        "rwi.archive.errors", "ArchiveUnavailableError", capability="forecast archive"
    )
    try:
        results = [_verify(archive, v, settings) for v in wanted]
    except archive_error as exc:
        # The archive opens lazily, so an unreadable file surfaces on first read.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"forecast archive unavailable: {type(exc).__name__}: {exc}",
        ) from exc
    finally:
        archive.close()
    return V1Verification(generated_at_utc=utc_now(), variables=results)


# --- /api/v1/fronts (ADR 0012) ---------------------------------------------------------

FRONT_ENSEMBLE_MODEL = "ecmwf_ifs025"
FRONT_DETERMINISTIC_MODEL = "ecmwf_aifs025_single"
FRONT_CACHE_TTL_S = 600.0
FRONT_FETCH_TIMEOUT_S = 30.0

_fronts_cache: dict[tuple[tuple[str, ...], float], tuple[datetime, Any]] = {}
_fronts_lock = asyncio.Lock()


def _reset_fronts_cache() -> None:
    """Drop cached outlooks (tests; each TestClient runs its own event loop)."""
    global _fronts_lock
    _fronts_cache.clear()
    _fronts_lock = asyncio.Lock()


def _reason(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}".rstrip(": ")


@router.get("/fronts", response_model=FrontOutlook)
async def read_fronts(
    settings: SettingsDep,
    stops: Annotated[str | None, Query(description="comma-separated stop names")] = None,
    hours: Annotated[float, Query(ge=1.0, le=168.0)] = 72.0,
) -> Any:
    """Ensemble front probability and conditional P10/P50/P90 passage per stop (ADR 0012)."""
    variables: Any = require_attr(
        "rwi.alerts.synoptic", "SYNOPTIC_VARIABLES", capability="synoptic analysis"
    )
    load_route: Any = require_attr("rwi.route.bwsc", "load_bwsc_route", capability="BWSC route")
    from rwi.api.routers.meteorologist import _synoptic_points

    known = {name: (dist, point) for name, dist, point in _synoptic_points(load_route())}
    wanted = (
        tuple(s.strip() for s in stops.split(",") if s.strip()) if stops else DEFAULT_FRONT_STOPS
    )
    unknown = [s for s in wanted if s not in known]
    if unknown or not wanted:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown stop(s) {unknown}; known: {sorted(known)}",
        )
    now = utc_now()
    key = (wanted, hours)
    async with _fronts_lock:
        cached = _fronts_cache.get(key)
        if cached is not None and (now - cached[0]).total_seconds() < FRONT_CACHE_TTL_S:
            return cached[1]
        available: Any = require_attr(
            "rwi.providers.registry", "available_providers", capability="weather providers"
        )
        close: Any = require_attr(
            "rwi.providers.registry", "close_providers", capability="weather providers"
        )
        horizon = timedelta(hours=hours)
        window = TimeWindow(
            start_utc=now - timedelta(hours=FRONT_LOOKBACK_H),
            end_utc=now + horizon + timedelta(hours=3),
        )
        providers = available(settings)
        try:
            openmeteo = providers.get("open-meteo")
            if openmeteo is None or not openmeteo.capabilities.supports_ensemble:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Open-Meteo ensemble provider is not available",
                )

            async def ens(point: GeoPoint) -> Any:
                return await asyncio.wait_for(
                    openmeteo.fetch_ensemble(point, window, variables, models=FRONT_ENSEMBLE_MODEL),
                    timeout=FRONT_FETCH_TIMEOUT_S,
                )

            async def det(point: GeoPoint) -> Any:
                return await asyncio.wait_for(
                    openmeteo.fetch_forecast(
                        point, window, variables, models=FRONT_DETERMINISTIC_MODEL
                    ),
                    timeout=FRONT_FETCH_TIMEOUT_S,
                )

            results = await asyncio.gather(
                *(f(known[s][1]) for s in wanted for f in (ens, det)), return_exceptions=True
            )
        finally:
            await close(providers)
        per_stop = []
        for i, name in enumerate(wanted):
            e, d = results[2 * i], results[2 * i + 1]
            dist, point = known[name]
            per_stop.append(
                stop_front_passage(
                    name,
                    dist,
                    point,
                    None if isinstance(e, BaseException) else e,
                    now=now,
                    horizon=horizon,
                    ensemble_error=_reason(e) if isinstance(e, BaseException) else None,
                    deterministic=None if isinstance(d, BaseException) else d,
                    deterministic_model=FRONT_DETERMINISTIC_MODEL,
                    deterministic_error=_reason(d) if isinstance(d, BaseException) else None,
                )
            )
        watch = settings.front_alert_watch_probability
        warning = settings.front_alert_warning_probability
        try:
            alerts = front_alerts(per_stop, watch=watch, warning=warning, now=now)
        except ValueError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        outlook = FrontOutlook(
            generated_at_utc=now,
            window=window,
            horizon_h=hours,
            ensemble_model=FRONT_ENSEMBLE_MODEL,
            deterministic_model=FRONT_DETERMINISTIC_MODEL,
            stops=per_stop,
            alerts=alerts,
            alerts_configured=watch is not None and warning is not None,
        )
        if any(s.error is None for s in per_stop):
            _fronts_cache[key] = (now, outlook)
        return outlook
