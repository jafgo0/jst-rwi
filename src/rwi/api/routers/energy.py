"""Modelled solar energy: irradiance forecast -> array power -> energy."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from rwi.api.array_config import get_active_array
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


class ArrayConfigResponse(BaseModel):
    """ArrayConfig fields plus a safe summary of the angular response matrix.

    The matrix itself is 180 x 45 x N floats and does not belong in a JSON
    payload the dashboard polls. The summary tells a consumer whether per-cell
    geometry is applied, how it was obtained, and what its range is.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    area_m2: float
    cell_efficiency_stc: float
    temperature_coefficient_pmax_per_c: float
    mppt_efficiency: float
    wiring_efficiency: float
    mounting: str
    surface_tilt_deg: float
    surface_azimuth_deg: float | None
    noct_c: float
    albedo: float
    parameter_source: str
    source_notes: str | None
    angular_response: dict[str, Any] | None = Field(
        description="None if no per-group geometry is applied (flat-plane model). "
        "Otherwise: {name, source, group_count, cell_weights, mean, min, max, notes}."
    )


def _summarise_angular(response: Any) -> dict[str, Any] | None:
    if response is None:
        return None
    from rwi.energy.angular import AngularResponseTable

    if not isinstance(response, AngularResponseTable):
        return {"name": "unknown", "source": "unknown",
                "note": "non-AngularResponseTable value ignored by the model"}
    arr = response.values
    return {
        "name": response.name,
        "source": response.source,
        "is_measured": response.is_measured,
        "group_count": response.group_count,
        "cell_weights": list(response.cell_weights) if response.cell_weights else None,
        "shape": list(arr.shape),
        "min": float(arr.min()),
        "max": float(arr.max()),
        "mean": float(arr.mean()),
        "notes": response.notes,
    }


@router.get("/array", response_model=ArrayConfigResponse)
async def read_array_config() -> Any:
    """The array parameters in use. ``parameter_source`` says whether they are
    measured or assumed; ``angular_response`` summarises the per-cell matrix
    if one is configured, without shipping the whole 180x45xN array."""
    array = get_active_array()
    return ArrayConfigResponse(
        name=array.name,
        area_m2=array.area_m2,
        cell_efficiency_stc=array.cell_efficiency_stc,
        temperature_coefficient_pmax_per_c=array.temperature_coefficient_pmax_per_c,
        mppt_efficiency=array.mppt_efficiency,
        wiring_efficiency=array.wiring_efficiency,
        mounting=array.mounting,
        surface_tilt_deg=array.surface_tilt_deg,
        surface_azimuth_deg=array.surface_azimuth_deg,
        noct_c=array.noct_c,
        albedo=array.albedo,
        parameter_source=array.parameter_source,
        source_notes=array.source_notes,
        angular_response=_summarise_angular(array.angular_response),
    )


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
        array=get_active_array(),
        notes=notes,
        weather_provider=series.provider,
        weather_kind=series.kind,
        weather_retrieved_at_utc=series.retrieved_at_utc,
    )
