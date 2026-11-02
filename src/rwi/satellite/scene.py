"""Decode AHI tiles, resample them onto a regular lat/lon grid, assemble a scene.

What the numbers in a scene mean
--------------------------------
``Sectorized_CMI`` of a visible band (C01-C06) is a **reflectance factor**
(``standard_name = toa_bidirectional_reflectance``, ``units = 1``, packed ``int16``
with ``scale_factor = 1/2048``). It is **NOT sun-normalised**: it has not been
divided by ``cos(solar zenith)``. This was checked on the data, not assumed - the
same stretch of Australian desert (tile T069, 2026-11-02) reads a median of 0.175
at solar zenith 12 deg (03:00Z), 0.098 at 47 deg (06:00Z) and 0.033 at 74 deg
(08:00Z): it falls off nearly as ``cos(zenith)`` (0.98 / 0.68 / 0.28) rather than
staying put as a normalised albedo would. The statement is carried on every scene
(:data:`REFLECTANCE_STATEMENT`, in ``provenance.processing`` and ``notes``) and in
each decoded tile's metadata; a test pins it. A consumer that needs albedo-like
values applies :func:`rwi.satellite.cloudindex.normalise_reflectance_by_sun`
explicitly, with a per-pixel :func:`solar_zenith_field`.

Missing data
------------
The product declares **no** ``_FillValue``. Pixels that look at space are *not*
flagged: they hold small noise integers (-6 ... ~100, i.e. "reflectance" up to
0.05) that are indistinguishable from dark ocean. Masking therefore comes from
geometry - a pixel whose line of sight misses the Earth becomes NaN - and, in
addition, from any ``_FillValue``/``missing_value`` the file does declare (and the
netCDF default for ``int16``, -32767).

Regular grid
------------
``GeoGrid`` has one latitude per row and one longitude per column, which a
geostationary fixed grid does not have (rows and columns are slightly rotated
against parallels and meridians). Scenes are therefore **resampled** (bilinear,
only where all four native neighbours are valid) onto a regular lat/lon grid whose
spacing matches the native ground footprint at the scene centre. Rows run
north -> south (image convention). Longitudes are unwrapped around the
sub-satellite longitude (they may exceed 180 east of the antimeridian).
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import numpy as np
from numpy.typing import ArrayLike, NDArray

from rwi.config import Settings, get_settings
from rwi.contracts import GeoGrid, SatelliteScene, SatelliteTile
from rwi.core.errors import ProviderUnavailableError
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import ensure_utc, utc_now
from rwi.satellite.geo import (
    Bounds,
    FullGridGeometry,
    reference_geometry,
    tile_ids_covering,
    wrap_longitude,
)
from rwi.satellite.source import (
    ATTRIBUTION,
    LICENSE,
    fetch_tile,
    list_cached_tiles,
    read_cache_record,
)
from rwi.satellite.tiles import (
    DEFAULT_PRODUCT,
    SatelliteDataError,
    TileMissingError,
    normalise_band,
    parse_tile_key,
    read_attrs,
)

__all__ = [
    "ATTRIBUTION",
    "LICENSE",
    "REFLECTANCE_STATEMENT",
    "REFLECTANCE_SUN_NORMALISED",
    "NativeTile",
    "decode_tile",
    "load_scene",
    "load_scene_async",
    "read_native_tile",
    "solar_zenith_field",
    "solar_zenith_range",
    "subset",
]

log = logging.getLogger(__name__)

FloatArray = NDArray[np.float64]

REFLECTANCE_SUN_NORMALISED: Final[bool] = False
"""Visible-band values are reflectance FACTORS, not divided by cos(solar zenith)."""

REFLECTANCE_STATEMENT: Final[str] = (
    "reflectance factor (CF toa_bidirectional_reflectance), NOT sun-normalised: not divided "
    "by cos(solar zenith); apply rwi.satellite.cloudindex.normalise_reflectance_by_sun "
    "with solar_zenith_field() before comparing pixels at different sun angles"
)

_SOURCE_URL: Final[str] = "https://registry.opendata.aws/noaa-himawari"
_MAX_GRID_PIXELS: Final[int] = 40_000_000
_NETCDF_INT16_FILL: Final[int] = -32767
_KM_PER_DEG: Final[float] = 111.32
"""Same constant the retrieval module uses to turn ``resolution_km`` into degrees."""


# ---------------------------------------------------------------------------
# Native (fixed-grid) tile
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NativeTile:
    """A decoded tile (or window of one) on the satellite's own fixed grid."""

    tile_id: str | None
    band: str
    values: NDArray[np.float32]
    """Physical values (reflectance factor or K); NaN = off-disk or declared fill."""
    row0: int
    """Full-disk row index of ``values[0, :]``."""
    col0: int
    """Full-disk column index of ``values[:, 0]``."""
    geometry: FullGridGeometry
    quantity: str
    unit: str
    metadata: dict[str, Any]


def _quantity(standard_name: str, units: str) -> tuple[str, str]:
    if standard_name == "toa_bidirectional_reflectance":
        return "reflectance", "1"
    if standard_name == "toa_brightness_temperature" or "brightness_temperature" in standard_name:
        return "brightness_temperature", "K"
    raise SatelliteDataError(
        f"unsupported Sectorized_CMI standard_name {standard_name!r} (units {units!r}); "
        "refusing to guess what the numbers mean"
    )


def read_native_tile(
    path: Path,
    *,
    rows: tuple[int, int] | None = None,
    cols: tuple[int, int] | None = None,
) -> NativeTile:
    """Read one tile file; apply ``scale_factor``/``add_offset``; mask what is not data.

    ``rows``/``cols`` are optional half-open **full-disk** index ranges: only the
    intersection with the tile is read from disk (HDF5 chunks outside it are not
    touched). The returned values are float32 with NaN for off-disk pixels and for
    the file's declared fill values - never raw ``int16`` posing as reflectance.
    """
    import h5py  # deferred import; declared in pyproject, see tiles.read_attrs

    path = Path(path)
    parsed = parse_tile_key(path.name)
    try:
        ds = h5py.File(str(path), "r")
    except OSError as exc:
        raise SatelliteDataError(f"cannot open {path.name} as NetCDF-4/HDF5: {exc}") from exc
    with ds:
        # h5py never scales or masks: the raw integers stay visible and we scale explicitly.
        try:
            gattrs = dict(read_attrs(ds))
            xa, ya = read_attrs(ds["x"]), read_attrs(ds["y"])
            geometry = FullGridGeometry.from_attrs(
                gattrs, xa, ya, read_attrs(ds["fixedgrid_projection"])
            )
            cmi = ds["Sectorized_CMI"]
            ca = read_attrs(cmi)
            x_raw = np.asarray(ds["x"][:], dtype=np.int64)
            y_raw = np.asarray(ds["y"][:], dtype=np.int64)
        except (KeyError, IndexError) as exc:
            raise SatelliteDataError(f"{path.name} lacks expected variable {exc}") from exc
        if x_raw.size < 1 or y_raw.size < 1:
            raise SatelliteDataError(f"{path.name}: degenerate x/y axes")
        if not (np.all(np.diff(x_raw) == 1) and np.all(np.diff(y_raw) == 1)):
            raise SatelliteDataError(f"{path.name}: x/y are not consecutive full-disk indices")

        quantity, unit = _quantity(str(ca.get("standard_name", "")), str(ca.get("units", "")))
        tile_row_offset = int(gattrs.get("tile_row_offset", -1))
        tile_col_offset = int(gattrs.get("tile_column_offset", -1))
        r_first, c_first = int(y_raw[0]), int(x_raw[0])
        h, w = y_raw.size, x_raw.size
        if not (tile_row_offset <= r_first < tile_row_offset + geometry.tile_rows
                and tile_col_offset <= c_first < tile_col_offset + geometry.tile_cols):  # fmt: skip
            raise SatelliteDataError(
                f"{path.name}: x/y start at (row {r_first}, col {c_first}) which is outside the "
                f"tile declared by tile_row_offset={tile_row_offset}, "
                f"tile_column_offset={tile_col_offset}"
            )
        tile_id = parsed.tile_id if parsed else None
        if tile_id is not None:
            expected = geometry.tile_offsets(tile_id)
            if expected != (tile_row_offset, tile_col_offset):
                raise SatelliteDataError(
                    f"{path.name}: the file says its offsets are "
                    f"({tile_row_offset}, {tile_col_offset}) but the layout of its own grid "
                    f"puts {tile_id} at {expected}; tile numbering assumption is wrong"
                )

        # Window in tile-local indices.
        i0, i1 = 0, h
        j0, j1 = 0, w
        if rows is not None:
            i0, i1 = max(0, rows[0] - r_first), min(h, rows[1] - r_first)
        if cols is not None:
            j0, j1 = max(0, cols[0] - c_first), min(w, cols[1] - c_first)
        if i1 <= i0 or j1 <= j0:
            empty = np.empty((0, 0), dtype=np.float32)
            return NativeTile(tile_id, parsed.band if parsed else "?", empty, r_first + i0,
                              c_first + j0, geometry, quantity, unit, {})  # fmt: skip
        raw = np.asarray(cmi[i0:i1, j0:j1])

        scale = float(ca.get("scale_factor", 1.0))
        offset = float(ca.get("add_offset", 0.0))
        values = (raw.astype(np.float64) * scale + offset).astype(np.float32)

        # Declared fill / missing / valid range. The product declares none (verified), but
        # a file that does must be honoured; the netCDF int16 default is masked as well.
        fill_values = [_NETCDF_INT16_FILL]
        for key in ("_FillValue", "missing_value"):
            if key in ca:
                fill_values.extend(np.atleast_1d(ca[key]).astype(np.int64).tolist())
        fill_mask = np.isin(raw, np.array(sorted(set(fill_values)), dtype=raw.dtype))
        if "valid_min" in ca:
            fill_mask |= raw < ca["valid_min"]
        if "valid_max" in ca:
            fill_mask |= raw > ca["valid_max"]

        # Geometry mask: pixels that look at space are not flagged in the file.
        x_urad = x_raw[j0:j1] * float(xa["scale_factor"]) + float(xa["add_offset"])
        y_urad = y_raw[i0:i1] * float(ya["scale_factor"]) + float(ya["add_offset"])
        lat, _ = geometry.projection.scan_to_latlon(x_urad[None, :], y_urad[:, None])
        off_disk = ~np.isfinite(lat)

        values[fill_mask | off_disk] = np.nan
        meta: dict[str, Any] = {
            "product_name": gattrs.get("product_name"),
            "tile_id": tile_id,
            "satellite": geometry.satellite,
            "band": parsed.band if parsed else None,
            "central_wavelength_um": float(gattrs.get("central_wavelength", float("nan"))),
            "standard_name": ca.get("standard_name"),
            "units_in_file": ca.get("units"),
            "scale_factor": scale,
            "add_offset": offset,
            "fill_values_masked": sorted(set(fill_values)),
            "native_resolution_km": geometry.resolution_km,
            "tile_row_offset": tile_row_offset,
            "tile_column_offset": tile_col_offset,
            "start_date_time": str(gattrs.get("start_date_time", "")),
            "n_pixels": int(raw.size),
            "n_off_disk_masked": int(off_disk.sum()),
            "n_fill_masked": int((fill_mask & ~off_disk).sum()),
            "sun_normalised": REFLECTANCE_SUN_NORMALISED if quantity == "reflectance" else None,
            "reflectance_statement": REFLECTANCE_STATEMENT if quantity == "reflectance" else None,
            "tile_center_latitude_in_file": float(gattrs.get("tile_center_latitude", float("nan"))),
            "tile_center_longitude_in_file": float(
                gattrs.get("tile_center_longitude", float("nan"))
            ),
        }
        return NativeTile(
            tile_id, parsed.band if parsed else "?", values, r_first + i0, c_first + j0,
            geometry, quantity, unit, meta,
        )  # fmt: skip


# ---------------------------------------------------------------------------
# Grids and resampling
# ---------------------------------------------------------------------------


def _make_grid(lats: FloatArray, lons: FloatArray, step_deg: float) -> GeoGrid:
    return GeoGrid(
        latitudes=[float(v) for v in lats],
        longitudes=[float(v) for v in lons],
        shape=(int(lats.size), int(lons.size)),
        resolution_km=float(step_deg * _KM_PER_DEG),
        bounds=(
            float(lats.min() - step_deg / 2.0),
            float(lons.min() - step_deg / 2.0),
            float(lats.max() + step_deg / 2.0),
            float(lons.max() + step_deg / 2.0),
        ),
    )


def _native_step_deg(geometry: FullGridGeometry, lat: float, lon: float) -> float:
    """Ground footprint of one native pixel at (lat, lon), as a degree step.

    Geometric mean of the north-south and east-west spacing of neighbouring pixel
    centres, converted with 111.32 km/deg. Off the sub-satellite point a pixel is
    larger than its nominal size (about 0.6 km instead of 0.5 km at Darwin).
    """
    pix = geometry.point_to_pixel(lat, lon)
    if pix is None:
        raise SatelliteDataError(f"({lat:.3f}, {lon:.3f}) is not visible from the satellite")
    r, c = pix
    la0, lo0 = geometry.pixel_to_latlon(r, c)
    la1, _ = geometry.pixel_to_latlon(r + 1.0, c)
    _, lo1 = geometry.pixel_to_latlon(r, c + 1.0)
    ns_deg = abs(float(la1 - la0))
    ew_deg = abs(float(wrap_longitude(lo1 - lo0)))
    # ew_deg is a longitude difference; convert to the same ground unit as ns_deg.
    ew_ground = ew_deg * math.cos(math.radians(lat))
    return math.sqrt(ns_deg * ew_ground) if ew_ground > 0 else ns_deg


def _regular_axes(bounds: Bounds, step_deg: float, lon0: float) -> tuple[FloatArray, FloatArray]:
    min_lat, min_lon, max_lat, max_lon = bounds
    west = float(wrap_longitude(min_lon, lon0))
    span = max_lon - min_lon
    if span < 0:
        span += 360.0
    n_lat = max(1, math.ceil((max_lat - min_lat) / step_deg - 1e-9))
    n_lon = max(1, math.ceil(span / step_deg - 1e-9))
    if n_lat * n_lon > _MAX_GRID_PIXELS:
        raise ValueError(
            f"requested grid is {n_lat} x {n_lon} = {n_lat * n_lon:,} pixels (limit "
            f"{_MAX_GRID_PIXELS:,}); use a smaller box or a larger resolution_deg"
        )
    lats = max_lat - step_deg * (np.arange(n_lat) + 0.5)
    lons = west + step_deg * (np.arange(n_lon) + 0.5)
    return lats.astype(np.float64), lons.astype(np.float64)


def _bilinear(tiles: Sequence[NativeTile], row: FloatArray, col: FloatArray) -> NDArray[np.float32]:
    """Sample the mosaic of ``tiles`` at fractional full-disk (row, col).

    NaN where the point is off the disk, outside the supplied windows, or any of the
    four neighbouring pixels is NaN. The mosaic window is allocated just large enough
    for the sampled points.
    """
    out = np.full(row.shape, np.nan, dtype=np.float32)
    ok = np.isfinite(row) & np.isfinite(col)
    if not ok.any():
        return out
    r_lo, r_hi = math.floor(row[ok].min()), math.floor(row[ok].max()) + 2
    c_lo, c_hi = math.floor(col[ok].min()), math.floor(col[ok].max()) + 2
    mosaic = np.full((r_hi - r_lo, c_hi - c_lo), np.nan, dtype=np.float32)
    for t in tiles:
        if t.values.size == 0:
            continue
        a0, a1 = max(r_lo, t.row0), min(r_hi, t.row0 + t.values.shape[0])
        b0, b1 = max(c_lo, t.col0), min(c_hi, t.col0 + t.values.shape[1])
        if a1 > a0 and b1 > b0:
            mosaic[a0 - r_lo : a1 - r_lo, b0 - c_lo : b1 - c_lo] = t.values[
                a0 - t.row0 : a1 - t.row0, b0 - t.col0 : b1 - t.col0
            ]
    r0 = np.floor(row[ok]).astype(np.int64)
    c0 = np.floor(col[ok]).astype(np.int64)
    fr = (row[ok] - r0).astype(np.float32)
    fc = (col[ok] - c0).astype(np.float32)
    ri, ci = r0 - r_lo, c0 - c_lo
    v00, v01 = mosaic[ri, ci], mosaic[ri, ci + 1]
    v10, v11 = mosaic[ri + 1, ci], mosaic[ri + 1, ci + 1]
    val = (
        v00 * (1 - fr) * (1 - fc) + v01 * (1 - fr) * fc + v10 * fr * (1 - fc) + v11 * fr * fc
    )  # NaN in any neighbour propagates: deliberate
    out[ok] = val.astype(np.float32)
    return out


def _resample(
    tiles: Sequence[NativeTile], geometry: FullGridGeometry, lats: FloatArray, lons: FloatArray
) -> NDArray[np.float32]:
    """Fixed grid -> regular lat/lon grid (``len(lats)`` x ``len(lons)``)."""
    la, lo = np.meshgrid(lats, lons, indexing="ij")
    row, col = geometry.latlon_to_pixel(la, lo)
    return _bilinear(tiles, row, col)


def _window_for(
    geometry: FullGridGeometry, lats: FloatArray, lons: FloatArray
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Full-disk (rows, cols) index ranges needed to resample onto the axes."""
    la = np.array([lats.min(), lats.max()])
    lo = np.array([lons.min(), lons.max()])
    # Sample the perimeter (the fixed grid is rotated against the parallels).
    ramp_la = np.linspace(la[0], la[1], 33)
    ramp_lo = np.linspace(lo[0], lo[1], 33)
    eg_la = np.concatenate([ramp_la, ramp_la, np.full(33, la[0]), np.full(33, la[1])])
    eg_lo = np.concatenate([np.full(33, lo[0]), np.full(33, lo[1]), ramp_lo, ramp_lo])
    row, col = geometry.latlon_to_pixel(eg_la, eg_lo)
    ok = np.isfinite(row) & np.isfinite(col)
    if not ok.any():
        return None
    pad = 3
    return (
        (math.floor(row[ok].min()) - pad, math.ceil(row[ok].max()) + pad + 1),
        (math.floor(col[ok].min()) - pad, math.ceil(col[ok].max()) + pad + 1),
    )


def _grid_and_values(
    paths: Sequence[Path], bounds: Bounds | None, resolution_deg: float | None
) -> tuple[NDArray[np.float32], GeoGrid, list[NativeTile], dict[str, Any]]:
    first = read_native_tile(paths[0], rows=(0, 0), cols=(0, 0))  # geometry only
    geometry = first.geometry
    lon0 = geometry.projection.longitude_of_projection_origin_deg
    if bounds is None:
        if len(paths) != 1 or first.tile_id is None:
            raise ValueError(
                "bounds are required unless exactly one tile with a standard AHI file name is given"
            )
        bounds = geometry.tile_bounds(first.tile_id)
        # tile_bounds is padded; trim back to the part that is actually on the disk
        bounds = (bounds[0] + 0.25, bounds[1] + 0.25, bounds[2] - 0.25, bounds[3] - 0.25)
    min_lat, min_lon, max_lat, max_lon = bounds
    centre_lat, centre_lon = (
        (min_lat + max_lat) / 2.0,
        float(wrap_longitude((min_lon + (max_lon - min_lon) % 360.0 / 2.0), 0.0)),
    )
    step = resolution_deg
    if step is None:
        step = _native_step_deg(geometry, centre_lat, centre_lon)
    lats, lons = _regular_axes(bounds, step, lon0)
    window = _window_for(geometry, lats, lons)
    if window is None:
        raise SatelliteDataError("the requested box does not see any part of the Earth's disk")
    tiles = [read_native_tile(p, rows=window[0], cols=window[1]) for p in paths]
    for t in tiles:
        if t.geometry.signature != geometry.signature:
            raise SatelliteDataError(
                f"tile {t.tile_id} has a different grid/projection than {first.tile_id}; "
                "refusing to mosaic them"
            )
    values = _resample(tiles, geometry, lats, lons)
    grid = _make_grid(lats, lons, step)
    meta = {
        "regrid": "bilinear, requires all four native neighbours valid",
        "grid_step_deg": step,
        "native_resolution_km": geometry.resolution_km,
        "full_disk_window_rows": window[0],
        "full_disk_window_cols": window[1],
        "n_tiles": len(tiles),
    }
    return values, grid, tiles, meta


def decode_tile(
    path: Path,
    *,
    bounds: Bounds | None = None,
    resolution_deg: float | None = None,
) -> tuple[NDArray[np.float32], GeoGrid, dict[str, Any]]:
    """Decode one tile file -> ``(values, grid, metadata)`` on a regular lat/lon grid.

    ``values`` is float32, physical units (reflectance FACTOR - see the module
    docstring - or kelvin), NaN for off-disk and declared-fill pixels, shape
    ``grid.shape`` (rows north -> south). ``scale_factor``/``add_offset`` are applied;
    raw integers never escape. ``bounds`` (min_lat, min_lon, max_lat, max_lon)
    restricts the output to a sub-box (and the read to the chunks it needs); the
    default is the tile's on-disk footprint. ``resolution_deg`` overrides the grid
    step (default: the native footprint at the box centre).

    ``metadata`` carries the file's facts (scale, offset, masked counts,
    ``sun_normalised`` = False, tile centre as stated in the file, ...).
    """
    values, grid, tiles, meta = _grid_and_values([Path(path)], bounds, resolution_deg)
    t = tiles[0]
    md = dict(t.metadata)
    md.update(meta)
    md["quantity"] = t.quantity
    md["unit"] = t.unit
    md["n_valid_output_pixels"] = int(np.isfinite(values).sum())
    return values, grid, md


def subset(
    values: NDArray[np.float32], grid: GeoGrid, bounds: Bounds
) -> tuple[NDArray[np.float32], GeoGrid]:
    """Cut ``values`` (on ``grid``) down to the pixels whose centres lie inside ``bounds``.

    ``bounds`` is (min_lat, min_lon, max_lat, max_lon); longitudes may be in either
    (-180, 180] or the grid's unwrapped form. Raises ``ValueError`` when nothing
    overlaps, rather than returning an empty array that looks like "no clouds".
    """
    if values.shape != tuple(grid.shape):
        raise ValueError(f"values shape {values.shape} does not match grid {grid.shape}")
    lats = np.asarray(grid.latitudes, dtype=np.float64)
    lons = np.asarray(grid.longitudes, dtype=np.float64)
    min_lat, min_lon, max_lat, max_lon = bounds
    centre = float(lons.mean())
    w = float(wrap_longitude(min_lon, centre))
    span = max_lon - min_lon
    if span < 0:
        span += 360.0
    rows = np.flatnonzero((lats >= min_lat) & (lats <= max_lat))
    cols = np.flatnonzero((lons >= w) & (lons <= w + span))
    if rows.size == 0 or cols.size == 0:
        raise ValueError(f"bounds {bounds} do not overlap the grid {grid.bounds}")
    r0, r1, c0, c1 = int(rows.min()), int(rows.max()) + 1, int(cols.min()), int(cols.max()) + 1
    step = float(grid.resolution_km / _KM_PER_DEG)
    sub_grid = _make_grid(lats[r0:r1], lons[c0:c1], step)
    return values[r0:r1, c0:c1].copy(), sub_grid


# ---------------------------------------------------------------------------
# Sun geometry
# ---------------------------------------------------------------------------


def _sun_declination_eot(when: datetime, lat: float, lon: float) -> tuple[float, float]:
    from rwi.core.geo import GeoPoint
    from rwi.solar import solar_position  # lazy: keeps this module importable without pvlib

    pos = solar_position(
        GeoPoint(latitude_deg=max(-90.0, min(90.0, lat)),
                 longitude_deg=float(wrap_longitude(lon, 0.0))),
        when,
    )  # fmt: skip
    if pos.declination_deg is None or pos.equation_of_time_min is None:
        raise SatelliteDataError("rwi.solar returned no declination / equation of time")
    return pos.declination_deg, pos.equation_of_time_min


def solar_zenith_field(
    latitudes: ArrayLike, longitudes: ArrayLike, when_utc: datetime
) -> FloatArray:
    """Geometric solar zenith angle (degrees) on the product grid ``lat[:, None] x lon[None, :]``.

    Declination and equation of time come from :mod:`rwi.solar` (NREL SPA via pvlib)
    evaluated once at the grid centre; the zenith then follows from the standard
    spherical-triangle relation ``cos z = sin(lat) sin(dec) + cos(lat) cos(dec) cos(H)``
    with ``H = 15 * (UTC_h + EoT_min/60) + lon - 180``. No refraction. Declination
    changes < 0.01 deg across a scene, so a single value is exact enough; agreement with
    SPA at individual points is tested to < 0.05 deg.
    """
    lat = np.asarray(latitudes, dtype=np.float64)
    lon = np.asarray(longitudes, dtype=np.float64)
    when = ensure_utc(when_utc)
    dec_deg, eot_min = _sun_declination_eot(when, float(lat.mean()), float(lon.mean()))
    hours = when.hour + when.minute / 60.0 + (when.second + when.microsecond / 1e6) / 3600.0
    hour_angle = np.radians(15.0 * (hours + eot_min / 60.0) + lon[None, :] - 180.0)
    phi = np.radians(lat)[:, None]
    dec = math.radians(dec_deg)
    cos_z = np.sin(phi) * math.sin(dec) + np.cos(phi) * math.cos(dec) * np.cos(hour_angle)
    return np.asarray(np.degrees(np.arccos(np.clip(cos_z, -1.0, 1.0))), dtype=np.float64)


def solar_zenith_range(grid: GeoGrid, when_utc: datetime) -> tuple[float, float]:
    """(min, max) geometric solar zenith over the grid at ``when_utc``."""
    lat = np.asarray(grid.latitudes)
    lon = np.asarray(grid.longitudes)
    # Coarse subsample keeps this cheap on large grids; the field is smooth.
    step_r, step_c = max(1, lat.size // 64), max(1, lon.size // 64)
    sub = solar_zenith_field(
        np.append(lat[::step_r], lat[-1]), np.append(lon[::step_c], lon[-1]), when_utc
    )
    return float(sub.min()), float(sub.max())


# ---------------------------------------------------------------------------
# Scene assembly
# ---------------------------------------------------------------------------


def _tiles_for_request(
    bounds: Bounds,
    slot: datetime,
    band: str,
    product: str,
    cache_dir: Path,
    settings: Settings,
    geometry: FullGridGeometry,
) -> list[SatelliteTile]:
    from rwi.satellite.geo import tiles_covering  # local import avoids a cycle at import time

    ids = tile_ids_covering(bounds, band=band, geometry=geometry, cache_dir=cache_dir)
    if not ids:
        raise SatelliteDataError(
            f"bounds {bounds} are outside the part of the Earth Himawari can see; no tile covers "
            "them"
        )
    if settings.allow_network:
        found = tiles_covering(bounds, slot, band=band, geometry=geometry, cache_dir=cache_dir,
                               settings=settings)  # fmt: skip
        if not found:
            raise SatelliteDataError(
                f"no {band} files are published for slot {slot:%Y-%m-%dT%H:%MZ}"
            )
        return found
    cached = {t.tile_id: t for t in list_cached_tiles(slot, band=band, cache_dir=cache_dir,
                                                       product=product)}  # fmt: skip
    missing = [i for i in ids if i not in cached]
    if missing:
        raise ProviderUnavailableError(
            f"offline and tiles {missing} of {band} for slot {slot:%Y-%m-%dT%H:%MZ} are not in "
            "the cache; nothing is invented"
        )
    return [cached[i] for i in ids]


async def load_scene_async(
    bounds: Bounds,
    slot_time_utc: datetime,
    *,
    band: str = "C03",
    cache_dir: Path,
    product: str = DEFAULT_PRODUCT,
    resolution_deg: float | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> tuple[NDArray[np.float32], SatelliteScene]:
    """Download (or read from cache) the tiles covering ``bounds`` and assemble a scene.

    Returns ``(values, scene)``: ``values`` is float32 ``scene.grid.shape``, NaN where
    there is no valid data; ``scene`` carries the grid, quantity, solar-zenith range
    and full provenance. Raises :class:`SatelliteDataError` for a box off the disk,
    :class:`TileMissingError` for a partly published slot, and
    :class:`ProviderUnavailableError` offline without a cached copy. Never returns a
    mosaic with a silent hole.
    """
    settings = settings or get_settings()
    slot = ensure_utc(slot_time_utc)
    band = normalise_band(band)
    prior = reference_geometry(band)

    tiles = await asyncio.to_thread(
        _tiles_for_request, bounds, slot, band, product, Path(cache_dir), settings, prior
    )
    paths = list(
        await asyncio.gather(*(fetch_tile(t, cache_dir=Path(cache_dir), settings=settings)
                               for t in tiles))
    )  # fmt: skip
    return await asyncio.to_thread(
        _assemble, bounds, slot, band, tiles, paths, prior, resolution_deg, now
    )


def _assemble(
    bounds: Bounds,
    slot: datetime,
    band: str,
    tiles: list[SatelliteTile],
    paths: list[Path],
    prior: FullGridGeometry,
    resolution_deg: float | None,
    now: datetime | None,
) -> tuple[NDArray[np.float32], SatelliteScene]:
    values, grid, native, meta = _grid_and_values(paths, bounds, resolution_deg)
    geometry = native[0].geometry

    # The prior (which tiles to download) must agree with the files (what they are).
    chosen = sorted(t.tile_id for t in tiles)
    truth = tile_ids_covering(bounds, band=band, geometry=geometry)
    if chosen != truth:
        raise SatelliteDataError(
            f"the files' own grid says {truth} cover the box but {chosen} were downloaded: "
            "the reference geometry no longer matches the satellite; update it"
        )
    for t in native:
        if t.metadata.get("tile_row_offset") != prior.tile_offsets(str(t.tile_id))[0] or (
            t.metadata.get("tile_column_offset") != prior.tile_offsets(str(t.tile_id))[1]
        ):
            raise SatelliteDataError(
                f"{t.tile_id}: file offsets disagree with the reference layout; "
                "the reference geometry no longer matches the satellite"
            )

    first = native[0]
    quantity = first.quantity
    if any(t.quantity != quantity for t in native):
        raise SatelliteDataError("tiles of one band disagree on the physical quantity")

    zen = solar_zenith_range(grid, slot)
    retrieved = [rec.retrieved_at_utc for p in paths if (rec := read_cache_record(p)) is not None]
    if len(retrieved) != len(paths):
        raise SatelliteDataError("a cached tile has no retrieval record; cannot state data age")
    created = [t.published_at_utc for t in tiles if t.published_at_utc is not None]
    latency_s = (max(created) - slot).total_seconds() if created else None
    now_ = ensure_utc(now) if now is not None else utc_now()
    resolution_note = (
        f"native {geometry.resolution_km} km at sub-satellite point, regridded to "
        f"{grid.resolution_km:.3f} km (N-S) steps"
    )
    if quantity == "reflectance":
        normalisation = REFLECTANCE_STATEMENT
    else:
        normalisation = "brightness temperature in kelvin (no sun dependence)"
    processing = (
        f"{normalisation}; bilinear resample of {len(tiles)} fixed-grid tile(s) onto a "
        "regular lat/lon grid; off-disk and fill pixels are NaN"
    )
    notes = (
        f"{normalisation}. satellite={first.geometry.satellite}; band={band}; "
        f"slot_time_utc={slot.isoformat()} (nominal scan start; pixels are acquired over ~10 min); "
        f"tiles={','.join(sorted(t.tile_id for t in tiles))}; "
        f"keys={'; '.join(t.key for t in tiles)}; {resolution_note}; "
        f"product_created_latency_s={latency_s}; "
        f"image_age_at_load_s={(now_ - slot).total_seconds():.0f}; "
        f"earliest_tile_retrieved_at_utc={min(retrieved).isoformat()}; "
        f"valid_pixels={meta and int(np.isfinite(values).sum())}/{values.size}. "
        f"NOT a ground measurement. Attribution: {ATTRIBUTION}"
    )
    provenance = Provenance(
        provider="NOAA STAR / JMA (via NOAA NODD)",
        dataset=f"{DEFAULT_PRODUCT}/{band}",
        kind=DataKind.DERIVED,
        retrieved_at_utc=min(retrieved),
        valid_time_utc=slot,
        spatial_resolution_km=float(grid.resolution_km),
        temporal_resolution_s=600.0,
        processing=processing,
        license=LICENSE,
        source_url=_SOURCE_URL,
        notes=notes,
    )
    scene = SatelliteScene(
        satellite=first.geometry.satellite,
        band=band,
        slot_time_utc=slot,
        grid=grid,
        quantity=quantity,  # type: ignore[arg-type]
        unit=first.unit,
        solar_zenith_range_deg=zen,
        sun_normalised=REFLECTANCE_SUN_NORMALISED if quantity == "reflectance" else None,
        provenance=provenance,
    )
    return values, scene


def load_scene(
    bounds: Bounds,
    slot_time_utc: datetime,
    *,
    band: str = "C03",
    cache_dir: Path,
    product: str = DEFAULT_PRODUCT,
    resolution_deg: float | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> tuple[NDArray[np.float32], SatelliteScene]:
    """Synchronous :func:`load_scene_async` for scripts and threads.

    Must not be called from inside a running event loop (use the async form there).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            load_scene_async(bounds, slot_time_utc, band=band, cache_dir=cache_dir,
                             product=product, resolution_deg=resolution_deg,
                             settings=settings, now=now)
        )  # fmt: skip
    raise RuntimeError("load_scene() called inside a running event loop; await load_scene_async()")


_ = TileMissingError  # re-exported for callers that catch it from this module
