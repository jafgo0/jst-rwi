"""Operational alerts, assembled from whatever the platform can actually see."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from rwi.alerts import data_integrity_alerts, sort_alerts, weather_alerts
from rwi.api.capability import require_attr
from rwi.api.deps import resolve_point
from rwi.config import Settings, get_settings
from rwi.contracts import Alert
from rwi.core.errors import PositionUnavailableError, RwiError
from rwi.core.provenance import DataKind
from rwi.core.state import StateStore, get_state_store
from rwi.core.timebase import TimeWindow, utc_now

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

StoreDep = Annotated[StateStore, Depends(get_state_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

ALERT_VARIABLES = (
    "temperature_2m",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "precipitation",
    "precipitation_probability",
    "visibility",
    "ghi",
)


@router.get("", response_model=list[Alert])
async def read_alerts(
    store: StoreDep,
    settings: SettingsDep,
    hours: Annotated[float, Query(gt=0.0, le=48.0)] = 6.0,
) -> Any:
    now = utc_now()
    fix = store.state.fix
    series: list[Any] = []
    health: list[Any] = []

    try:
        available: Any = require_attr(
            "rwi.providers.registry", "available_providers", capability="weather providers"
        )
        fetch_with_fallback: Any = require_attr(
            "rwi.providers.registry", "fetch_with_fallback", capability="weather providers"
        )
        providers = available(settings)
        health = [await provider.health() for provider in providers.values()]
        point = resolve_point(None, None, None, store)
        window = TimeWindow(start_utc=now, end_utc=now + timedelta(hours=hours))
        series = [
            await fetch_with_fallback(
                providers, point, window, ALERT_VARIABLES, kind=DataKind.FORECAST
            )
        ]
    except (PositionUnavailableError, RwiError):
        series = []
    except Exception:
        series = []

    alerts: list[Alert] = []
    for item in series:
        alerts.extend(weather_alerts(item, now=now, horizon_s=hours * 3600.0))
    alerts.extend(
        data_integrity_alerts(series=series, fix=fix, provider_health=health, now=now)
    )
    return sort_alerts(alerts)
