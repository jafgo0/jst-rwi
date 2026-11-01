"""Modelled solar energy: irradiance forecast -> array power -> energy."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from rwi.api.capability import require_attr
from rwi.api.deps import LatQuery, LonQuery, resolve_point
from rwi.api.integration import (
    ENERGY_VARIABLES,
    energy_from_estimates,
    power_estimates_from_forecast,
)
from rwi.config import Settings, get_settings
from rwi.contracts import ArrayConfig, EnergyEstimate, PowerEstimate
from rwi.core.provenance import DataKind
from rwi.core.state import StateStore, get_state_store
from rwi.core.timebase import TimeWindow, utc_now

router = APIRouter(prefix="/api/energy", tags=["energy"])

StoreDep = Annotated[StateStore, Depends(get_state_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


class EnergyForecastResponse(BaseModel):
    """Power per step plus the integrated energy, with the caveats attached."""

    estimates: list[PowerEstimate]
    energy: EnergyEstimate | None
    array: ArrayConfig
    notes: list[str]
    weather_provider: str
    weather_kind: DataKind
    weather_retrieved_at_utc: Any


@router.get("/array")
async def read_array_config() -> ArrayConfig:
    """The array parameters in use. `parameter_source` says whether they are
    measured or assumed - the UI must show that."""
    return require_attr("rwi.energy.array", "DEFAULT_ARRAY", capability="PV model")


@router.get("/forecast", response_model=EnergyForecastResponse)
async def read_energy_forecast(
    store: StoreDep,
    settings: SettingsDep,
    lat: Annotated[float | None, LatQuery] = None,
    lon: Annotated[float | None, LonQuery] = None,
    elev: Annotated[float | None, Query()] = None,
    hours: Annotated[float, Query(gt=0.0, le=120.0)] = 24.0,
    heading_deg: Annotated[float | None, Query(ge=0.0, lt=360.0)] = None,
    provider: Annotated[str | None, Query()] = None,
) -> Any:
    available: Any = require_attr(
        "rwi.providers.registry", "available_providers", capability="weather providers"
    )
    fetch_with_fallback: Any = require_attr(
        "rwi.providers.registry", "fetch_with_fallback", capability="weather providers"
    )
    point = resolve_point(lat, lon, elev, store)
    heading = heading_deg if heading_deg is not None else store.state.effective_heading_deg
    providers = available(settings)
    if provider:
        providers = {name: p for name, p in providers.items() if name == provider}
    now = utc_now()
    window = TimeWindow(start_utc=now, end_utc=now + timedelta(hours=hours))
    series = await fetch_with_fallback(
        providers, point, window, ENERGY_VARIABLES, kind=DataKind.FORECAST
    )
    estimates, notes = power_estimates_from_forecast(
        series=series, point=point, heading_deg=heading
    )
    energy = energy_from_estimates(estimates)
    return EnergyForecastResponse(
        estimates=estimates,
        energy=energy,
        array=require_attr("rwi.energy.array", "DEFAULT_ARRAY", capability="PV model"),
        notes=notes,
        weather_provider=series.provider,
        weather_kind=series.kind,
        weather_retrieved_at_utc=series.retrieved_at_utc,
    )
