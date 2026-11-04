"""SatCORPS CERES GEO Edition 4 Himawari-9 cloud+radiation retrievals.

This is the complement to ``rwi.satellite.tiles``, not a replacement for it.
The two differ in what they do and when:

* ``tiles`` is the live path. It downloads raw AHI L2 Sectorised full-disk
  tiles from the public NOAA S3 bucket every 10 minutes, and we compute
  the cloud index, Heliosat retrieval and nowcast ourselves. That is the
  pipeline the car's strategy layer consumes in real time.
* ``satcorps`` (this module) reads NASA LaRC's own re-processing of
  Himawari-9 into hourly cloud and radiation properties at 6 km, published
  as SatCORPS CERES GEO Edition 4 Himawari-9 Southern Hemisphere V1.4
  (CMR id ``C3880513838-LARC_CLOUD``). It is for cross-validation of our
  retrieval, for retrospective analysis against race-day history, and for
  cloud-property fields (optical depth, cloud top height, cloud phase)
  that we do not derive ourselves.

Why both. Our Heliosat chain assumes a cloud-index -> clear-sky-index
relationship from Beyer, Costanzo and Heinemann (1996) whose tuning was
never validated against ground truth in Australia. SatCORPS's independent
cloud-optical-depth gives us a direct check: for the same scene time and
point, is our k* consistent with the optical depth NASA retrieves?

Not a measurement. Everything in this module is still a retrieval - a
radiative-transfer model applied to radiances observed from orbit. It is
better-labelled and more carefully validated than ours; it is not truth.

Access. Reading SatCORPS needs a free NASA Earthdata login (persisted in
``~/.netrc`` by one-time ``earthaccess.login(persist=True)``). Discovery
(CMR search) works without credentials. Download, open and read need them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from rwi.core.geo import GeoPoint
from rwi.core.timebase import TimeWindow, ensure_utc

log = logging.getLogger(__name__)

__all__ = [
    "COLLECTION_CONCEPT_ID",
    "COLLECTION_SHORT_NAME",
    "DEFAULT_VARIABLES",
    "SatCorpsCapabilityError",
    "SatCorpsScene",
    "download_scene",
    "read_cloud_point",
    "search_scenes",
]

#: CMR short name. Reader passes this to earthaccess for discovery.
COLLECTION_SHORT_NAME = "CER_GEO_Ed4_HIM09_SH"

#: CMR concept id. Pinning the id defends against a later renaming of the
#: short name; CMR concept ids are immutable per revision.
COLLECTION_CONCEPT_ID = "C3880513838-LARC_CLOUD"

#: SatCORPS netCDF variables we extract by default. Names verified against
#: CER_GEO_Ed4_HIM09_SH V01.4 granule 2025.236.0030.06K. The pixel-level file
#: carries cloud *properties* per pixel; it does not carry a cloud-fraction
#: or cloud-mask grid (those are aggregate products you'd compute from
#: cloud_phase over a region). Per-pixel "is this pixel cloudy?" is
#: ``cloud_phase != 0``.
DEFAULT_VARIABLES: tuple[str, ...] = (
    # cloud_phase: 0=snow, 1=water, 2=ice, 3=no retrieval, 4=clear, 5=bad,
    # 6=suspected water, 7=suspected ice, 13=cleaned. CLEAR is 4, not 0.
    "cloud_phase",
    "cloud_visible_optical_depth",   # dimensionless, valid 0..150
    "cloud_top_height",              # km, valid -0.1..20
    "cloud_top_temperature",         # K
    "cloud_effective_height",        # km, radiatively effective layer
    "broadband_shortwave_albedo",    # % (not 0..1), valid 0..150
)


class SatCorpsCapabilityError(RuntimeError):
    """Raised when SatCORPS access needs a capability this environment lacks.

    Most commonly: earthaccess is not installed, Earthdata credentials are
    not in ``~/.netrc``, or the ``h5py`` reader is missing. Each case
    includes the exact remediation step.
    """


# ---------------------------------------------------------------------------
# Scene type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SatCorpsScene:
    """One hourly SatCORPS granule and where to fetch it from.

    ``local_path`` is ``None`` until :func:`download_scene` has been called
    for this scene; the search step alone does not touch the filesystem.
    """

    granule_id: str
    time_start_utc: datetime
    time_end_utc: datetime
    data_url: str
    local_path: Path | None = None

    @property
    def is_downloaded(self) -> bool:
        return self.local_path is not None and self.local_path.is_file()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _require_earthaccess() -> Any:
    try:
        import earthaccess
    except ImportError as exc:
        raise SatCorpsCapabilityError(
            "earthaccess is not installed. Add it with `pip install earthaccess` "
            "or re-sync the project dependencies."
        ) from exc
    return earthaccess


def search_scenes(window: TimeWindow) -> list[SatCorpsScene]:
    """List SatCORPS granules whose scan time falls in ``window``.

    Pure discovery: no file is downloaded here. Call :func:`download_scene`
    on each returned scene when you are ready to read its contents. A
    one-hour window usually returns one granule; an open-ended window
    returns up to CMR's 2000-row default page size.

    This function does not require Earthdata credentials; the CMR search
    endpoint is public. Download does require them.
    """
    earthaccess = _require_earthaccess()
    results = earthaccess.search_data(
        short_name=COLLECTION_SHORT_NAME,
        temporal=(
            ensure_utc(window.start_utc).strftime("%Y-%m-%d"),
            ensure_utc(window.end_utc).strftime("%Y-%m-%d"),
        ),
    )
    scenes: list[SatCorpsScene] = []
    for g in results:
        umm = g.get("umm", {}) if isinstance(g, dict) else getattr(g, "umm", {})
        granule_id = str(umm.get("GranuleUR") or g.get("meta", {}).get("concept-id", "unknown"))
        temporal = umm.get("TemporalExtent", {}).get("RangeDateTime", {})
        start = _parse_iso(temporal.get("BeginningDateTime"))
        end = _parse_iso(temporal.get("EndingDateTime"))
        urls = g.data_links() if hasattr(g, "data_links") else []
        if not urls or start is None or end is None:
            continue
        # Clip to the requested window - CMR returns daily granules whose
        # 'time_end' is 23:59 even when you asked for one hour, so without
        # this a one-hour request returns 24 granules.
        if end < ensure_utc(window.start_utc) or start > ensure_utc(window.end_utc):
            continue
        scenes.append(SatCorpsScene(
            granule_id=granule_id,
            time_start_utc=start,
            time_end_utc=end,
            data_url=urls[0],
        ))
    return scenes


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).astimezone(UTC)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def download_scene(scene: SatCorpsScene, *, cache_dir: Path) -> SatCorpsScene:
    """Fetch ``scene``'s netCDF to ``cache_dir`` and return a new scene with ``local_path`` set.

    Idempotent: if the target file already exists, no HTTPS call is made.
    The byte-for-byte check is deliberately not here; SatCORPS re-processes
    older data when the algorithm changes, and an outdated cached file is
    the caller's problem to decide when to refresh.
    """
    earthaccess = _require_earthaccess()
    cache_dir.mkdir(parents=True, exist_ok=True)
    expected_name = scene.data_url.rsplit("/", 1)[-1]
    target = cache_dir / expected_name
    if target.is_file():
        return SatCorpsScene(
            granule_id=scene.granule_id,
            time_start_utc=scene.time_start_utc,
            time_end_utc=scene.time_end_utc,
            data_url=scene.data_url,
            local_path=target,
        )
    # earthaccess.download accepts a list of url-strings or granule objects.
    # The granule object carries the correct auth header via its session.
    results = earthaccess.search_data(
        short_name=COLLECTION_SHORT_NAME,
        temporal=(
            scene.time_start_utc.strftime("%Y-%m-%d"),
            scene.time_end_utc.strftime("%Y-%m-%d"),
        ),
    )
    match = next(
        (g for g in results
         if g.data_links() and g.data_links()[0] == scene.data_url),
        None,
    )
    if match is None:
        raise SatCorpsCapabilityError(
            f"granule {scene.granule_id} not found by CMR search; it may have been "
            "superseded by a new revision. Call search_scenes again."
        )
    # earthaccess requires an explicit login() in each process before download
    # even when ~/.netrc already has the credentials; it initialises an
    # internal auth store that search_data does not need.
    earthaccess.login(strategy="netrc")
    paths = earthaccess.download(match, local_path=str(cache_dir))
    if not paths:
        raise SatCorpsCapabilityError(
            "earthaccess.download returned no files. Most likely cause: Earthdata "
            "credentials are missing. Run `./.venv/bin/python -c \"import "
            "earthaccess; earthaccess.login(persist=True)\"` once per machine."
        )
    return SatCorpsScene(
        granule_id=scene.granule_id,
        time_start_utc=scene.time_start_utc,
        time_end_utc=scene.time_end_utc,
        data_url=scene.data_url,
        local_path=Path(paths[0]),
    )


# ---------------------------------------------------------------------------
# Point extraction
# ---------------------------------------------------------------------------


def read_cloud_point(
    path: Path,
    point: GeoPoint,
    *,
    variables: tuple[str, ...] = DEFAULT_VARIABLES,
) -> dict[str, float | None]:
    """Extract ``variables`` at the pixel nearest ``point`` from one SatCORPS file.

    The SatCORPS files are netCDF-4 (HDF5 underneath); we read them with
    ``h5py`` which this project already depends on for the raw-tile pipeline.
    The pixel geolocation is stored as 2-D ``latitude`` / ``longitude``
    arrays (one per pixel row/column); a brute-force nearest-neighbour is
    enough at 6 km resolution over the 180 x 45 region a single car occupies.

    Returns a dict ``variable -> value`` with ``None`` for any variable that
    is missing, flagged invalid by a fill value, or marked bad by the
    product QA. The caller must not treat ``None`` as zero.

    This function reads a single granule. Streaming many granules through
    one point is the caller's job; the module keeps no state so it can be
    used from any pipeline.
    """
    try:
        import h5py
    except ImportError as exc:
        raise SatCorpsCapabilityError(
            "h5py is required to read SatCORPS netCDF-4 granules but is not installed."
        ) from exc

    with h5py.File(path, "r") as f:
        try:
            lat = np.asarray(f["latitude"][:])
            lon = np.asarray(f["longitude"][:])
        except KeyError as exc:
            raise SatCorpsCapabilityError(
                f"SatCORPS granule {path.name} has no latitude/longitude dataset; "
                "the file may follow a different version's layout. Inspect it with "
                "`h5ls -r` and pass the correct variable names explicitly."
            ) from exc
        row, col = _nearest_pixel(lat, lon, point.latitude_deg, point.longitude_deg)
        out: dict[str, float | None] = {}
        for var in variables:
            if var not in f:
                out[var] = None
                continue
            dataset = f[var]
            raw = float(np.asarray(dataset[row, col]))
            fill = _fill_value(dataset)
            if fill is not None and raw == fill:
                out[var] = None
            else:
                out[var] = _apply_scale_offset(dataset, raw)
    return out


def _nearest_pixel(
    lat_grid: np.ndarray,
    lon_grid: np.ndarray,
    target_lat: float,
    target_lon: float,
) -> tuple[int, int]:
    """Row, col of the pixel closest to the target in planar distance.

    Not haversine - the SatCORPS grid spacing at 23 S is 6 km and a car
    sees the sky across hundreds of km of parallax, so a planar
    approximation is already tighter than the pixel footprint.

    SATCORPS full-disk pixels that fall off Earth carry latitude/longitude
    ``_FillValue`` 3.4e+38; computed in float32 their square overflows.
    Promote to float64 and mask out-of-range lat/lon with +inf distance
    so they never win argmin.
    """
    lat = lat_grid.astype(np.float64)
    lon = lon_grid.astype(np.float64)
    bad = (np.abs(lat) > 90.0) | (np.abs(lon) > 180.0)
    dlat = lat - target_lat
    dlon = (lon - target_lon) * np.cos(np.radians(target_lat))
    d2 = dlat * dlat + dlon * dlon
    d2[bad] = np.inf
    idx = int(np.argmin(d2))
    return divmod(idx, lat_grid.shape[1])


def _fill_value(dataset: Any) -> float | None:
    for attr in ("_FillValue", "missing_value"):
        if attr in dataset.attrs:
            return float(np.asarray(dataset.attrs[attr]).item())
    return None


def _apply_scale_offset(dataset: Any, raw: float) -> float:
    scale = float(np.asarray(dataset.attrs.get("scale_factor", 1.0)).item())
    offset = float(np.asarray(dataset.attrs.get("add_offset", 0.0)).item())
    return raw * scale + offset
