"""ECMWF AIFS Single (open data) as a `GriddedSource` (ADR 0008 §1).

Downloads one AIFS run from ECMWF open data with ``ecmwf-opendata``, decodes the
GRIB2 with ``cfgrib``/``xarray``, crops it to the caller's `BBox` and converts
units into the `rwi.providers.gridded.GRID_FIELDS` vocabulary. Units are read
from each message's GRIB metadata; an unknown unit is an error, never a guess.

Heavy dependencies live in the optional ``[ml]`` extra and are imported lazily,
so the core platform (and ``make check``) runs without them.
"""

import asyncio
import hashlib
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from rwi.core.errors import ProviderUnavailableError
from rwi.core.provenance import DataKind
from rwi.core.timebase import ensure_utc, utc_now
from rwi.providers.gridded import BBox, GriddedRun

__all__ = ["AIFS_PARAMS", "AifsOpenDataSource", "run_from_grib"]

#: GRIB shortName -> grid field.
AIFS_PARAMS: Mapping[str, str] = {
    "2t": "temperature_2m",
    "2d": "dew_point_2m",
    "msl": "pressure_msl",
    "sp": "surface_pressure",
    "10u": "wind_u_10m",
    "10v": "wind_v_10m",
    "tcc": "cloud_cover",
    "lcc": "cloud_cover_low",
    "mcc": "cloud_cover_mid",
    "hcc": "cloud_cover_high",
    "tp": "precipitation_accum",
    "ssrd": "ssrd_accum",
}
_FIELD_TO_PARAM = {v: k for k, v in AIFS_PARAMS.items()}

#: (grid field, GRIB units string) -> converter into the GRID_FIELDS unit.
_CONVERT: Mapping[tuple[str, str], Callable[[np.ndarray], np.ndarray]] = {
    ("temperature_2m", "K"): lambda a: a - 273.15,
    ("dew_point_2m", "K"): lambda a: a - 273.15,
    ("pressure_msl", "Pa"): lambda a: a,
    ("surface_pressure", "Pa"): lambda a: a,
    ("wind_u_10m", "m s**-1"): lambda a: a,
    ("wind_v_10m", "m s**-1"): lambda a: a,
    ("precipitation_accum", "m"): lambda a: a * 1000.0,
    ("precipitation_accum", "kg m**-2"): lambda a: a,
    ("ssrd_accum", "J m**-2"): lambda a: a,
    **{(f, "%"): (lambda a: a) for f in AIFS_PARAMS.values() if f.startswith("cloud_cover")},
    **{
        (f, "(0 - 1)"): (lambda a: a * 100.0)
        for f in AIFS_PARAMS.values()
        if f.startswith("cloud_cover")
    },
}

_DATASET = "aifs-single-open-data"
_DOC_URL = "https://www.ecmwf.int/en/forecasts/datasets/open-data"
_SOURCE_URL = "https://data.ecmwf.int/forecasts/"
_LICENSE = "CC-BY-4.0"
#: Hours after nominal run time before a run is assumed fully published.
_PUBLICATION_LAG_H = 7
_RUN_HOURS = (0, 6, 12, 18)
_MAX_STEP_H = 360.0


def _lazy_cfgrib() -> Any:
    try:
        import cfgrib
    except ImportError as exc:  # pragma: no cover - depends on the [ml] extra
        raise ProviderUnavailableError(
            "AIFS needs the optional [ml] extra (xarray, cfgrib, eccodes); "
            "install it with: uv pip install -e '.[ml]'"
        ) from exc
    return cfgrib


def run_from_grib(
    paths: Sequence[Path],
    bbox: BBox,
    *,
    retrieved_at_utc: datetime,
    model_version: str | None = "aifs-single-0p25",
    kind: DataKind = DataKind.FORECAST,
) -> GriddedRun:
    """Decode AIFS GRIB2 file(s) into a `GriddedRun` cropped to ``bbox`` (FORECAST provenance)."""
    cfgrib = _lazy_cfgrib()
    arrays: dict[str, np.ndarray] = {}
    lats: np.ndarray | None = None
    lons: np.ndarray | None = None
    times: tuple[datetime, ...] | None = None
    init: datetime | None = None
    for path in paths:
        try:
            groups = cfgrib.open_datasets(str(path), backend_kwargs={"indexpath": ""})
        except Exception as exc:
            raise ProviderUnavailableError(f"ecmwf: cannot decode GRIB {path.name}: {exc}") from exc
        for ds in groups:
            for name in ds.data_vars:
                da = ds[name]
                short = str(da.attrs.get("GRIB_shortName", name))
                field = AIFS_PARAMS.get(short)
                if field is None or field in arrays:
                    continue
                units = str(da.attrs.get("GRIB_units", da.attrs.get("units", "")))
                conv = _CONVERT.get((field, units))
                if conv is None:
                    raise ProviderUnavailableError(
                        f"ecmwf: {short} arrived in unexpected units {units!r}; refusing to guess"
                    )
                f_lat, f_lon, f_times, f_init, data = _cropped(da, bbox)
                if lats is None:
                    lats, lons, times, init = f_lat, f_lon, f_times, f_init
                elif (
                    f_init != init
                    or f_times != times
                    or not np.array_equal(f_lat, lats)
                    or not np.array_equal(f_lon, np.asarray(lons))
                ):
                    raise ProviderUnavailableError(
                        f"ecmwf: {short} is on a different grid/run than the other fields"
                    )
                arrays[field] = conv(data)
    if lats is None or lons is None or times is None or init is None:
        raise ProviderUnavailableError("ecmwf: no known AIFS parameters in the GRIB file(s)")
    return GriddedRun(
        provider="ecmwf",
        dataset=_DATASET,
        kind=kind,
        model_name="aifs",
        model_version=model_version,
        init_time_utc=init,
        retrieved_at_utc=retrieved_at_utc,
        valid_times_utc=times,
        latitudes=lats,
        longitudes=lons,
        fields=arrays,
        spatial_resolution_km=28.0,
        license=_LICENSE,
        source_url=_SOURCE_URL,
    )


def _cropped(
    da: Any, bbox: BBox
) -> tuple[np.ndarray, np.ndarray, tuple[datetime, ...], datetime, np.ndarray]:
    """(lat, lon, valid times, init, data[t, lat, lon]) cropped to bbox plus one node."""
    lat = np.asarray(da["latitude"].values, dtype=float)
    lon = np.asarray(da["longitude"].values, dtype=float)
    lon = np.where(lon > 180.0, lon - 360.0, lon)
    order = np.argsort(lon)
    lon = lon[order]
    data = np.asarray(da.values, dtype=float)
    if data.ndim == 2:
        data = data[np.newaxis, ...]
    data = data[..., order]
    i = _bracket(lat, bbox.south, bbox.north)
    j = _bracket(lon, bbox.west, bbox.east)
    init = _to_utc(da["time"].values)
    steps = np.atleast_1d(da["step"].values) if "step" in da.coords else np.array([0])
    times = tuple(init + timedelta(seconds=float(s) / 1e9) for s in steps.astype("int64"))
    return lat[i], lon[j], times, init, data[:, i, :][:, :, j]


def _bracket(axis: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Indices covering [lo, hi] on a monotonic axis, widened by one node each side."""
    inside = np.nonzero((axis >= lo) & (axis <= hi))[0]
    if inside.size == 0:
        raise ProviderUnavailableError(f"ecmwf: grid does not cover [{lo}, {hi}]")
    a = max(int(inside.min()) - 1, 0)
    b = min(int(inside.max()) + 1, axis.size - 1)
    return np.arange(a, b + 1)


def _to_utc(value: Any) -> datetime:
    ns = int(np.datetime64(value, "ns").astype("int64"))
    return datetime.fromtimestamp(ns / 1e9, tz=UTC)


class AifsOpenDataSource:
    """`GriddedSource` for ECMWF AIFS Single from ECMWF open data (6-hourly steps)."""

    provider = "ecmwf"
    dataset = _DATASET
    fields = frozenset(AIFS_PARAMS.values())
    max_forecast_hours = _MAX_STEP_H
    spatial_resolution_km = 28.0
    license = _LICENSE
    documentation_url = _DOC_URL

    def __init__(
        self,
        *,
        download_dir: Path | None = None,
        source: str = "ecmwf",
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._dir = download_dir
        self._source = source
        self._clock = clock

    def latest_init_time(self, now: datetime) -> datetime:
        """Newest 00/06/12/18 UTC run older than the publication lag."""
        t = ensure_utc(now) - timedelta(hours=_PUBLICATION_LAG_H)
        hour = max(h for h in _RUN_HOURS if h <= t.hour)
        return t.replace(hour=hour, minute=0, second=0, microsecond=0)

    async def fetch_run(
        self, init_time: datetime, steps_h: Sequence[int], fields: Sequence[str], bbox: BBox
    ) -> GriddedRun:
        """Download the requested steps/params of one run and decode them (network)."""
        params = [_FIELD_TO_PARAM[f] for f in fields]
        init = ensure_utc(init_time)
        workdir = self._dir or Path(tempfile.mkdtemp(prefix="rwi-aifs-"))
        workdir.mkdir(parents=True, exist_ok=True)
        steps = list(steps_h)
        key = hashlib.sha256(f"{steps}|{sorted(params)}".encode()).hexdigest()[:8]
        target = workdir / f"aifs_{init:%Y%m%dT%H}Z_{key}.grib2"
        if target.is_file():
            # Same run, steps and params already on disk (an earlier --once process):
            # reuse it, and keep the time it was actually retrieved as provenance.
            retrieved = datetime.fromtimestamp(target.stat().st_mtime, tz=UTC)
        else:
            partial = target.with_suffix(".part")
            await asyncio.to_thread(self._download, init, steps, params, partial)
            partial.replace(target)  # only a complete download ever gets the final name
            retrieved = self._clock()
        return await asyncio.to_thread(run_from_grib, [target], bbox, retrieved_at_utc=retrieved)

    def _download(self, init: datetime, steps: list[int], params: list[str], target: Path) -> None:
        try:
            from ecmwf.opendata import Client
        except ImportError as exc:  # pragma: no cover - depends on the [ml] extra
            raise ProviderUnavailableError(
                "AIFS download needs ecmwf-opendata from the optional [ml] extra"
            ) from exc
        client = Client(source=self._source, model="aifs-single", resol="0p25")
        try:
            client.retrieve(
                date=init.strftime("%Y%m%d"),
                time=init.hour,
                type="fc",
                step=steps,
                param=params,
                target=str(target),
            )
        except Exception as exc:
            raise ProviderUnavailableError(f"ecmwf: AIFS download failed: {exc}") from exc
