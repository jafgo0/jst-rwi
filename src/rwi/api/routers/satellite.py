"""Satellite cloud intelligence endpoints.

What these answer that nothing else in the platform can: is a cloud band about
to cross the car, and when. A numerical model on a 9-25 km grid refreshed every
six hours cannot see that; 0.5 km imagery every ten minutes can.

What they must never imply: that any of it is measured at ground level, or that
its accuracy is known. A satellite irradiance is a retrieval, and this platform
has no ground truth to check it against, so every response carries that.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from rwi.api.capability import require_attr
from rwi.api.deps import LatQuery, LonQuery, resolve_point
from rwi.config import Settings, get_settings
from rwi.contracts import CloudMotion, IrradianceNowcast, SatelliteTile
from rwi.core.state import StateStore, get_state_store
from rwi.core.timebase import utc_now

router = APIRouter(prefix="/api/satellite", tags=["satellite"])

StoreDep = Annotated[StateStore, Depends(get_state_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

MAX_FIELD_CELLS = 128
"""Hard cap on the downsampled grid returned as JSON.

A 0.5 km tile is 2200x2200 pixels; serialising that would be roughly 20 MB of
numbers per request. The field endpoint exists to show a strategist where the
cloud is, not to ship the raster.
"""
DEFAULT_REGION_KM = 300.0


class SatelliteStatus(BaseModel):
    """Whether the imagery is reachable, how fresh it is, what it costs."""

    available: bool
    satellite: str | None = None
    latest_slot_utc: datetime | None = None
    image_age_s: float | None = Field(
        default=None, description="how old the latest image is NOW, not its nominal latency"
    )
    cadence_s: float | None = None
    tiles_for_route: list[SatelliteTile] = Field(default_factory=list)
    bytes_per_slot: int | None = Field(
        default=None, description="download cost per 10-minute slot for the route's tiles"
    )
    attribution: str | None = None
    message: str | None = None
    caveat: str = (
        "Satellite irradiance is a retrieval, not a ground measurement, and no "
        "ground-truth irradiance exists in this platform, so its error is unknown."
    )


class CloudFieldResponse(BaseModel):
    """A downsampled cloud-index field for display."""

    slot_time_utc: datetime
    image_age_s: float
    bounds: tuple[float, float, float, float] = Field(
        description="(min_lat, min_lon, max_lat, max_lon)"
    )
    shape: tuple[int, int]
    cloud_index: list[list[float | None]] = Field(
        description="0 = clear ground, 1 = thickest cloud; null where unavailable"
    )
    usable: bool = Field(description="false when the sun is too low for a visible-band product")
    unusable_reason: str | None = None
    motion: CloudMotion | None = None
    satellite: str
    band: str
    attribution: str | None = None


def _satellite(name: str, attribute: str) -> Any:
    return require_attr(f"rwi.satellite.{name}", attribute, capability="satellite imagery")


@router.get("/status", response_model=SatelliteStatus)
async def read_status(store: StoreDep, settings: SettingsDep) -> Any:
    """Freshness and cost of the imagery, or a plain statement that it is absent."""
    try:
        latest_slot = _satellite("source", "latest_slot")
        tiles_covering = _satellite("geo", "tiles_covering")
    except HTTPException:
        raise
    except Exception as exc:
        return SatelliteStatus(available=False, message=f"{type(exc).__name__}: {exc}")

    now = utc_now()
    try:
        slot = latest_slot(now=now)
    except Exception as exc:
        return SatelliteStatus(
            available=False,
            message=f"imagery source unreachable: {type(exc).__name__}: {exc}",
        )
    if slot is None:
        return SatelliteStatus(available=False, message="no imagery slot published yet today")

    tiles: list[Any] = []
    try:
        fix = store.state.fix
        if fix is not None:
            half = DEFAULT_REGION_KM / 111.0 / 2.0
            bounds = (
                fix.point.latitude_deg - half,
                fix.point.longitude_deg - half,
                fix.point.latitude_deg + half,
                fix.point.longitude_deg + half,
            )
            tiles = list(tiles_covering(bounds, slot))
    except Exception:
        tiles = []

    return SatelliteStatus(
        available=True,
        satellite=tiles[0].satellite if tiles else "himawari-9",
        latest_slot_utc=slot,
        image_age_s=(now - slot).total_seconds(),
        cadence_s=600.0,
        tiles_for_route=tiles,
        bytes_per_slot=sum(t.size_bytes or 0 for t in tiles) or None,
        attribution=_attribution(),
    )


def _attribution() -> str | None:
    """The credit the data licence requires, taken from the module that knows it.

    Missing attribution is a licence problem, not a crash, so this degrades to
    None and the UI shows the source without a credit line rather than failing.
    """
    try:
        import rwi.satellite.source as source

        value = getattr(source, "ATTRIBUTION", None)
        return str(value) if value else None
    except Exception:
        return None


@router.get("/field", response_model=CloudFieldResponse)
async def read_cloud_field(
    store: StoreDep,
    settings: SettingsDep,
    lat: Annotated[float | None, LatQuery] = None,
    lon: Annotated[float | None, LonQuery] = None,
    elev: Annotated[float | None, Query()] = None,
    region_km: Annotated[float, Query(gt=10.0, le=1500.0)] = DEFAULT_REGION_KM,
    cells: Annotated[int, Query(ge=16, le=MAX_FIELD_CELLS)] = 64,
) -> Any:
    """The cloud field around a point, downsampled for display."""
    import numpy as np

    load_scene = _satellite("scene", "load_scene")
    latest_slot = _satellite("source", "latest_slot")
    cloud_index = _satellite("cloudindex", "cloud_index")

    point = resolve_point(lat, lon, elev, store)
    now = utc_now()
    slot = latest_slot(now=now)
    if slot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="no satellite imagery slot is published for today",
        )
    half = region_km / 111.0 / 2.0
    bounds = (
        point.latitude_deg - half,
        point.longitude_deg - half,
        point.latitude_deg + half,
        point.longitude_deg + half,
    )
    values, scene = load_scene(bounds, slot, cache_dir=settings.cache_dir / "satellite")

    if not scene.is_usable_visible:
        return CloudFieldResponse(
            slot_time_utc=scene.slot_time_utc,
            image_age_s=(now - scene.slot_time_utc).total_seconds(),
            bounds=scene.grid.bounds,
            shape=(0, 0),
            cloud_index=[],
            usable=False,
            unusable_reason=(
                "the sun is too low for a visible-band product: a dark image here means "
                "night, not clear sky"
            ),
            satellite=scene.satellite,
            band=scene.band,
        )

    index = np.asarray(cloud_index(values), dtype=float)
    rows = max(1, index.shape[0] // cells)
    cols = max(1, index.shape[1] // cells)
    trimmed = index[: (index.shape[0] // rows) * rows, : (index.shape[1] // cols) * cols]
    coarse = np.nanmean(
        trimmed.reshape(trimmed.shape[0] // rows, rows, trimmed.shape[1] // cols, cols),
        axis=(1, 3),
    )
    grid = [[None if not np.isfinite(v) else round(float(v), 4) for v in row] for row in coarse]
    return CloudFieldResponse(
        slot_time_utc=scene.slot_time_utc,
        image_age_s=(now - scene.slot_time_utc).total_seconds(),
        bounds=scene.grid.bounds,
        shape=(coarse.shape[0], coarse.shape[1]),
        cloud_index=grid,
        usable=True,
        satellite=scene.satellite,
        band=scene.band,
    )


@router.get("/nowcast", response_model=IrradianceNowcast)
async def read_nowcast(
    store: StoreDep,
    settings: SettingsDep,
    lat: Annotated[float | None, LatQuery] = None,
    lon: Annotated[float | None, LonQuery] = None,
    elev: Annotated[float | None, Query()] = None,
    horizon_min: Annotated[float, Query(gt=0.0, le=180.0)] = 60.0,
    step_min: Annotated[float, Query(ge=5.0, le=60.0)] = 10.0,
) -> Any:
    """Short-term irradiance outlook from cloud motion.

    The response carries its own `validation_status` and `limitations`; a client
    that renders the numbers without them is misrepresenting it.
    """
    import numpy as np

    load_scene = _satellite("scene", "load_scene")
    latest_slot = _satellite("source", "latest_slot")
    nowcast_point = _satellite("nowcast", "nowcast_point")
    clear_sky_series = require_attr(
        "rwi.solar.clearsky", "clear_sky_series", capability="clear-sky irradiance"
    )

    point = resolve_point(lat, lon, elev, store)
    now = utc_now()
    slot = latest_slot(now=now)
    if slot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="no satellite imagery slot is published for today",
        )

    half = DEFAULT_REGION_KM / 111.0 / 2.0
    bounds = (
        point.latitude_deg - half,
        point.longitude_deg - half,
        point.latitude_deg + half,
        point.longitude_deg + half,
    )
    cache = settings.cache_dir / "satellite"
    scenes: list[Any] = []
    arrays: list[Any] = []
    # Two consecutive slots: motion needs a pair, and the earlier one is what
    # makes this a nowcast rather than a snapshot.
    for offset in (1, 0):
        slot_time = slot - timedelta(minutes=10 * offset)
        try:
            values, scene = load_scene(bounds, slot_time, cache_dir=cache)
        except Exception:
            continue
        scenes.append(scene)
        arrays.append(np.asarray(values, dtype=float))

    if not scenes:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="no usable satellite scene could be loaded for this point",
        )

    steps = int(horizon_min // step_min)
    times = [now + timedelta(minutes=step_min * i) for i in range(steps + 1)]
    clear_sky = clear_sky_series(point, times)
    return nowcast_point(
        point,
        scenes,
        arrays,
        horizon_s=horizon_min * 60.0,
        step_s=step_min * 60.0,
        clear_sky=clear_sky,
    )
