"""Gridded forecast fields for the map view (not part of the stable product surface).

``GET /api/map/grid?model=aifs|ifs`` returns one regular lat/lon grid covering
the route (bbox derived from the route waypoints, never hardcoded), with GHI,
cloud cover, 10 m wind and 2 m temperature at each valid time.

* ``aifs`` - native ECMWF AIFS from the capture worker's GRIB cache
  (``cache_dir/aifs-capture``). The API never downloads; no file means
  ``available: false`` with the reason. Native 6-hourly valid times, no temporal
  interpolation. GHI is the preceding-hour mean reconstructed from the
  de-accumulated SSRD with the clear-sky-index method; Sun down all hour => 0.0.
* ``ifs`` - ECMWF IFS 0.25 deg through Open-Meteo (multi-location request),
  hourly, cached in memory per 6-hourly run slot. GHI is Open-Meteo's
  preceding-hour mean shortwave radiation, forced to exactly 0.0 when the Sun
  is below the horizon for the whole hour (pvlib SPA).

Every value is model output (``DataKind.FORECAST``), never an observation.
"""

import asyncio
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
import numpy as np
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from rwi.api.deps import RouteRepository, get_route_repository
from rwi.config import Settings, get_settings
from rwi.contracts import GeoPoint
from rwi.core.errors import ProviderUnavailableError
from rwi.core.provenance import DataKind
from rwi.core.timebase import ensure_utc, utc_now
from rwi.providers.gridded import (
    BBox,
    GriddedRun,
    bilinear_weights,
    deaccumulate,
    hourly_ghi_from_interval_means,
    wind_from_uv,
)
from rwi.solar.geometry import solar_positions

router = APIRouter(prefix="/api/map", tags=["map"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
RepoDep = Annotated[RouteRepository, Depends(get_route_repository)]

MapModel = Literal["aifs", "ifs"]
FIELDS: tuple[str, ...] = (
    "ghi",
    "cloud_cover",
    "wind_speed_kmh",
    "wind_direction_deg",
    "temperature_c",
)
UNITS: dict[str, str] = {
    "ghi": "W/m2 (preceding-hour mean)",
    "cloud_cover": "%",
    "wind_speed_kmh": "km/h",
    "wind_direction_deg": "deg (blowing from, clockwise from north)",
    "temperature_c": "degC",
}
#: Margin around the route, degrees.
_MARGIN_DEG = 1.0
_IFS_MODEL = "ecmwf_ifs025"
_IFS_HOURS = 72
_IFS_VARS = (
    "shortwave_radiation",
    "cloud_cover",
    "wind_speed_10m",
    "wind_direction_10m",
    "temperature_2m",
)
_AIFS_SUBDIR = "aifs-capture"


class GridProvenance(BaseModel):
    """Where every number in a `MapGrid` came from."""

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    provider: str
    dataset: str
    model_name: str
    model_version: str | None
    kind: DataKind
    init_time_utc: datetime | None
    retrieved_at_utc: datetime
    grid_step_deg: float
    temporal_step_h: float
    license: str | None
    source_url: str | None
    notes: list[str]


class MapGrid(BaseModel):
    """Regular grid of forecast fields; ``fields[name][t][i_lat][j_lon]``, null = missing."""

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    available: bool
    model: MapModel
    message: str | None = None
    lats: list[float] = []
    lons: list[float] = []
    valid_times_utc: list[datetime] = []
    fields: dict[str, list[list[list[float | None]]]] = {}
    units: dict[str, str] = UNITS
    provenance: GridProvenance | None = None


# --------------------------------------------------------------------------- grid


def route_bbox(repo: RouteRepository, route_id: str | None) -> BBox:
    """Bbox of the route waypoints (all routes when ``route_id`` is None) plus a margin."""
    routes = [repo.get(route_id)] if route_id else list(repo.all().values())
    points = [w.point for r in routes for w in r.waypoints]
    if not points:
        raise ProviderUnavailableError("no route waypoints to derive the map area from")
    return BBox.around(points, margin_deg=_MARGIN_DEG)


def grid_axes(bbox: BBox, step_deg: float) -> tuple[list[float], list[float]]:
    """Node latitudes/longitudes on multiples of ``step_deg`` inside ``bbox`` (snapped inward)."""

    def axis(lo: float, hi: float) -> list[float]:
        a = math.ceil(lo / step_deg - 1e-9)
        b = math.floor(hi / step_deg + 1e-9)
        return [round(k * step_deg, 6) for k in range(a, b + 1)]

    return axis(bbox.south, bbox.north), axis(bbox.west, bbox.east)


def _clean(a: np.ndarray, ndigits: int = 1) -> list[Any]:
    """Nested lists with NaN -> None, rounded."""
    out: list[Any] = np.where(np.isfinite(a), np.round(a, ndigits), np.nan).tolist()

    def fix(x: Any) -> Any:
        if isinstance(x, list):
            return [fix(v) for v in x]
        return None if isinstance(x, float) and math.isnan(x) else x

    return [fix(v) for v in out]


def _empty(model: MapModel, message: str) -> MapGrid:
    return MapGrid(available=False, model=model, message=message)


# --------------------------------------------------------------------------- AIFS

_aifs_cache: dict[tuple[str, float, float, tuple[float, ...]], MapGrid] = {}


def newest_aifs_grib(cache_dir: Path) -> Path | None:
    """Newest complete AIFS GRIB written by the capture worker, if any."""
    files = sorted((cache_dir / _AIFS_SUBDIR).glob("aifs_*.grib2"))
    return files[-1] if files else None


def _decode_aifs(path: Path, bbox: BBox) -> GriddedRun:
    from rwi.providers.ecmwf_aifs import run_from_grib

    retrieved = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return run_from_grib([path], bbox, retrieved_at_utc=retrieved)


def aifs_grid(run: GriddedRun, lats: list[float], lons: list[float], step: float) -> MapGrid:
    """Sample an AIFS run at the map nodes (native valid times, GHI via clear-sky index)."""
    nt, ny, nx = len(run.valid_times_utc), len(lats), len(lons)
    out = {f: np.full((nt, ny, nx), np.nan) for f in FIELDS}
    src_lat = np.asarray(run.latitudes, dtype=float)
    src_lon = np.asarray(run.longitudes, dtype=float)
    lat_order = np.argsort(src_lat)
    s_lat = src_lat[lat_order]
    fields = {k: np.asarray(v, dtype=float)[:, lat_order, :] for k, v in run.fields.items()}
    times = list(run.valid_times_utc)
    starts = [run.init_time_utc, *times[:-1]]
    secs = np.array([(e - s).total_seconds() for s, e in zip(starts, times, strict=True)])

    def sample(name: str, w: tuple[tuple[int, int, float], tuple[int, int, float]]) -> np.ndarray:
        (i0, i1, wy), (j0, j1, wx) = w
        a = fields[name]
        top = a[:, i0, j0] * (1 - wx) + a[:, i0, j1] * wx
        bot = a[:, i1, j0] * (1 - wx) + a[:, i1, j1] * wx
        return np.asarray(top * (1 - wy) + bot * wy, dtype=float)

    for i, la in enumerate(lats):
        for j, lo in enumerate(lons):
            try:
                w = bilinear_weights(s_lat, src_lon, la, lo)
            except ValueError:
                continue
            if "cloud_cover" in fields:
                out["cloud_cover"][:, i, j] = np.clip(sample("cloud_cover", w), 0.0, 100.0)
            if "temperature_2m" in fields:
                out["temperature_c"][:, i, j] = sample("temperature_2m", w)
            if "wind_u_10m" in fields and "wind_v_10m" in fields:
                spd, dirn = wind_from_uv(sample("wind_u_10m", w), sample("wind_v_10m", w))
                out["wind_speed_kmh"][:, i, j] = spd * 3.6
                out["wind_direction_deg"][:, i, j] = dirn
            if "ssrd_accum" in fields:
                with np.errstate(invalid="ignore", divide="ignore"):
                    means = deaccumulate(sample("ssrd_accum", w), times, run.init_time_utc) / secs
                point = GeoPoint(latitude_deg=la, longitude_deg=lo)
                hours, ghi = hourly_ghi_from_interval_means(point, times, run.init_time_utc, means)
                by_hour = dict(zip(hours, ghi, strict=True))
                out["ghi"][:, i, j] = [by_hour.get(t, np.nan) for t in times]
    notes = [
        "native AIFS 6-hourly valid times; nothing is interpolated in time",
        "fields bilinearly interpolated from the 0.25 deg grid to the map nodes",
        "GHI = preceding-hour mean reconstructed from de-accumulated SSRD "
        "(clear-sky-index method); exactly 0 when the Sun is down all hour",
        "read from the capture worker's GRIB cache; the API itself never downloads",
    ]
    if "ssrd_accum" not in fields:
        notes.append("SSRD missing from the GRIB file: GHI unavailable")
    return MapGrid(
        available=True,
        model="aifs",
        lats=lats,
        lons=lons,
        valid_times_utc=times,
        fields={f: _clean(out[f], 0 if f == "wind_direction_deg" else 1) for f in FIELDS},
        provenance=GridProvenance(
            provider=run.provider,
            dataset=run.dataset,
            model_name=run.model_name,
            model_version=run.model_version,
            kind=DataKind.FORECAST,
            init_time_utc=run.init_time_utc,
            retrieved_at_utc=run.retrieved_at_utc,
            grid_step_deg=step,
            temporal_step_h=6.0,
            license=run.license,
            source_url=run.source_url,
            notes=notes,
        ),
    )


async def _aifs(settings: Settings, bbox: BBox, step: float) -> MapGrid:
    path = newest_aifs_grib(Path(settings.cache_dir))
    if path is None:
        return _empty(
            "aifs",
            "no AIFS GRIB in the capture cache yet - the capture worker with "
            "RWI_CAPTURE_AIFS=true writes one every 6-hourly run",
        )
    key = (path.name, path.stat().st_mtime, step, (bbox.south, bbox.north, bbox.west, bbox.east))
    if key in _aifs_cache:
        return _aifs_cache[key]
    lats, lons = grid_axes(bbox, step)
    try:
        run = await asyncio.to_thread(_decode_aifs, path, bbox)
        grid = await asyncio.to_thread(aifs_grid, run, lats, lons, step)
    except ProviderUnavailableError as exc:
        return _empty("aifs", str(exc))
    _aifs_cache.clear()
    _aifs_cache[key] = grid
    return grid


# --------------------------------------------------------------------------- IFS

_ifs_cache: dict[tuple[datetime, float, tuple[float, ...]], MapGrid] = {}


def _run_slot(now: datetime) -> datetime:
    """Start of the current 6-hourly slot; the IFS cache is refreshed once per slot."""
    t = ensure_utc(now)
    return t.replace(hour=t.hour - t.hour % 6, minute=0, second=0, microsecond=0)


def sun_down_all_hour(point: GeoPoint, hour_ends: list[datetime]) -> np.ndarray:
    """True where the apparent Sun is below the horizon at the start, middle and end of the hour."""
    probes = [e - timedelta(minutes=m) for e in hour_ends for m in (60, 30, 0)]
    elev = np.array([p.apparent_elevation_deg for p in solar_positions(point, probes)])
    return np.asarray((elev.reshape(len(hour_ends), 3) <= 0.0).all(axis=1), dtype=bool)


def ifs_grid(
    payload: list[dict[str, Any]],
    lats: list[float],
    lons: list[float],
    step: float,
    *,
    retrieved_at_utc: datetime,
    init_time_utc: datetime | None,
    source_url: str,
) -> MapGrid:
    """Build the map grid from an Open-Meteo multi-location response (row-major nodes)."""
    ny, nx = len(lats), len(lons)
    if len(payload) != ny * nx:
        raise ProviderUnavailableError(
            f"open-meteo returned {len(payload)} locations for {ny * nx} requested"
        )
    times = [
        datetime.fromisoformat(t).replace(tzinfo=UTC)
        for t in payload[0].get("hourly", {}).get("time", [])
    ]
    nt = len(times)
    names = {
        "ghi": "shortwave_radiation",
        "cloud_cover": "cloud_cover",
        "wind_speed_kmh": "wind_speed_10m",
        "wind_direction_deg": "wind_direction_10m",
        "temperature_c": "temperature_2m",
    }
    out = {f: np.full((nt, ny, nx), np.nan) for f in FIELDS}
    for k, loc in enumerate(payload):
        i, j = divmod(k, nx)
        hourly = loc.get("hourly", {})
        for f, src in names.items():
            vals = hourly.get(src) or []
            arr = np.array([np.nan if v is None else float(v) for v in vals[:nt]], dtype=float)
            out[f][: arr.size, i, j] = arr
        night = sun_down_all_hour(GeoPoint(latitude_deg=lats[i], longitude_deg=lons[j]), times)
        out["ghi"][night, i, j] = 0.0
    return MapGrid(
        available=True,
        model="ifs",
        lats=lats,
        lons=lons,
        valid_times_utc=times,
        fields={f: _clean(out[f], 0 if f == "wind_direction_deg" else 1) for f in FIELDS},
        provenance=GridProvenance(
            provider="open-meteo",
            dataset="forecast",
            model_name=_IFS_MODEL,
            model_version=None,
            kind=DataKind.FORECAST,
            init_time_utc=init_time_utc,
            retrieved_at_utc=retrieved_at_utc,
            grid_step_deg=step,
            temporal_step_h=1.0,
            license="CC-BY-4.0 (Open-Meteo; ECMWF data)",
            source_url=source_url,
            notes=[
                "Open-Meteo values at the map nodes (provider-side interpolation from 0.25 deg)",
                "GHI = Open-Meteo shortwave_radiation, preceding-hour mean; forced to exactly "
                "0 when the Sun is below the horizon all hour (pvlib SPA)",
                "init_time_utc from the Open-Meteo Metadata API when it can be asserted",
            ],
        ),
    )


async def _ifs_init_time(client: httpx.AsyncClient, settings: Settings) -> datetime | None:
    """Latest published IFS run from the Open-Meteo Metadata API, or None if unsure."""
    from urllib.parse import urlsplit

    parts = urlsplit(settings.openmeteo_base_url)
    url = f"{parts.scheme}://{parts.netloc}/data/{_IFS_MODEL}/static/meta.json"
    try:
        r = await client.get(url)
        r.raise_for_status()
        meta = r.json()
        init = datetime.fromtimestamp(float(meta["last_run_initialisation_time"]), tz=UTC)
        avail = datetime.fromtimestamp(float(meta["last_run_availability_time"]), tz=UTC)
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return None
    # same 10-minute safety margin as rwi.providers.openmeteo
    return init if (utc_now() - avail).total_seconds() >= 600 else None


async def _ifs(settings: Settings, bbox: BBox, step: float) -> MapGrid:
    slot = _run_slot(utc_now())
    key = (slot, step, (bbox.south, bbox.north, bbox.west, bbox.east))
    if key in _ifs_cache:
        return _ifs_cache[key]
    if not settings.allow_network:
        return _empty("ifs", "network access is disabled (RWI_ALLOW_NETWORK=false)")
    lats, lons = grid_axes(bbox, step)
    nodes = [(la, lo) for la in lats for lo in lons]
    url = settings.openmeteo_base_url.rstrip("/") + "/forecast"
    params: dict[str, Any] = {
        "latitude": ",".join(f"{la:g}" for la, _ in nodes),
        "longitude": ",".join(f"{lo:g}" for _, lo in nodes),
        "models": _IFS_MODEL,
        "hourly": ",".join(_IFS_VARS),
        "forecast_hours": _IFS_HOURS,
        "timezone": "GMT",
        "wind_speed_unit": "kmh",
    }
    host = url.split("//", 1)[-1]
    if settings.openmeteo_api_key and host.startswith("customer-"):
        params["apikey"] = settings.openmeteo_api_key
    try:
        async with httpx.AsyncClient(timeout=max(settings.http_timeout_s, 30.0)) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            payload = r.json()
            init = await _ifs_init_time(client, settings)
    except (httpx.HTTPError, ValueError) as exc:
        return _empty("ifs", f"open-meteo request failed: {exc}")
    if isinstance(payload, dict):
        payload = [payload]
    try:
        grid = await asyncio.to_thread(
            ifs_grid,
            payload,
            lats,
            lons,
            step,
            retrieved_at_utc=utc_now(),
            init_time_utc=init,
            source_url=url,
        )
    except ProviderUnavailableError as exc:
        return _empty("ifs", str(exc))
    _ifs_cache.clear()
    _ifs_cache[key] = grid
    return grid


# --------------------------------------------------------------------------- route


@router.get("/grid", response_model=MapGrid)
async def read_grid(
    settings: SettingsDep,
    repo: RepoDep,
    model: Annotated[MapModel, Query()] = "aifs",
    route_id: Annotated[str | None, Query()] = None,
    step_deg: Annotated[float, Query(ge=0.5, le=2.0)] = 1.0,
) -> MapGrid:
    """Forecast grid over the route for the map (FORECAST provenance; never an observation)."""
    try:
        bbox = route_bbox(repo, route_id)
    except ProviderUnavailableError as exc:
        return _empty(model, str(exc))
    if model == "aifs":
        return await _aifs(settings, bbox, step_deg)
    return await _ifs(settings, bbox, step_deg)
