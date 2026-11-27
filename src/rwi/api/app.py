"""FastAPI application.

Design notes:
  * every endpoint reads the single authoritative position/time state, or takes
    an explicit coordinate - there is no default location anywhere;
  * a feature area whose module is absent answers 501 naming what is missing,
    instead of returning a plausible-looking placeholder;
  * /api/health reports what the platform can currently do, so the dashboard can
    disable a view rather than show an empty one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.responses import Response
from starlette.types import Scope

from rwi import __version__
from rwi.api.capability import capability_status, import_errors, require_attr
from rwi.api.errors import register_error_handlers
from rwi.api.product import ProductVersionMiddleware
from rwi.api.routers import (
    alerts,
    camp,
    climatology,
    energy,
    forecast_quality,
    map,
    meteorologist,
    ml,
    route,
    satellite,
    solar,
    state,
    strategy,
    v1,
    validation,
    weather,
)
from rwi.config import Settings, get_settings
from rwi.contracts import ProviderHealth
from rwi.core.timebase import utc_now

_STARTED_AT = utc_now()

SettingsDep = Annotated[Settings, Depends(get_settings)]


class HealthResponse(BaseModel):
    status: str
    version: str
    server_time_utc: datetime
    uptime_s: float
    capabilities: dict[str, bool]
    unavailable_modules: dict[str, str]
    providers: list[ProviderHealth]
    provider_errors: dict[str, str]
    network_enabled: bool
    freshness_limits_s: dict[str, float]


health_router = APIRouter(tags=["health"])


@health_router.get("/api/health", response_model=HealthResponse)
async def health(settings: SettingsDep) -> Any:
    capabilities = capability_status()
    providers: list[ProviderHealth] = []
    errors: dict[str, str] = {}
    if capabilities.get("providers"):
        try:
            available: Any = require_attr(
                "rwi.providers.registry", "available_providers", capability="weather providers"
            )
            for name, provider in available(settings).items():
                try:
                    providers.append(await provider.health())
                except Exception as exc:
                    errors[name] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            errors["registry"] = f"{type(exc).__name__}: {exc}"
    degraded = not all(capabilities.values()) or bool(errors)
    return HealthResponse(
        status="degraded" if degraded else "ok",
        version=__version__,
        server_time_utc=utc_now(),
        uptime_s=(utc_now() - _STARTED_AT).total_seconds(),
        capabilities=capabilities,
        unavailable_modules=import_errors(),
        providers=providers,
        provider_errors=errors,
        network_enabled=settings.allow_network,
        # The UI must mark data stale at the same limits the backend uses;
        # hardcoding them in the dashboard would let the two drift apart.
        freshness_limits_s={
            "observation": settings.max_observation_age_s,
            "forecast": settings.max_forecast_age_s,
            "gps": settings.max_gps_age_s,
        },
    )


@health_router.get("/api/regulations")
async def regulations() -> dict[str, Any]:
    """The verified BWSC 2027 limits the platform enforces, with clause numbers.

    Includes what the regulations do NOT yet publish and what they are silent
    about, so the UI can show the gaps rather than imply completeness.
    """
    from rwi.regulations import summary

    return summary()


def _log_onboard_exit(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        logging.getLogger(__name__).error(
            "on-board sensor ingest stopped", exc_info=task.exception()
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    onboard: asyncio.Task[None] | None = None
    if settings.onboard_sensors_enabled:
        # Built before the task starts so a bad sensor config fails startup (ADR 0010).
        from rwi.core.state import get_state_store
        from rwi.telemetry.ingest import OnboardConfig, run_live

        config = OnboardConfig.from_settings(settings)
        onboard = asyncio.create_task(run_live(settings, get_state_store(), config))
        onboard.add_done_callback(_log_onboard_exit)
    try:
        yield
    finally:
        if onboard is not None:
            onboard.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await onboard


def create_app() -> FastAPI:
    app = FastAPI(
        title="JST-RWI - Race Weather Intelligence",
        version=__version__,
        summary="Meteorological and race decision-support platform for JU Solar Team, BWSC 2027",
        description=(
            "Every numeric payload carries provenance: provider, model, valid time, "
            "position, unit and quality. Observations, forecasts and physical-model "
            "output are distinct kinds and are never substituted for one another."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
        expose_headers=["X-Rwi-Product-Version", "X-Rwi-Product-Tier"],
    )
    app.add_middleware(ProductVersionMiddleware, version=__version__)
    register_error_handlers(app)
    app.include_router(health_router)
    app.include_router(state.router)
    app.include_router(solar.router)
    app.include_router(weather.router)
    app.include_router(route.router)
    app.include_router(energy.router)
    app.include_router(strategy.router)
    app.include_router(forecast_quality.router)
    app.include_router(satellite.router)
    app.include_router(validation.router)
    app.include_router(alerts.router)
    app.include_router(meteorologist.router)
    app.include_router(ml.router)
    app.include_router(v1.router)
    app.include_router(map.router)
    app.include_router(camp.router)
    app.include_router(climatology.router)

    web_dir = Path(__file__).resolve().parents[3] / "web"
    if web_dir.is_dir():
        app.mount("/", _RevalidatingStaticFiles(directory=web_dir, html=True), name="dashboard")
    return app


class _RevalidatingStaticFiles(StaticFiles):
    """Dashboard files served with ``Cache-Control: no-cache``.

    Without it browsers (Safari in particular) heuristically cache ES modules and
    keep running a stale dashboard after a deploy. ``no-cache`` still allows the
    cached copy, but only after an ETag revalidation (a cheap 304).
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app = create_app()
