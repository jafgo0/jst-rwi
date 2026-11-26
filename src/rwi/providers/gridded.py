"""Gridded model runs (AIFS today; Aurora/GraphCast later) behind `WeatherProvider`.

ADR 0008 §1. A `GriddedSource` downloads one model run cropped to a caller-given
`BBox` and hands back a `GriddedRun`: plain numpy arrays in a small, fixed field
vocabulary (`GRID_FIELDS`) with units already converted by the source.
`GriddedProvider` turns that run into an hourly `WeatherSeries` at a point:

* **Space** - bilinear interpolation between the four surrounding grid nodes
  (``processing="bilinear_grid_interp"``). A point outside the run's grid is
  refused; nothing is extrapolated. Any NaN corner makes the value MISSING.
* **Time, instantaneous fields** - linear between native steps; values at a
  native step are not interpolated. Wind is interpolated as u/v and only then
  converted to speed and "from" direction.
* **Accumulated fields** - de-accumulated per native interval. Precipitation is
  spread uniformly over the interval's hours (labelled, it is a disaggregation).
* **GHI** - the interval-mean GHI from ``ssrd`` is turned into a clear-sky index
  ``k = GHI_mean / GHI_cs_mean`` (pvlib Ineichen clear sky, ADR 0003), ``k`` is
  interpolated in time between interval midpoints, multiplied by the hourly
  clear-sky mean, and rescaled so each native interval keeps the model's energy.
  Hourly values are **preceding-hour means** (the Open-Meteo default). When the
  clear sky is 0 the whole hour (Sun below the horizon) GHI is exactly 0.0.
* **DNI** - from the hourly GHI with `rwi.solar.decomposition.decompose_ghi`
  (DIRINT, static part), geometry at the hour midpoint; exactly 0.0 whenever the
  Sun is below the horizon there (hard rule 5).

The bbox always comes from the caller (e.g. the active route's extent); this
module holds no coordinates (hard rule 4).
"""

import itertools
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, Self

import numpy as np

from rwi.config import Settings
from rwi.contracts import ProviderCapabilities, WeatherSeries, WeatherStep
from rwi.core.errors import CapabilityNotSupportedError, ProviderUnavailableError
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Measured, Provenance, QualityFlag
from rwi.core.timebase import TimeWindow, ensure_utc
from rwi.core.variables import unit_for
from rwi.providers.base import BaseProvider, ClockFn, ceil_hour, classify_value
from rwi.solar.clearsky import clear_sky_series
from rwi.solar.decomposition import decompose_ghi
from rwi.solar.geometry import SolarPosition, solar_positions

__all__ = [
    "ACCUMULATED_FIELDS",
    "GRID_FIELDS",
    "BBox",
    "GriddedProvider",
    "GriddedRun",
    "GriddedSource",
    "bilinear_weights",
    "deaccumulate",
    "hourly_ghi_from_interval_means",
    "wind_from_uv",
]

#: Field vocabulary a `GriddedSource` fills, with the unit it must deliver.
#: Accumulated fields are totals since the run's init time.
GRID_FIELDS: Mapping[str, str] = {
    "temperature_2m": "degC",
    "dew_point_2m": "degC",
    "pressure_msl": "Pa",
    "surface_pressure": "Pa",
    "wind_u_10m": "m/s",
    "wind_v_10m": "m/s",
    "cloud_cover": "percent",
    "cloud_cover_low": "percent",
    "cloud_cover_mid": "percent",
    "cloud_cover_high": "percent",
    "precipitation_accum": "mm",
    "ssrd_accum": "J/m2",
}
ACCUMULATED_FIELDS = frozenset({"precipitation_accum", "ssrd_accum"})

#: Grid fields that map one-to-one onto a canonical variable.
_DIRECT = (
    "temperature_2m",
    "dew_point_2m",
    "pressure_msl",
    "surface_pressure",
    "cloud_cover",
    "cloud_cover_low",
    "cloud_cover_mid",
    "cloud_cover_high",
)

#: Interval clear-sky mean [W/m2] below which the clear-sky index is undefined.
_MIN_CLEAR_SKY_MEAN_W_M2 = 10.0
#: Clear-sky sampling inside one hour (minutes) for the hourly mean.
_CS_SAMPLE_MIN = 10
#: De-accumulated totals more negative than this (relative units) are corrupt.
_NEGATIVE_ACCUM_TOLERANCE = 1e-3
_GHI_MAX = 1600.0


# ---------------------------------------------------------------------------
# Data types (provider-internal; not contract types)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BBox:
    """Latitude/longitude box [deg]; longitudes in [-180, 180], west < east."""

    south: float
    north: float
    west: float
    east: float

    def __post_init__(self) -> None:
        vals = (self.south, self.north, self.west, self.east)
        if not all(math.isfinite(v) for v in vals):
            raise ValueError(f"bbox must be finite, got {vals}")
        if not (-90.0 <= self.south < self.north <= 90.0):
            raise ValueError(f"bbox needs -90 <= south < north <= 90, got {vals}")
        if not (-180.0 <= self.west < self.east <= 180.0):
            raise ValueError(f"bbox needs -180 <= west < east <= 180, got {vals}")

    @classmethod
    def around(cls, points: Iterable[GeoPoint], margin_deg: float) -> Self:
        """Smallest box holding ``points``, widened by ``margin_deg`` on every side."""
        pts = list(points)
        if not pts:
            raise ValueError("BBox.around needs at least one point")
        if margin_deg < 0.0:
            raise ValueError("margin_deg must be >= 0")
        lats = [p.latitude_deg for p in pts]
        lons = [p.longitude_deg for p in pts]
        return cls(
            south=max(-90.0, min(lats) - margin_deg),
            north=min(90.0, max(lats) + margin_deg),
            west=max(-180.0, min(lons) - margin_deg),
            east=min(180.0, max(lons) + margin_deg),
        )


@dataclass(frozen=True)
class GriddedRun:
    """One model run cropped to a bbox. Arrays are (time, lat, lon)."""

    provider: str
    dataset: str
    kind: DataKind
    model_name: str
    model_version: str | None
    init_time_utc: datetime
    retrieved_at_utc: datetime
    valid_times_utc: tuple[datetime, ...]
    latitudes: np.ndarray
    longitudes: np.ndarray
    fields: Mapping[str, np.ndarray]
    spatial_resolution_km: float | None
    license: str
    source_url: str | None
    notes: str | None = None
    _lat_ascending: bool = field(init=False, repr=False, default=True)

    def __post_init__(self) -> None:
        if self.kind not in (DataKind.FORECAST, DataKind.SIMULATION):
            raise ValueError(f"a gridded run is a FORECAST or a SIMULATION, not {self.kind}")
        init = ensure_utc(self.init_time_utc)
        times = tuple(ensure_utc(t) for t in self.valid_times_utc)
        object.__setattr__(self, "init_time_utc", init)
        object.__setattr__(self, "retrieved_at_utc", ensure_utc(self.retrieved_at_utc))
        object.__setattr__(self, "valid_times_utc", times)
        if not times:
            raise ValueError("a gridded run needs at least one valid time")
        if any(b <= a for a, b in itertools.pairwise(times)):
            raise ValueError("valid_times_utc must be strictly increasing")
        if times[0] < init:
            raise ValueError("valid times must not precede the init time")
        lat = np.asarray(self.latitudes, dtype=float)
        lon = np.asarray(self.longitudes, dtype=float)
        if lat.ndim != 1 or lon.ndim != 1 or lat.size < 2 or lon.size < 2:
            raise ValueError("latitudes/longitudes must be 1-D with at least two nodes")
        dlat = np.diff(lat)
        if not (np.all(dlat > 0) or np.all(dlat < 0)):
            raise ValueError("latitudes must be strictly monotonic")
        if not np.all(np.diff(lon) > 0):
            raise ValueError("longitudes must be strictly increasing")
        object.__setattr__(self, "latitudes", lat)
        object.__setattr__(self, "longitudes", lon)
        object.__setattr__(self, "_lat_ascending", bool(dlat[0] > 0))
        shape = (len(times), lat.size, lon.size)
        clean: dict[str, np.ndarray] = {}
        for name, arr in self.fields.items():
            if name not in GRID_FIELDS:
                raise ValueError(f"unknown grid field {name!r}; allowed: {sorted(GRID_FIELDS)}")
            a = np.asarray(arr, dtype=float)
            if a.shape != shape:
                raise ValueError(f"field {name!r} has shape {a.shape}, expected {shape}")
            clean[name] = a
        object.__setattr__(self, "fields", clean)

    def covers(self, point: GeoPoint) -> bool:
        """True when ``point`` lies inside the grid (edges included)."""
        lat, lon = self.latitudes, self.longitudes
        return bool(
            min(lat[0], lat[-1]) <= point.latitude_deg <= max(lat[0], lat[-1])
            and lon[0] <= point.longitude_deg <= lon[-1]
        )

    def at_point(self, name: str, point: GeoPoint) -> np.ndarray:
        """Bilinear time series of field ``name`` at ``point`` (NaN where any corner is NaN)."""
        lats = self.latitudes if self._lat_ascending else self.latitudes[::-1]
        data = self.fields[name] if self._lat_ascending else self.fields[name][:, ::-1, :]
        (i0, i1, wi), (j0, j1, wj) = bilinear_weights(
            lats, self.longitudes, point.latitude_deg, point.longitude_deg
        )
        return np.asarray(
            (1 - wi) * (1 - wj) * data[:, i0, j0]
            + (1 - wi) * wj * data[:, i0, j1]
            + wi * (1 - wj) * data[:, i1, j0]
            + wi * wj * data[:, i1, j1],
            dtype=float,
        )


class GriddedSource(Protocol):
    """Downloads one model run cropped to a bbox. Implemented per model family."""

    @property
    def provider(self) -> str: ...

    @property
    def dataset(self) -> str: ...

    @property
    def fields(self) -> frozenset[str]:
        """Grid fields (`GRID_FIELDS` keys) this source can deliver."""
        ...

    @property
    def max_forecast_hours(self) -> float: ...

    @property
    def spatial_resolution_km(self) -> float: ...

    @property
    def license(self) -> str: ...

    @property
    def documentation_url(self) -> str: ...

    def latest_init_time(self, now: datetime) -> datetime:
        """Newest run expected to be fully published at ``now``."""
        ...

    async def fetch_run(
        self, init_time: datetime, steps_h: Sequence[int], fields: Sequence[str], bbox: BBox
    ) -> GriddedRun: ...


# ---------------------------------------------------------------------------
# Pure numerics
# ---------------------------------------------------------------------------


def bilinear_weights(
    lats: np.ndarray, lons: np.ndarray, lat: float, lon: float
) -> tuple[tuple[int, int, float], tuple[int, int, float]]:
    """Bracketing indices and weights for ascending ``lats``/``lons``; raises outside."""
    out = []
    for axis, x, name in ((lats, lat, "latitude"), (lons, lon, "longitude")):
        if not (axis[0] <= x <= axis[-1]):
            raise ValueError(f"{name} {x} is outside the grid [{axis[0]}, {axis[-1]}]")
        k = int(np.searchsorted(axis, x, side="right")) - 1
        k = min(max(k, 0), axis.size - 2)
        w = float((x - axis[k]) / (axis[k + 1] - axis[k]))
        out.append((k, k + 1, w))
    return out[0], out[1]


def deaccumulate(
    accum: np.ndarray, valid_times: Sequence[datetime], init_time: datetime
) -> np.ndarray:
    """Per-interval totals from run-cumulative values; interval i ends at valid_times[i].

    The first interval starts at ``init_time`` (accumulation 0 there). Tiny
    negative differences (packing noise) become 0; larger ones become NaN.
    A first valid time equal to the init time yields NaN for that zero-length step.
    """
    a = np.asarray(accum, dtype=float)
    prev = np.concatenate(([0.0], a[:-1]))
    diff = a - prev
    scale = np.maximum(np.abs(a), 1.0)
    bad = diff < -_NEGATIVE_ACCUM_TOLERANCE * scale
    diff = np.where(bad, np.nan, np.maximum(diff, 0.0))
    if ensure_utc(valid_times[0]) == ensure_utc(init_time):
        diff[0] = np.nan
    return np.asarray(diff, dtype=float)


def wind_from_uv(u: np.ndarray, v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(speed [m/s], direction the wind blows FROM [deg, 0-360) clockwise from north)."""
    speed = np.hypot(u, v)
    direction = np.mod(np.degrees(np.arctan2(-u, -v)), 360.0)
    direction = np.where(speed == 0.0, 0.0, direction)
    return np.asarray(speed, dtype=float), np.asarray(direction, dtype=float)


def _clear_sky_hour_means(point: GeoPoint, hour_ends: Sequence[datetime]) -> np.ndarray:
    """Preceding-hour mean clear-sky GHI [W/m2] for each hour end (pvlib Ineichen)."""
    n = 60 // _CS_SAMPLE_MIN
    samples = [
        end - timedelta(minutes=60) + timedelta(minutes=_CS_SAMPLE_MIN * (k + 0.5))
        for end in hour_ends
        for k in range(n)
    ]
    ghi = np.array([c.ghi_w_m2 for c in clear_sky_series(point, samples)], dtype=float)
    return np.asarray(ghi.reshape(len(hour_ends), n).mean(axis=1), dtype=float)


def hourly_ghi_from_interval_means(
    point: GeoPoint,
    interval_ends: Sequence[datetime],
    interval_start: datetime,
    interval_mean_ghi: np.ndarray,
) -> tuple[list[datetime], np.ndarray]:
    """Hourly preceding-hour-mean GHI [W/m2] from interval-mean GHI (clear-sky-index method).

    ``interval_ends`` are the native step ends (whole hours), the first interval
    starting at ``interval_start``. Returns (hour ends, values). Values are NaN
    where the interval mean is NaN and the Sun is up; exactly 0.0 for hours whose
    clear-sky mean is 0 (Sun below the horizon all hour).
    """
    ends = [ensure_utc(t) for t in interval_ends]
    starts = [ensure_utc(interval_start), *ends[:-1]]
    hours: list[datetime] = []
    owner: list[int] = []
    for i, (s, e) in enumerate(zip(starts, ends, strict=True)):
        h = ceil_hour(s + timedelta(minutes=1))
        while h <= e:
            hours.append(h)
            owner.append(i)
            h += timedelta(hours=1)
    if not hours:
        return [], np.array([], dtype=float)
    cs_hour = _clear_sky_hour_means(point, hours)
    own = np.array(owner)
    means = np.asarray(interval_mean_ghi, dtype=float)

    # interval clear-sky means and clear-sky index at each interval midpoint
    n_int = len(ends)
    cs_int = np.array([cs_hour[own == i].mean() if np.any(own == i) else 0.0 for i in range(n_int)])
    k_int = np.where(
        (cs_int >= _MIN_CLEAR_SKY_MEAN_W_M2) & np.isfinite(means),
        means / np.maximum(cs_int, 1e-9),
        np.nan,
    )
    mids = np.array(
        [(s.timestamp() + e.timestamp()) / 2 for s, e in zip(starts, ends, strict=True)]
    )
    hour_mid = np.array([h.timestamp() - 1800.0 for h in hours])
    ok = np.isfinite(k_int)
    k_hour = np.interp(hour_mid, mids[ok], k_int[ok]) if ok.any() else np.full(len(hours), np.nan)
    ghi = k_hour * cs_hour

    # keep each interval's energy: rescale its hours to the interval mean
    for i in range(n_int):
        sel = own == i
        if not np.isfinite(means[i]):
            ghi[sel] = np.nan
            continue
        current = float(np.nanmean(ghi[sel])) if np.any(np.isfinite(ghi[sel])) else 0.0
        if current > 0.0:
            ghi[sel] = ghi[sel] * (means[i] / current)
    ghi = np.where(cs_hour <= 0.0, 0.0, ghi)
    ghi = np.where(np.isfinite(ghi), np.clip(ghi, 0.0, _GHI_MAX), np.nan)
    return hours, np.asarray(ghi, dtype=float)


# ---------------------------------------------------------------------------
# Provider adapter
# ---------------------------------------------------------------------------


def _variables_for(fields: frozenset[str]) -> frozenset[str]:
    out = {f for f in _DIRECT if f in fields}
    if {"wind_u_10m", "wind_v_10m"} <= fields:
        out |= {"wind_speed_10m", "wind_direction_10m"}
    if "precipitation_accum" in fields:
        out.add("precipitation")
    if "ssrd_accum" in fields:
        out |= {"ghi", "dni"}
    return frozenset(out)


def _fields_for(variables: Sequence[str]) -> list[str]:
    need: list[str] = []
    for v in variables:
        if v in _DIRECT:
            need.append(v)
        elif v in ("wind_speed_10m", "wind_direction_10m"):
            need += ["wind_u_10m", "wind_v_10m"]
        elif v == "precipitation":
            need.append("precipitation_accum")
        elif v in ("ghi", "dni"):
            need.append("ssrd_accum")
    return list(dict.fromkeys(need))


def _time_interp(
    hours: Sequence[datetime], native: Sequence[datetime], values: np.ndarray
) -> np.ndarray:
    """Linear interpolation in time; NaN outside the native range or next to a NaN."""
    x = np.array([t.timestamp() for t in native])
    xq = np.array([t.timestamp() for t in hours])
    out = np.full(xq.shape, np.nan)
    for q, tq in enumerate(xq):
        k = int(np.searchsorted(x, tq, side="right")) - 1
        if k < 0 or tq > x[-1]:
            continue
        if tq == x[k]:
            out[q] = values[k]
            continue
        w = (tq - x[k]) / (x[k + 1] - x[k])
        out[q] = (1 - w) * values[k] + w * values[k + 1]
    return out


class GriddedProvider(BaseProvider):
    """`WeatherProvider` over any `GriddedSource`; serves the latest run inside a bbox."""

    def __init__(
        self,
        source: GriddedSource,
        bbox: BBox,
        settings: Settings | None = None,
        *,
        run_cache: dict[datetime, GriddedRun] | None = None,
        clock: ClockFn | None = None,
    ) -> None:
        super().__init__(settings, clock=clock)
        self.source = source
        self.bbox = bbox
        self.name = source.provider
        self._runs: dict[datetime, GriddedRun] = run_cache if run_cache is not None else {}

    @property
    def capabilities(self) -> ProviderCapabilities:
        s = self.source
        return ProviderCapabilities(
            provider=s.provider,
            datasets=(s.dataset,),
            supports_observations=False,
            supports_forecast=True,
            supports_ensemble=False,
            supports_archive=False,
            variables=_variables_for(s.fields),
            max_forecast_hours=s.max_forecast_hours,
            spatial_resolution_km=s.spatial_resolution_km,
            update_interval_s=6 * 3600.0,
            license=s.license,
            attribution=f"{s.provider} {s.dataset}",
            documentation_url=s.documentation_url,
            notes="gridded run cropped to the caller's bbox; bilinear point interpolation",
        )

    def add_run(self, run: GriddedRun) -> None:
        """Register an already-loaded run (cache, replay or tests)."""
        self._runs[run.init_time_utc] = run

    async def _run_for(self, window: TimeWindow, fields: Sequence[str]) -> GriddedRun:
        init = self.source.latest_init_time(self._now())
        run = self._runs.get(init)
        if run is not None and set(fields) <= set(run.fields):
            return run
        if not self.settings.allow_network:
            cached = [r for r in self._runs.values() if set(fields) <= set(r.fields)]
            if cached:
                return max(cached, key=lambda r: r.init_time_utc)
            raise ProviderUnavailableError(
                f"{self.name}: network disabled and no run in memory; refusing to invent values"
            )
        last_h = max(0.0, (window.end_utc - init).total_seconds() / 3600.0)
        last_h = min(last_h, self.source.max_forecast_hours)
        steps = [h for h in range(0, int(self.source.max_forecast_hours) + 1, 6) if h <= last_h + 6]
        run = await self.source.fetch_run(init, steps, fields, self.bbox)
        self._runs[run.init_time_utc] = run
        return run

    async def fetch_forecast(
        self, point: GeoPoint, window: TimeWindow, variables: Sequence[str]
    ) -> WeatherSeries:
        wanted = self._check_variables(variables)
        need = _fields_for(wanted)
        run = await self._run_for(window, need)
        return series_from_run(run, point, window, wanted)


def series_from_run(
    run: GriddedRun, point: GeoPoint, window: TimeWindow, variables: Sequence[str]
) -> WeatherSeries:
    """Hourly `WeatherSeries` at ``point`` from one run (see module docstring)."""
    missing = [f for f in _fields_for(variables) if f not in run.fields]
    if missing:
        raise CapabilityNotSupportedError(f"{run.provider}: run lacks fields {missing}")
    if not run.covers(point):
        raise ProviderUnavailableError(
            f"{run.provider}: point ({point.latitude_deg}, {point.longitude_deg}) is outside "
            "the run's bbox; widen the bbox instead of extrapolating"
        )
    native = list(run.valid_times_utc)
    first = ceil_hour(max(window.start_utc, native[0]))
    hours: list[datetime] = []
    h = first
    while h < window.end_utc and h <= native[-1]:
        hours.append(h)
        h += timedelta(hours=1)
    if not hours:
        raise ProviderUnavailableError(f"{run.provider}: run does not cover the window")
    native_set = set(native)
    pt = {name: run.at_point(name, point) for name in _fields_for(variables)}

    series: dict[str, np.ndarray] = {}
    for v in variables:
        if v in _DIRECT:
            series[v] = _time_interp(hours, native, pt[v])
    if "wind_speed_10m" in variables or "wind_direction_10m" in variables:
        u = _time_interp(hours, native, pt["wind_u_10m"])
        w = _time_interp(hours, native, pt["wind_v_10m"])
        series["wind_speed_10m"], series["wind_direction_10m"] = wind_from_uv(u, w)
    if "precipitation" in variables:
        tot = deaccumulate(pt["precipitation_accum"], native, run.init_time_utc)
        series["precipitation"] = _per_hour(hours, native, run.init_time_utc, tot)
    ghi_hours: dict[datetime, float] = {}
    if "ghi" in variables or "dni" in variables:
        joules = deaccumulate(pt["ssrd_accum"], native, run.init_time_utc)
        starts = [run.init_time_utc, *native[:-1]]
        secs = np.array([(e - s).total_seconds() for s, e in zip(starts, native, strict=True)])
        means = np.where(secs > 0, joules / np.maximum(secs, 1.0), np.nan)
        hh, gv = hourly_ghi_from_interval_means(point, native, run.init_time_utc, means)
        ghi_hours = dict(zip(hh, gv.tolist(), strict=True))
        series["ghi"] = np.array([ghi_hours.get(t, np.nan) for t in hours])

    base = {
        "provider": run.provider,
        "dataset": run.dataset,
        "kind": run.kind,
        "retrieved_at_utc": run.retrieved_at_utc,
        "model_name": run.model_name,
        "model_version": run.model_version,
        "model_init_time_utc": run.init_time_utc,
        "point": point,
        "spatial_resolution_km": run.spatial_resolution_km,
        "temporal_resolution_s": 3600.0,
        "license": run.license,
        "source_url": run.source_url,
        "notes": run.notes,
    }
    dni_pos = (
        solar_positions(point, [t - timedelta(minutes=30) for t in hours])
        if "dni" in variables
        else []
    )
    steps: list[WeatherStep] = []
    for q, t in enumerate(hours):
        at_native = t in native_set
        values: dict[str, Measured] = {}
        for v in variables:
            if v == "dni":
                continue
            processing = _processing(v, at_native)
            prov = Provenance(valid_time_utc=t, processing=processing, **base)  # type: ignore[arg-type]
            value, flag = classify_value(v, float(series[v][q]))
            if flag is QualityFlag.OK and not at_native and v not in ("ghi", "precipitation"):
                flag = QualityFlag.INTERPOLATED
            values[v] = Measured(
                variable=v, value=value, unit=unit_for(v), provenance=prov, quality=flag
            )
        if "dni" in variables:
            values["dni"] = _dni(t, float(series["ghi"][q]), dni_pos[q], base)
        steps.append(WeatherStep(valid_time_utc=t, values=values))
    return WeatherSeries(
        point=point,
        requested_point=None,
        provider=run.provider,
        dataset=run.dataset,
        kind=run.kind,
        model_name=run.model_name,
        model_version=run.model_version,
        model_init_time_utc=run.init_time_utc,
        retrieved_at_utc=run.retrieved_at_utc,
        spatial_resolution_km=run.spatial_resolution_km,
        temporal_resolution_s=3600.0,
        license=run.license,
        source_url=run.source_url,
        steps=steps,
    )


def _processing(variable: str, at_native: bool) -> str:
    if variable == "ghi":
        return (
            "bilinear_grid_interp;deaccumulated;clearsky_index_time_interp;"
            "interval_energy_preserving;preceding_hour_mean"
        )
    if variable == "precipitation":
        return "bilinear_grid_interp;deaccumulated;uniform_hourly_disaggregation"
    if variable in ("wind_speed_10m", "wind_direction_10m"):
        tail = "" if at_native else ";linear_time_interp_uv"
        return f"bilinear_grid_interp_uv{tail};uv_to_speed_direction"
    return "bilinear_grid_interp" + ("" if at_native else ";linear_time_interp")


def _per_hour(
    hours: Sequence[datetime], native: Sequence[datetime], init: datetime, totals: np.ndarray
) -> np.ndarray:
    """Spread each native interval's total evenly over its hours."""
    starts = [init, *native[:-1]]
    out = np.full(len(hours), np.nan)
    for q, t in enumerate(hours):
        for i, (s, e) in enumerate(zip(starts, native, strict=True)):
            if s < t <= e:
                n = (e - s).total_seconds() / 3600.0
                out[q] = totals[i] / n if n > 0 else np.nan
                break
    return out


def _dni(t: datetime, ghi: float, solar: SolarPosition, base: Mapping[str, object]) -> Measured:
    """DNI from hourly GHI by DIRINT at the hour midpoint; exactly 0 with the Sun down."""
    prov = Provenance(
        valid_time_utc=t,
        processing=_processing("ghi", True),
        **base,  # type: ignore[arg-type]
    )
    if not math.isfinite(ghi):
        return Measured(
            variable="dni",
            value=None,
            unit=unit_for("dni"),
            provenance=prov,
            quality=QualityFlag.MISSING,
        )
    comp = decompose_ghi(ghi, solar, model="dirint", provenance=prov)
    value, flag = classify_value("dni", comp.dni_w_m2)
    return Measured(
        variable="dni", value=value, unit=unit_for("dni"), provenance=comp.provenance, quality=flag
    )
