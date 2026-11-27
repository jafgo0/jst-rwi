"""The /api/meteorologist/* endpoints: the briefing layer.

This router is pure orchestration. It calls the existing engines
synchronously via the already-wired capabilities and hands their
structured results to :mod:`rwi.meteorologist.tools`, which in turn
hands them to :mod:`rwi.meteorologist.composer`. It never computes a
number itself.

The deterministic endpoints work without network when the state and
engines allow it; endpoints that need weather data report unavailable
through the tool layer rather than invent values.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from rwi.alerts import (
    data_integrity_alerts,
    sort_alerts,
    weather_alerts,
)
from rwi.api.capability import require_attr
from rwi.api.deps import resolve_point
from rwi.api.security import ASK_LIMITER, enforce_rate_limit, require_shared_secret
from rwi.config import Settings, get_settings
from rwi.core.errors import PositionUnavailableError, RwiError
from rwi.core.provenance import DataKind
from rwi.core.state import StateStore, get_state_store
from rwi.core.timebase import TimeWindow, utc_now
from rwi.meteorologist import (
    Briefing,
    BriefingContext,
    QuestionKind,
    ToolResult,
    compose,
)
from rwi.meteorologist import tools as mt
from rwi.meteorologist.llm import AdapterUnavailable, build_classifier
from rwi.meteorologist.self_knowledge import platform_confidence

if TYPE_CHECKING:
    from rwi.alerts.synoptic import RoutePointSeries, SynopticAnalysis
    from rwi.contracts import Route

router = APIRouter(prefix="/api/meteorologist", tags=["meteorologist"])

StoreDep = Annotated[StateStore, Depends(get_state_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

BRIEFING_VARIABLES = (
    "temperature_2m",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "ghi",
    "dni",
    "dhi",
    "precipitation",
    "precipitation_probability",
    "visibility",
    "cloud_cover",
    # Instability indices for the morning briefing's convection section.
    # If the provider cannot deliver them the fetch still succeeds for the
    # rest; the meteorologist layer surfaces the gap in `unknowns`.
    "cape",
    "lifted_index",
)


async def _assemble_context(
    store: StateStore, settings: Settings, hours: float
) -> BriefingContext:
    """Fetch what the engines can give us; shape it into tool results.

    Every failure degrades to ``ToolResult.unavailable`` with the reason.
    The composer then surfaces each gap in :attr:`Briefing.unknowns`
    rather than silently omitting it.
    """
    now = utc_now()
    # Weather windows and outlooks follow the selected time; data-integrity and
    # archive ages stay on the wall clock.
    at = store.state.effective_time(now)
    state_result = mt.where_and_when(store.state)

    # Solar day events: only when we have a position.
    solar_day_result: ToolResult = ToolResult.unavailable(
        "ingen position för soldag-kalkyl"
    )
    try:
        if store.state.fix is not None:
            solar_day_events: Any = require_attr(
                "rwi.solar.geometry", "solar_day_events", capability="solar geometry"
            )
            events = solar_day_events(store.state.fix.point, at)
            solar_day_result = mt.solar_day(events)
    except (PositionUnavailableError, RwiError) as exc:
        solar_day_result = ToolResult.unavailable(
            f"soldag-kalkyl misslyckades: {exc}"
        )
    except Exception as exc:
        solar_day_result = ToolResult.unavailable(
            f"soldag-kalkyl misslyckades: {type(exc).__name__}"
        )

    # Weather series + irradiance/power: single provider fetch feeds both.
    weather_series: list[Any] = []
    provider_health: list[Any] = []
    power_estimates: list[Any] = []
    try:
        available: Any = require_attr(
            "rwi.providers.registry",
            "available_providers",
            capability="weather providers",
        )
        fetch_with_fallback: Any = require_attr(
            "rwi.providers.registry",
            "fetch_with_fallback",
            capability="weather providers",
        )
        providers = available(settings)
        provider_health = [await p.health() for p in providers.values()]
        point = resolve_point(None, None, None, store)
        window = TimeWindow(start_utc=at, end_utc=at + timedelta(hours=hours))
        series = await fetch_with_fallback(
            providers, point, window, BRIEFING_VARIABLES, kind=DataKind.FORECAST
        )
        weather_series = [series]
        power_fn: Any | None
        try:
            power_fn = require_attr(
                "rwi.api.integration",
                "power_estimates_from_forecast",
                capability="energy model",
            )
        except Exception:
            power_fn = None
        if power_fn is not None:
            try:
                estimates, _ = power_fn(
                    series=series,
                    point=point,
                    heading_deg=store.state.effective_heading_deg,
                )
                power_estimates = list(estimates)
            except Exception:
                power_estimates = []
    except (PositionUnavailableError, RwiError):
        weather_series = []
    except Exception:
        weather_series = []

    irradiance_result = mt.irradiance_forecast(power_estimates or None)
    # No route endpoint plumbed into Fas 1 (requires route_id orchestration).
    route_weather_result: ToolResult = ToolResult.unavailable(
        "ruttväder är inte kopplat i Fas 1 (kräver route_id-param)"
    )

    # Alerts: already composable from the fetched series.
    alerts_list: list[Any] | None = None
    if weather_series:
        alerts: list[Any] = []
        for item in weather_series:
            alerts.extend(
                weather_alerts(item, now=at, horizon_s=hours * 3600.0)
            )
        alerts.extend(
            data_integrity_alerts(
                series=weather_series,
                fix=store.state.fix,
                suspect_fix=store.state.suspect_fix,
                provider_health=provider_health,
                now=now,
            )
        )
        alerts_list = sort_alerts(alerts)
    alerts_result = mt.active_alerts(alerts_list)

    # Ensemble and fusion: Fas 1 does not wire the ensemble fetch (it needs a
    # second provider call with the ensemble API). The tool returns
    # unavailable here, which the composer renders as a data gap - exactly
    # what section 12 requires: say what is not known.
    ensemble_ghi_result: ToolResult = mt.ensemble_summary(None, variable="ghi")
    ensemble_wind_result: ToolResult = mt.ensemble_summary(
        None, variable="wind_speed_10m"
    )
    disagreements_ghi_result: ToolResult = mt.model_disagreements(
        None, variable="ghi"
    )

    # Verification: read any stored scores, otherwise unavailable with reason.
    verification_ghi: ToolResult = mt.verification_skill(
        None, variable="ghi", lead_time_s=hours * 3600.0
    )

    # Convection + wind outlooks: read from the first fetched series if any.
    series_for_outlook = weather_series[0] if weather_series else None
    convection_result = mt.convection_outlook(
        series_for_outlook, now=at, horizon_s=hours * 3600.0
    )
    wind_result = mt.wind_outlook(
        series_for_outlook, now=at, horizon_s=hours * 3600.0
    )

    # Fas 3: self-knowledge from the verification archive. open_archive is
    # lazy (does not raise on a missing file); platform_confidence catches
    # ArchiveUnavailableError at the first read and returns an honest
    # 'archive_available=False' summary. We never pretend a verification
    # exists when it does not.
    self_knowledge_result: ToolResult = ToolResult.unavailable(
        "arkivkoppling kunde inte upprättas"
    )
    try:
        open_archive: Any = require_attr(
            "rwi.archive.store", "open_archive", capability="forecast archive"
        )
        archive = open_archive(settings)
        summary = platform_confidence(
            archive, variables=BRIEFING_VARIABLES, now=now
        )
        self_knowledge_result = mt.platform_self_knowledge(summary)
    except Exception as exc:
        self_knowledge_result = ToolResult.unavailable(
            f"självkännedoms-beräkning misslyckades: {type(exc).__name__}: {exc}"
        )

    return BriefingContext(
        state=state_result,
        solar_day=solar_day_result,
        route_weather=route_weather_result,
        irradiance=irradiance_result,
        ensemble_ghi=ensemble_ghi_result,
        ensemble_wind=ensemble_wind_result,
        disagreements_ghi=disagreements_ghi_result,
        verification_ghi=verification_ghi,
        alerts=alerts_result,
        convection=convection_result,
        wind=wind_result,
        self_knowledge=self_knowledge_result,
    )


# ----------------------------------------------------------------------------- synoptic (#10, #14)

SYNOPTIC_LOOKBACK_H = 24.0
"""Model look-back so the front rules can compare with the same hour the day before."""
SYNOPTIC_AHEAD_H = 48.0
"""Covers the longest briefing horizon (24 h) plus a full 24 h tendency after it."""
SYNOPTIC_POINT_TIMEOUT_S = 20.0
SYNOPTIC_CACHE_TTL_S = 600.0
"""Open-Meteo updates hourly; ten minutes avoids eleven fetches per dashboard poll."""

_synoptic_cache: tuple[datetime, SynopticAnalysis] | None = None
_synoptic_lock = asyncio.Lock()


def _reset_synoptic_cache() -> None:
    """Drop the cached analysis (tests; each TestClient runs its own event loop)."""
    global _synoptic_cache, _synoptic_lock
    _synoptic_cache = None
    _synoptic_lock = asyncio.Lock()


def _synoptic_points(route: Route) -> list[tuple[str, float, Any]]:
    """Named route waypoints (start, control stops, finish) in route order."""
    out: list[tuple[str, float, Any]] = []
    for w in route.waypoints:
        if not w.name or w.kind.value == "waypoint":
            continue
        name = w.name.removesuffix(" Control Stop")
        out.append((name, w.cumulative_distance_m, w.point))
    return out


async def _fetch_route_series(
    fetch: Any,
    providers: Any,
    points: Sequence[tuple[str, float, Any]],
    window: TimeWindow,
) -> list[RoutePointSeries]:
    """One forecast per point, in parallel; a failed point is recorded, not fatal."""
    from rwi.alerts.synoptic import SYNOPTIC_VARIABLES, RoutePointSeries

    async def one(point: Any) -> Any:
        return await asyncio.wait_for(
            fetch(providers, point, window, SYNOPTIC_VARIABLES, kind=DataKind.FORECAST),
            timeout=SYNOPTIC_POINT_TIMEOUT_S,
        )

    results = await asyncio.gather(*(one(p) for _, _, p in points), return_exceptions=True)
    out: list[RoutePointSeries] = []
    for (name, dist, point), res in zip(points, results, strict=True):
        if isinstance(res, BaseException):
            reason = f"{type(res).__name__}: {res}".rstrip(": ")
            out.append(RoutePointSeries(name, dist, point, None, reason))
        else:
            out.append(RoutePointSeries(name, dist, point, res))
    return out


async def _synoptic_result(
    settings: Settings, now: datetime, hours: float
) -> ToolResult[Any]:
    """Route-scale surface analysis for the morning briefing, cached briefly.

    Every failure becomes ``ToolResult.unavailable`` with the reason; the
    composer then lists it under unknowns instead of claiming a calm day.
    """
    global _synoptic_cache
    horizon_s = hours * 3600.0
    try:
        analyse_route: Any = require_attr(
            "rwi.alerts.synoptic", "analyse_route", capability="synoptic analysis"
        )
        async with _synoptic_lock:
            cached = _synoptic_cache
            if cached is not None and (now - cached[0]).total_seconds() < SYNOPTIC_CACHE_TTL_S:
                return mt.synoptic_outlook(cached[1], now=now, horizon_s=horizon_s)
            load_route: Any = require_attr(
                "rwi.route.bwsc", "load_bwsc_route", capability="BWSC route"
            )
            available: Any = require_attr(
                "rwi.providers.registry", "available_providers", capability="weather providers"
            )
            close: Any = require_attr(
                "rwi.providers.registry", "close_providers", capability="weather providers"
            )
            fetch: Any = require_attr(
                "rwi.providers.registry", "fetch_with_fallback", capability="weather providers"
            )
            window = TimeWindow(
                start_utc=now - timedelta(hours=SYNOPTIC_LOOKBACK_H),
                end_utc=now + timedelta(hours=SYNOPTIC_AHEAD_H),
            )
            providers = available(settings)
            try:
                series = await _fetch_route_series(
                    fetch, providers, _synoptic_points(load_route()), window
                )
            finally:
                await close(providers)
            analysis: SynopticAnalysis = analyse_route(series, now=now)
            if analysis.available:
                _synoptic_cache = (now, analysis)
    except Exception as exc:
        return ToolResult.unavailable(
            f"synoptisk analys misslyckades: {type(exc).__name__}: {exc}"
        )
    return mt.synoptic_outlook(analysis, now=now, horizon_s=horizon_s)


@router.get("/risk-ahead", response_model=Briefing)
async def risk_ahead(
    store: StoreDep,
    settings: SettingsDep,
    hours: Annotated[float, Query(gt=0.0, le=48.0)] = 12.0,
) -> Any:
    """Section 12 question: 'Vad är den viktigaste väderrisken framför oss?'"""
    ctx = await _assemble_context(store, settings, hours)
    return compose(QuestionKind.RISK_AHEAD, ctx, asked_at=utc_now())


@router.get("/forecast-reliability", response_model=Briefing)
async def forecast_reliability(
    store: StoreDep,
    settings: SettingsDep,
    variable: Annotated[str, Query()] = "ghi",
    hours: Annotated[float, Query(gt=0.0, le=48.0)] = 6.0,
) -> Any:
    """Section 12 question: 'Hur pålitlig är den här instrålningsprognosen?'"""
    ctx = await _assemble_context(store, settings, hours)
    return compose(
        QuestionKind.FORECAST_RELIABILITY,
        ctx,
        asked_at=utc_now(),
        variable=variable,
    )


@router.get("/morning-briefing", response_model=Briefing)
async def morning_briefing(
    store: StoreDep,
    settings: SettingsDep,
    hours: Annotated[float, Query(gt=0.0, le=24.0)] = 12.0,
) -> Any:
    """Synoptic morning briefing: convection, fronts along the route (#10, #14), wind."""
    now = utc_now()
    ctx, synoptic = await asyncio.gather(
        _assemble_context(store, settings, hours),
        _synoptic_result(settings, now, hours),
    )
    ctx.synoptic = synoptic
    return compose(QuestionKind.MORNING_BRIEFING, ctx, asked_at=now)


@router.post("/ask", response_model=Briefing)
async def ask(
    payload: dict[str, Any],
    store: StoreDep,
    settings: SettingsDep,
    x_telemetry_secret: Annotated[str | None, Header(alias="X-Telemetry-Secret")] = None,
) -> Any:
    """Free-form question → classifier → deterministic composer.

    The LLM is a CLASSIFIER only: it reads the free-text question and
    picks a :class:`QuestionKind` and a variable. The Briefing is still
    built by the deterministic composer from cited engine output, so a
    hallucinating model cannot invent a weather value here.

    Returns 501 when the adapter is not configured (no API key, or the
    ``anthropic`` package not installed); the dashboard then knows the
    capability is off and can show it honestly.
    """
    require_shared_secret(settings, x_telemetry_secret, action="meteorologist/ask")
    question = str(payload.get("question", "")).strip()
    if not question:
        raise HTTPException(
            status_code=400,
            detail={"code": "missing_question", "message": "fältet 'question' krävs"},
        )
    hours = float(payload.get("hours", 12.0))
    if not 0.0 < hours <= 48.0:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_hours", "message": "hours måste vara i (0, 48]"},
        )
    enforce_rate_limit(
        ASK_LIMITER, settings.meteorologist_ask_per_minute, action="meteorologist/ask"
    )
    try:
        classifier = build_classifier(
            api_key=settings.anthropic_api_key, model=settings.anthropic_model
        )
    except AdapterUnavailable as exc:
        raise HTTPException(
            status_code=501,
            detail={"code": "capability_not_configured", "message": str(exc)},
        ) from exc
    try:
        classification = classifier.classify(question)
    except AdapterUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "classifier_failed", "message": str(exc)},
        ) from exc
    ctx = await _assemble_context(store, settings, hours)
    briefing = compose(
        classification.question_kind,
        ctx,
        asked_at=utc_now(),
        variable=classification.variable,
    )
    # Keep the user's actual phrasing visible; the composer uses a canned
    # title per kind, which is unhelpful when the question was free-form.
    return briefing.model_copy(update={"question": question})


__all__ = ["router"]
