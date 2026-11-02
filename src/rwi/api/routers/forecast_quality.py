"""Forecast intelligence endpoints: verification, ensembles, model comparison.

These exist so the dashboard can stop saying "no verification history yet" and
start saying how good the forecasts have actually been. Until the capture
worker has run for a while, the honest answer is still "not enough evidence",
and every endpoint here is built to say exactly that rather than to produce a
confident-looking number from six samples.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from rwi.api.capability import require_attr
from rwi.api.deps import LatQuery, LonQuery, resolve_point
from rwi.config import Settings, get_settings
from rwi.contracts import (
    EnsembleSummary,
    ModelComparison,
    VerificationReport,
)
from rwi.core.state import StateStore, get_state_store
from rwi.core.timebase import TimeWindow, utc_now

router = APIRouter(prefix="/api/forecast", tags=["forecast intelligence"])

StoreDep = Annotated[StateStore, Depends(get_state_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


class ArchiveStatus(BaseModel):
    """What the verification history actually contains right now."""

    available: bool
    coverage: dict[str, Any] = Field(default_factory=dict)
    message: str | None = None
    capture_enabled: bool = False
    capture_interval_s: float | None = None


def _open_archive(settings: Settings) -> Any:
    opener: Any = require_attr("rwi.archive", "open_archive", capability="forecast archive")
    return opener(settings)


@router.get("/archive", response_model=ArchiveStatus)
async def read_archive_status(settings: SettingsDep) -> Any:
    """Archive coverage, or a plain statement that there is none.

    A broken or absent archive must never look like an empty one: the message
    says which it is.
    """
    try:
        archive = _open_archive(settings)
    except Exception as exc:
        return ArchiveStatus(
            available=False,
            message=f"{type(exc).__name__}: {exc}",
            capture_enabled=settings.capture_enabled,
            capture_interval_s=settings.capture_interval_s,
        )
    try:
        return ArchiveStatus(
            available=True,
            coverage=archive.coverage(),
            capture_enabled=settings.capture_enabled,
            capture_interval_s=settings.capture_interval_s,
        )
    finally:
        archive.close()


@router.get("/verification", response_model=VerificationReport)
async def read_verification(
    settings: SettingsDep,
    variables: Annotated[str | None, Query(description="comma-separated canonical names")] = None,
    days: Annotated[float, Query(gt=0.0, le=400.0)] = 30.0,
    provider: Annotated[str | None, Query()] = None,
) -> Any:
    """Measured forecast skill, by variable and lead time.

    Returns a report with an empty score list and an explaining note when there
    is not yet enough history - never an invented score.
    """
    verify: Any = require_attr("rwi.verification", "verify", capability="forecast verification")
    names = (
        tuple(v.strip() for v in variables.split(",") if v.strip())
        if variables
        else ("temperature_2m", "wind_speed_10m", "cloud_cover", "ghi")
    )
    now = utc_now()
    period = TimeWindow(start_utc=now - timedelta(days=days), end_utc=now)
    archive = _open_archive(settings)
    try:
        return verify(
            archive,
            variables=names,
            period=period,
            providers=[provider] if provider else None,
        )
    finally:
        archive.close()


class EnsembleResponse(BaseModel):
    summaries: list[EnsembleSummary]
    variable: str
    member_count: int
    model_name: str | None
    caveat: str = Field(
        description="what the spread does and does not mean; the UI must show this"
    )


@router.get("/ensemble", response_model=EnsembleResponse)
async def read_ensemble(
    store: StoreDep,
    settings: SettingsDep,
    variable: Annotated[str, Query(description="canonical variable name")] = "ghi",
    lat: Annotated[float | None, LatQuery] = None,
    lon: Annotated[float | None, LonQuery] = None,
    elev: Annotated[float | None, Query()] = None,
    hours: Annotated[float, Query(gt=0.0, le=240.0)] = 48.0,
) -> Any:
    """Ensemble spread for one variable: the platform's only honest uncertainty.

    It comes from members that were actually run, not from an error bar added
    afterwards.
    """
    summarise: Any = require_attr("rwi.ensemble", "summarise", capability="ensemble forecasting")
    note: Any = require_attr(
        "rwi.ensemble", "spread_skill_note", capability="ensemble forecasting"
    )
    available: Any = require_attr(
        "rwi.providers.registry", "available_providers", capability="weather providers"
    )
    point = resolve_point(lat, lon, elev, store)
    now = utc_now()
    window = TimeWindow(start_utc=now, end_utc=now + timedelta(hours=hours))
    providers = available(settings)
    openmeteo = providers.get("open-meteo")
    if openmeteo is None or not openmeteo.capabilities.supports_ensemble:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={
                "error": {
                    "code": "CapabilityNotImplemented",
                    "message": "no configured provider offers ensemble forecasts",
                    "detail": "ensemble spread is the platform's only source of uncertainty; "
                    "without it, forecasts are shown without error bars",
                }
            },
        )
    forecast = await openmeteo.fetch_ensemble(point, window, [variable])
    summaries = summarise(forecast, variable=variable)
    return EnsembleResponse(
        summaries=summaries,
        variable=variable,
        member_count=len(forecast.members),
        model_name=forecast.model_name,
        caveat=note(summaries[0]) if summaries else "no ensemble members covered this period",
    )


@router.get("/models", response_model=ModelComparison)
async def read_model_comparison(
    store: StoreDep,
    settings: SettingsDep,
    variable: Annotated[str, Query()] = "cloud_cover",
    lat: Annotated[float | None, LatQuery] = None,
    lon: Annotated[float | None, LonQuery] = None,
    elev: Annotated[float | None, Query()] = None,
    hours: Annotated[float, Query(gt=0.0, le=240.0)] = 48.0,
    models: Annotated[str | None, Query(description="comma-separated model names")] = None,
) -> Any:
    """Several numerical models side by side, with their disagreements.

    Disagreement is the cheapest honest signal that a forecast needs a margin.
    """
    compare: Any = require_attr("rwi.fusion", "compare_models", capability="model comparison")
    available: Any = require_attr(
        "rwi.providers.registry", "available_providers", capability="weather providers"
    )
    capture_models: Any = require_attr("rwi.capture", "CAPTURE_MODELS", capability="model list")
    point = resolve_point(lat, lon, elev, store)
    now = utc_now()
    window = TimeWindow(start_utc=now, end_utc=now + timedelta(hours=hours))
    names = (
        tuple(m.strip() for m in models.split(",") if m.strip())
        if models
        else tuple(capture_models)
    )
    openmeteo = available(settings).get("open-meteo")
    series_by_model: dict[str, Any] = {}
    notes: list[str] = []
    for name in names:
        try:
            series_by_model[name] = await openmeteo.fetch_forecast(
                point, window, [variable], models=name
            )
        except Exception as exc:
            notes.append(f"{name}: {type(exc).__name__}: {exc}")
    comparison = compare(series_by_model, variable=variable)
    if notes:
        comparison = comparison.model_copy(
            update={"notes": [*comparison.notes, *notes]}
        )
    return comparison


class CaptureTriggerResponse(BaseModel):
    summary: str
    forecast_rows: int
    observation_rows: int
    failures: list[str]
    notes: list[str]


@router.post("/capture", response_model=CaptureTriggerResponse)
async def trigger_capture(
    settings: SettingsDep,
    sites: Annotated[str | None, Query(description="comma-separated site ids")] = None,
) -> Any:
    """Run one capture cycle now.

    Manual trigger for operators and for seeding the archive; the scheduled
    worker runs as its own process (`python -m rwi.capture`).
    """
    capture_once: Any = require_attr("rwi.capture", "capture_once", capability="capture worker")
    sites_from_ids: Any = require_attr("rwi.capture", "sites_from_ids", capability="capture worker")
    available: Any = require_attr(
        "rwi.providers.registry", "available_providers", capability="weather providers"
    )
    archive = _open_archive(settings)
    try:
        result = await capture_once(
            archive=archive,
            providers=available(settings),
            sites=sites_from_ids(
                [s.strip() for s in sites.split(",") if s.strip()] if sites else None
            ),
            forecast_hours=settings.capture_forecast_hours,
        )
    finally:
        archive.close()
    return CaptureTriggerResponse(
        summary=result.summary(),
        forecast_rows=result.forecast_rows,
        observation_rows=result.observation_rows,
        failures=result.failures,
        notes=result.notes,
    )
