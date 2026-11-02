"""Geolocation of geostationary imagery: pixel <-> latitude/longitude, tile extents.

Model
-----
The AHI tiles are on the *fixed grid* of a geostationary satellite: each pixel is
identified by two scan angles ``(x, y)`` seen from the satellite, not by a
latitude/longitude. The file stores them as ``x`` and ``y`` variables in
**microradians** (``units = "microradian"``), packed as ``int16`` with a
``scale_factor``/``add_offset`` (the packed integer is the pixel index in the
full-disk grid: ``x_packed == tile_column_offset + column``).

The CRS is built *from the file's own* ``fixedgrid_projection`` attributes
(sub-satellite longitude, ellipsoid axes, satellite height, sweep axis), never
from numbers typed into this module, so Himawari-10 or a re-processed archive
with a different ellipsoid decodes correctly without a code change.

Two pitfalls this module is built around (both tested):

1. **Units.** PROJ's ``geos`` projection wants *metres on the perspective plane*,
   i.e. ``scan_angle_radians * perspective_point_height``. The file has
   microradians. ``x_m = x_urad * 1e-6 * h``. Passing the raw microradian number
   gives coordinates a million times too small - every pixel then maps within a
   few millimetres of the sub-satellite point and the image looks like it is
   "slightly shifted" rather than obviously wrong.
2. **Sweep axis.** ``sweep_angle_axis`` is ``"y"`` for Himawari (and Meteosat) and
   ``"x"`` for GOES-R. The two conventions order the east-west and north-south
   rotations differently; using the wrong one does not crash - it bends the image
   progressively towards the limb (about 6 km at 5 degrees latitude, 15 degrees
   from the sub-satellite point on the NOAA tile centre we checked).

Off-disk points: PROJ returns ``inf``; this module converts that to ``NaN`` and the
scalar helpers return ``None``. A coordinate is never made up for a pixel that
looks at space.

Latitudes are geodetic on the ellipsoid the file declares (GRS80, within 0.1 mm of
WGS84), longitudes are returned in (-180, 180].
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import ArrayLike, NDArray
from pyproj import CRS, Transformer

from rwi.config import Settings, get_settings
from rwi.contracts import SatelliteTile
from rwi.core.timebase import ensure_utc
from rwi.satellite.tiles import (
    EXPECTED_TILES,
    SatelliteDataError,
    TileMissingError,
    band_resolution_km,
    normalise_band,
    read_attrs,
)

__all__ = [
    "FullGridGeometry",
    "GeostationaryProjection",
    "TileExtent",
    "reference_geometry",
    "tile_ids_covering",
    "tile_layout",
    "tiles_covering",
    "wrap_longitude",
]

log = logging.getLogger(__name__)

FloatArray = NDArray[np.float64]
Bounds = tuple[float, float, float, float]
"""(min_lat, min_lon, max_lat, max_lon), degrees; the order of ``GeoGrid.bounds``."""

MICRORADIAN = 1.0e-6
"""radians per microradian."""


def wrap_longitude(lon: ArrayLike, centre_deg: float = 0.0) -> FloatArray:
    """Wrap longitudes into ``[centre-180, centre+180)``.

    ``centre=0`` gives the usual (-180, 180]-style range; ``centre=140.7`` gives the
    continuous 'unwrapped' longitudes (60.7 .. 220.7) in which the Himawari disk
    has no discontinuity at the antimeridian.
    """
    arr = np.asarray(lon, dtype=np.float64)
    return np.asarray((arr - centre_deg + 180.0) % 360.0 - 180.0 + centre_deg, dtype=np.float64)


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeostationaryProjection:
    """The ``fixedgrid_projection`` variable of a tile, as numbers with units."""

    longitude_of_projection_origin_deg: float
    semi_major_m: float
    semi_minor_m: float
    perspective_point_height_m: float
    sweep_angle_axis: Literal["x", "y"]
    latitude_of_projection_origin_deg: float = 0.0

    @classmethod
    def from_attrs(cls, attrs: Mapping[str, Any]) -> GeostationaryProjection:
        """Build from the attributes of ``fixedgrid_projection``.

        Accepts the file's ``semi_major``/``semi_minor`` and the CF spelling
        ``semi_major_axis``/``semi_minor_axis``. Missing or unsupported attributes
        raise: a projection guessed from defaults would still produce an image.
        """
        name = attrs.get("grid_mapping_name")
        if name != "geostationary":
            raise SatelliteDataError(f"grid_mapping_name is {name!r}, expected 'geostationary'")

        def need(*names: str) -> float:
            for n in names:
                if n in attrs:
                    return float(attrs[n])
            raise SatelliteDataError(f"fixedgrid_projection lacks attribute {names[0]!r}")

        sweep = str(attrs.get("sweep_angle_axis", "")).lower()
        if sweep not in ("x", "y"):
            raise SatelliteDataError(f"sweep_angle_axis is {sweep!r}, expected 'x' or 'y'")
        lat0 = float(attrs.get("latitude_of_projection_origin", 0.0))
        if abs(lat0) > 1e-9:
            raise SatelliteDataError(f"latitude_of_projection_origin={lat0}; only 0 is supported")
        return cls(
            longitude_of_projection_origin_deg=need("longitude_of_projection_origin"),
            semi_major_m=need("semi_major", "semi_major_axis"),
            semi_minor_m=need("semi_minor", "semi_minor_axis"),
            perspective_point_height_m=need("perspective_point_height"),
            sweep_angle_axis="x" if sweep == "x" else "y",
            latitude_of_projection_origin_deg=lat0,
        )

    @property
    def proj_string(self) -> str:
        return (
            f"+proj=geos +h={self.perspective_point_height_m!r} "
            f"+lon_0={self.longitude_of_projection_origin_deg!r} "
            f"+a={self.semi_major_m!r} +b={self.semi_minor_m!r} "
            f"+sweep={self.sweep_angle_axis} +units=m +no_defs"
        )

    @cached_property
    def crs(self) -> CRS:
        return CRS.from_user_input(self.proj_string)

    @cached_property
    def _to_geo(self) -> Transformer:
        # Same ellipsoid on both sides: no datum shift is applied, which is what the
        # producer's geolocation assumes.
        return Transformer.from_crs(self.crs, self.crs.geodetic_crs, always_xy=True)

    def microradian_to_metres(self, scan_urad: ArrayLike) -> FloatArray:
        """Scan angle in microradian -> metres on the perspective plane.

        ``x_m = x_urad * 1e-6 * h`` (h = perspective point height).
        """
        scale = MICRORADIAN * self.perspective_point_height_m
        return np.asarray(scan_urad, dtype=np.float64) * scale

    def metres_to_microradian(self, metres: ArrayLike) -> FloatArray:
        scale = MICRORADIAN * self.perspective_point_height_m
        return np.asarray(metres, dtype=np.float64) / scale

    def scan_to_latlon(
        self, x_urad: ArrayLike, y_urad: ArrayLike
    ) -> tuple[FloatArray, FloatArray]:
        """Scan angles (microradian) -> ``(lat, lon)`` degrees; NaN where the line of
        sight misses the Earth."""
        x, y = np.broadcast_arrays(
            np.asarray(x_urad, dtype=np.float64), np.asarray(y_urad, dtype=np.float64)
        )
        lon, lat = self._to_geo.transform(
            self.microradian_to_metres(x), self.microradian_to_metres(y), errcheck=False
        )
        lat = np.asarray(lat, dtype=np.float64)
        lon = np.asarray(lon, dtype=np.float64)
        bad = ~(np.isfinite(lat) & np.isfinite(lon))
        lat = np.where(bad, np.nan, lat)
        lon = np.where(bad, np.nan, lon)
        return lat, lon

    def latlon_to_scan(
        self, lat_deg: ArrayLike, lon_deg: ArrayLike
    ) -> tuple[FloatArray, FloatArray]:
        """``(lat, lon)`` degrees -> scan angles (microradian); NaN for points the
        satellite cannot see (far side, or invalid latitude)."""
        lat, lon = np.broadcast_arrays(
            np.asarray(lat_deg, dtype=np.float64), np.asarray(lon_deg, dtype=np.float64)
        )
        bad_in = (np.abs(lat) > 90.0) | ~np.isfinite(lat) | ~np.isfinite(lon)
        xm, ym = self._to_geo.transform(
            np.where(bad_in, 0.0, lon), np.where(bad_in, 0.0, lat),
            direction="INVERSE", errcheck=False,
        )  # fmt: skip
        x = self.metres_to_microradian(xm)
        y = self.metres_to_microradian(ym)
        bad = bad_in | ~(np.isfinite(x) & np.isfinite(y))
        return np.where(bad, np.nan, x), np.where(bad, np.nan, y)


# ---------------------------------------------------------------------------
# Full-disk grid geometry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TileExtent:
    """Where one tile sits in the full-disk grid and on the Earth."""

    tile_id: str
    row_offset: int
    col_offset: int
    height: int
    width: int
    bounds: Bounds
    """(min_lat, min_lon, max_lat, max_lon) of the tile's on-disk pixels, padded
    0.25 degrees (the sampling is not pixel-exact at the limb). Longitudes are
    unwrapped around the sub-satellite longitude, so they may exceed 180."""
    on_disk_fraction: float
    """Fraction of the sampled tile area that looks at the Earth (1.0 = full)."""


@dataclass(frozen=True)
class FullGridGeometry:
    """Pixel index <-> scan angle for the whole disk, plus the tile layout.

    ``x_urad = x_scale * col + x_offset`` and ``y_urad = y_scale * row + y_offset``
    (``y_scale`` is negative: row 0 is the northern edge). ``col``/``row`` are
    full-disk indices; pixel centres sit on integers.
    """

    projection: GeostationaryProjection
    x_scale_urad: float
    x_offset_urad: float
    y_scale_urad: float
    y_offset_urad: float
    n_rows: int
    n_cols: int
    tile_rows: int
    tile_cols: int
    resolution_km: float
    number_tiles: int = EXPECTED_TILES
    satellite: str = "Himawari-9"

    # -- construction ------------------------------------------------------

    @classmethod
    def from_attrs(
        cls,
        global_attrs: Mapping[str, Any],
        x_attrs: Mapping[str, Any],
        y_attrs: Mapping[str, Any],
        projection_attrs: Mapping[str, Any],
    ) -> FullGridGeometry:
        """Build from the attribute dictionaries of one tile file."""
        try:
            return cls(
                projection=GeostationaryProjection.from_attrs(projection_attrs),
                x_scale_urad=float(x_attrs["scale_factor"]),
                x_offset_urad=float(x_attrs["add_offset"]),
                y_scale_urad=float(y_attrs["scale_factor"]),
                y_offset_urad=float(y_attrs["add_offset"]),
                n_rows=int(global_attrs["product_rows"]),
                n_cols=int(global_attrs["product_columns"]),
                tile_rows=int(global_attrs["product_tile_height"]),
                tile_cols=int(global_attrs["product_tile_width"]),
                resolution_km=float(global_attrs["source_spatial_resolution"]),
                number_tiles=int(global_attrs["number_product_tiles"]),
            )
        except KeyError as exc:
            raise SatelliteDataError(f"tile file lacks required attribute {exc}") from exc

    @classmethod
    def from_file(cls, path: Path) -> FullGridGeometry:
        """Read the geometry from any one tile file of the band (the file is authoritative)."""
        import h5py  # imported here: only the file route needs it

        try:
            with h5py.File(str(path), "r") as f:
                return cls.from_attrs(
                    read_attrs(f),
                    read_attrs(f["x"]),
                    read_attrs(f["y"]),
                    read_attrs(f["fixedgrid_projection"]),
                )
        except (OSError, KeyError) as exc:
            raise SatelliteDataError(f"cannot read grid geometry from {path}: {exc}") from exc

    # -- pixel <-> scan angle <-> lat/lon ------------------------------------

    @property
    def tiles_per_row(self) -> int:
        return self.n_cols // self.tile_cols

    @property
    def tiles_per_col(self) -> int:
        return self.n_rows // self.tile_rows

    def col_to_x(self, col: ArrayLike) -> FloatArray:
        """Full-disk column index -> scan angle x, microradian (east positive)."""
        return np.asarray(col, dtype=np.float64) * self.x_scale_urad + self.x_offset_urad

    def row_to_y(self, row: ArrayLike) -> FloatArray:
        """Full-disk row index -> scan angle y, microradian (north positive)."""
        return np.asarray(row, dtype=np.float64) * self.y_scale_urad + self.y_offset_urad

    def x_to_col(self, x_urad: ArrayLike) -> FloatArray:
        return (np.asarray(x_urad, dtype=np.float64) - self.x_offset_urad) / self.x_scale_urad

    def y_to_row(self, y_urad: ArrayLike) -> FloatArray:
        return (np.asarray(y_urad, dtype=np.float64) - self.y_offset_urad) / self.y_scale_urad

    def pixel_to_latlon(
        self, row: ArrayLike, col: ArrayLike
    ) -> tuple[FloatArray, FloatArray]:
        """Full-disk pixel (row, col; fractional allowed, centres on integers) ->
        ``(lat, lon)`` degrees, NaN off the Earth's disk."""
        return self.projection.scan_to_latlon(self.col_to_x(col), self.row_to_y(row))

    def latlon_to_pixel(
        self, lat_deg: ArrayLike, lon_deg: ArrayLike
    ) -> tuple[FloatArray, FloatArray]:
        """``(lat, lon)`` -> fractional full-disk ``(row, col)``; NaN if not visible."""
        x, y = self.projection.latlon_to_scan(lat_deg, lon_deg)
        return self.y_to_row(y), self.x_to_col(x)

    def point_to_pixel(self, lat_deg: float, lon_deg: float) -> tuple[float, float] | None:
        """Scalar :meth:`latlon_to_pixel`; ``None`` when the point is off the disk."""
        row, col = self.latlon_to_pixel(lat_deg, lon_deg)
        r, c = float(row), float(col)
        return None if math.isnan(r) or math.isnan(c) else (r, c)

    def pixel_to_point(self, row: float, col: float) -> tuple[float, float] | None:
        """Scalar :meth:`pixel_to_latlon`; ``None`` when the pixel looks at space."""
        lat, lon = self.pixel_to_latlon(row, col)
        la, lo = float(lat), float(lon)
        return None if math.isnan(la) or math.isnan(lo) else (la, lo)

    # -- tiles ---------------------------------------------------------------

    def _cell_on_disk(self, r: int, c: int) -> bool:
        """Does grid cell (r, c) contain any pixel centre looking at the Earth?

        The visible disk is convex and symmetric in x and y, so the cell touches it
        iff its pixel centre *closest to the origin of the scan angles* does.
        """
        xs = self.col_to_x(np.array([c * self.tile_cols, (c + 1) * self.tile_cols - 1]))
        ys = self.row_to_y(np.array([r * self.tile_rows, (r + 1) * self.tile_rows - 1]))
        xn = float(np.clip(0.0, xs.min(), xs.max()))
        yn = float(np.clip(0.0, ys.min(), ys.max()))
        lat, _ = self.projection.scan_to_latlon(xn, yn)
        return bool(np.isfinite(lat))

    def _cells(self) -> tuple[tuple[int, int], ...]:
        return _grid_cells(self)

    def _compute_cells(self) -> tuple[tuple[int, int], ...]:
        cells = [
            (r, c)
            for r in range(self.tiles_per_col)
            for c in range(self.tiles_per_row)
            if self._cell_on_disk(r, c)
        ]
        if len(cells) != self.number_tiles:
            raise SatelliteDataError(
                f"layout inconsistent: {len(cells)} grid cells see the Earth but the product "
                f"declares number_product_tiles={self.number_tiles}"
            )
        return tuple(cells)

    def tile_cell(self, tile_id: str) -> tuple[int, int]:
        """(row_cell, col_cell) of 'T044'. Tiles are numbered row-major over the cells
        that see the Earth (VERIFIED: T001 offset (0, 4400), T044 (8800, 19800),
        T059 (13200, 8800), T069 (15400, 8800), T078 (17600, 8800), T088 (19800, 15400))."""
        try:
            n = int(tile_id.lstrip("Tt"))
        except ValueError as exc:
            raise ValueError(f"not a tile id: {tile_id!r}") from exc
        cells = self._cells()
        if not 1 <= n <= len(cells):
            raise ValueError(f"tile id {tile_id!r} outside T001..T{len(cells):03d}")
        return cells[n - 1]

    def tile_id_for_cell(self, r: int, c: int) -> str | None:
        try:
            return f"T{self._cells().index((r, c)) + 1:03d}"
        except ValueError:
            return None

    def tile_offsets(self, tile_id: str) -> tuple[int, int]:
        """(row_offset, column_offset) of the tile's first pixel in the full grid -
        the values the file carries as ``tile_row_offset``/``tile_column_offset``."""
        r, c = self.tile_cell(tile_id)
        return r * self.tile_rows, c * self.tile_cols

    def tile_pixel_to_latlon(
        self, tile_id: str, row: ArrayLike, col: ArrayLike
    ) -> tuple[FloatArray, FloatArray]:
        """Tile-local pixel (row, col) -> ``(lat, lon)``; NaN off the disk."""
        r0, c0 = self.tile_offsets(tile_id)
        return self.pixel_to_latlon(np.asarray(row, dtype=np.float64) + r0,
                                    np.asarray(col, dtype=np.float64) + c0)  # fmt: skip

    def latlon_to_tile_pixel(
        self, tile_id: str, lat_deg: ArrayLike, lon_deg: ArrayLike
    ) -> tuple[FloatArray, FloatArray]:
        """``(lat, lon)`` -> fractional tile-local ``(row, col)``. NaN only if the point
        is off the disk; a point outside *this tile* gives indices outside
        ``[0, tile_rows) x [0, tile_cols)``."""
        r0, c0 = self.tile_offsets(tile_id)
        row, col = self.latlon_to_pixel(lat_deg, lon_deg)
        return row - r0, col - c0

    def tile_bounds(self, tile_id: str) -> Bounds:
        """Geographic bounds of the tile's on-disk part (see :class:`TileExtent`)."""
        return self._extent(tile_id).bounds

    def _extent(self, tile_id: str) -> TileExtent:
        r0, c0 = self.tile_offsets(tile_id)
        n = 65
        rows = r0 + np.linspace(0, self.tile_rows - 1, n)
        cols = c0 + np.linspace(0, self.tile_cols - 1, n)
        rr, cc = np.meshgrid(rows, cols, indexing="ij")
        lat, lon = self.pixel_to_latlon(rr, cc)
        ok = np.isfinite(lat)
        if not ok.any():  # a cell with no sampled on-disk point (sliver at the limb)
            raise SatelliteDataError(f"{tile_id} has no sampled on-disk pixel")
        lon_u = wrap_longitude(lon[ok], self.projection.longitude_of_projection_origin_deg)
        pad = 0.25
        bounds = (
            max(-90.0, float(lat[ok].min()) - pad),
            float(lon_u.min()) - pad,
            min(90.0, float(lat[ok].max()) + pad),
            float(lon_u.max()) + pad,
        )
        return TileExtent(
            tile_id=tile_id, row_offset=r0, col_offset=c0, height=self.tile_rows,
            width=self.tile_cols, bounds=bounds, on_disk_fraction=float(ok.mean()),
        )  # fmt: skip

    @property
    def signature(self) -> str:
        """Stable digest of everything that determines the tile layout."""
        p = self.projection
        blob = json.dumps(
            [
                p.longitude_of_projection_origin_deg, p.semi_major_m, p.semi_minor_m,
                p.perspective_point_height_m, p.sweep_angle_axis,
                self.x_scale_urad, self.x_offset_urad, self.y_scale_urad, self.y_offset_urad,
                self.n_rows, self.n_cols, self.tile_rows, self.tile_cols, self.number_tiles,
            ],
            separators=(",", ":"),
        )  # fmt: skip
        return hashlib.sha1(blob.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Reference (prior) geometry: lets us choose tiles BEFORE downloading any
# ---------------------------------------------------------------------------

# Measured from OR_HFD-005-B11-M1C03-T0xx_GH9_s20263060300000 on 2026-11-02 (and
# confirmed identical in T044, T059, T069, T078). It exists only to choose which
# tiles to download. Every tile that is then decoded is geolocated from ITS OWN
# attributes, and decode_tile() refuses a tile whose attributes disagree with the
# layout implied by this prior - so a wrong prior (e.g. a new satellite) fails
# loudly instead of mislocating clouds.
_PRIOR_PROJECTION = {
    "grid_mapping_name": "geostationary",
    "longitude_of_projection_origin": 140.7,
    "latitude_of_projection_origin": 0.0,
    "semi_major": 6378137.0,
    "semi_minor": 6356752.31414,
    "perspective_point_height": 35785831.0,
    "sweep_angle_axis": "y",
}
_PRIOR_PIXEL_URAD_AT_0P5KM = 13.97199774457005
_PRIOR_FULL_DISK_PIXELS_AT_0P5KM = 22000
_PRIOR_TILE_PIXELS_AT_0P5KM = 2200


def reference_geometry(band: str = "C03") -> FullGridGeometry:
    """Prior geometry for ``band``, from the 2026-11-02 measurement scaled to the band's
    resolution.

    The packed coordinates of band ``f`` times coarser than 0.5 km obey (VERIFIED on
    C01/C02 at 1 km and C13 at 2 km): ``scale = f * s0`` and
    ``offset = -scale * (N / 2 + 0.5)`` with ``N = 22000 / f``.
    """
    res = band_resolution_km(normalise_band(band))
    f = res / 0.5
    n = round(_PRIOR_FULL_DISK_PIXELS_AT_0P5KM / f)
    scale = _PRIOR_PIXEL_URAD_AT_0P5KM * f
    offset = scale * (n / 2.0 + 0.5)
    return FullGridGeometry(
        projection=GeostationaryProjection.from_attrs(_PRIOR_PROJECTION),
        x_scale_urad=scale,
        x_offset_urad=-offset,
        y_scale_urad=-scale,
        y_offset_urad=offset,
        n_rows=n,
        n_cols=n,
        tile_rows=round(_PRIOR_TILE_PIXELS_AT_0P5KM / f),
        tile_cols=round(_PRIOR_TILE_PIXELS_AT_0P5KM / f),
        resolution_km=res,
        number_tiles=EXPECTED_TILES,
        satellite="Himawari-9",
    )


# ---------------------------------------------------------------------------
# Tile map: tile id -> extent, cached on disk
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _grid_cells(geometry: FullGridGeometry) -> tuple[tuple[int, int], ...]:
    return geometry._compute_cells()


@lru_cache(maxsize=16)
def _compute_layout(geometry: FullGridGeometry) -> dict[str, TileExtent]:
    out: dict[str, TileExtent] = {}
    for i in range(1, geometry.number_tiles + 1):
        out[f"T{i:03d}"] = geometry._extent(f"T{i:03d}")
    return out


def _layout_path(cache_dir: Path, geometry: FullGridGeometry) -> Path:
    return Path(cache_dir) / "satellite" / "tile_maps" / f"tile_map-{geometry.signature}.json"


def tile_layout(
    geometry: FullGridGeometry, *, cache_dir: Path | None = None
) -> dict[str, TileExtent]:
    """Extent of every tile of ``geometry``, from a JSON map on disk when present.

    Computed once from the projection (no download) and written to
    ``<cache_dir>/satellite/tile_maps/tile_map-<signature>.json``; the signature
    covers the projection and grid, so a different satellite never reads a stale map.
    """
    path = _layout_path(cache_dir, geometry) if cache_dir is not None else None
    if path is not None:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc.get("signature") == geometry.signature and doc.get("schema") == 1:
                return {
                    k: TileExtent(
                        tile_id=k, row_offset=v["row_offset"], col_offset=v["col_offset"],
                        height=v["height"], width=v["width"], bounds=tuple(v["bounds"]),
                        on_disk_fraction=v["on_disk_fraction"],
                    )
                    for k, v in doc["tiles"].items()
                }  # fmt: skip
        except (OSError, ValueError, KeyError, TypeError):
            pass
    layout = _compute_layout(geometry)
    if path is not None:
        doc = {
            "schema": 1,
            "signature": geometry.signature,
            "tiles": {
                k: {
                    "row_offset": e.row_offset, "col_offset": e.col_offset,
                    "height": e.height, "width": e.width, "bounds": list(e.bounds),
                    "on_disk_fraction": e.on_disk_fraction,
                }
                for k, e in layout.items()
            },
        }  # fmt: skip
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            os.replace(tmp, path)
        except OSError as exc:  # a cache that cannot be written must not break a lookup
            log.warning("could not write tile map %s: %s", path, exc)
    return layout


def _sample_axis(lo: float, hi: float, step: float, cap: int = 400) -> FloatArray:
    n = min(cap, max(2, math.ceil((hi - lo) / step) + 1))
    return np.linspace(lo, hi, n)


def tile_ids_covering(
    bounds: Bounds,
    *,
    band: str = "C03",
    geometry: FullGridGeometry | None = None,
    cache_dir: Path | None = None,
) -> list[str]:
    """Tile ids whose pixels fall inside the lat/lon box ``bounds`` - no network.

    The box is sampled every ~0.1 degree (about 11 km, 1% of a tile), each sample is
    projected into the fixed grid, and the tiles those pixels belong to are
    returned. That is exact up to the sampling step and, unlike comparing bounding
    boxes, does not report a tile just because its (curved) bounding box overlaps.
    A box that sees no part of the disk returns ``[]``.

    ``bounds`` is ``(min_lat, min_lon, max_lat, max_lon)``; longitudes may be given in
    either (-180, 180] or unwrapped form. ``geometry`` defaults to
    :func:`reference_geometry`; ``cache_dir`` enables the on-disk tile map.
    """
    geometry = geometry or reference_geometry(band)
    min_lat, min_lon, max_lat, max_lon = bounds
    if min_lat > max_lat:
        raise ValueError(f"min_lat {min_lat} exceeds max_lat {max_lat}")
    lon0 = geometry.projection.longitude_of_projection_origin_deg
    # Unwrap the western edge, then make the eastern edge follow it so min <= max.
    w = float(wrap_longitude(min_lon, lon0))
    span = max_lon - min_lon
    if span < 0:  # box written across the antimeridian, e.g. (170, -170)
        span += 360.0
    span = min(span, 360.0)
    lats = _sample_axis(max(-90.0, min_lat), min(90.0, max_lat), 0.1)
    lons = _sample_axis(w, w + span, 0.1)
    la, lo = np.meshgrid(lats, lons, indexing="ij")
    row, col = geometry.latlon_to_pixel(la, lo)
    ok = np.isfinite(row) & np.isfinite(col)
    if not ok.any():
        return []
    rc = np.floor(row[ok] / geometry.tile_rows).astype(int)
    cc = np.floor(col[ok] / geometry.tile_cols).astype(int)
    valid = (rc >= 0) & (rc < geometry.tiles_per_col) & (cc >= 0) & (cc < geometry.tiles_per_row)
    ids: set[str] = set()
    for r, c in set(zip(rc[valid].tolist(), cc[valid].tolist(), strict=True)):
        tid = geometry.tile_id_for_cell(r, c)
        if tid is not None:
            ids.add(tid)
    if cache_dir is not None:
        tile_layout(geometry, cache_dir=cache_dir)  # materialise the tile map on disk
    return sorted(ids)


def tiles_covering(
    bounds: Bounds,
    slot_time_utc: datetime,
    *,
    band: str = "C03",
    geometry: FullGridGeometry | None = None,
    cache_dir: Path | None = None,
    settings: Settings | None = None,
) -> list[SatelliteTile]:
    """The published tiles of ``band`` at ``slot_time_utc`` that intersect ``bounds``.

    Geometry decides *which* tiles (no download, see :func:`tile_ids_covering`); one
    paginated listing then supplies their keys. ``[]`` if the box is off the disk
    (no network call at all) or the slot has no files. If the slot is partly
    published and a needed tile is absent, :class:`TileMissingError` is raised
    instead of returning a mosaic with a hole.
    """
    from rwi.satellite.source import list_tiles  # local: source does not import geo

    settings = settings or get_settings()
    if cache_dir is None:
        cache_dir = settings.cache_dir
    wanted = tile_ids_covering(bounds, band=band, geometry=geometry, cache_dir=cache_dir)
    if not wanted:
        return []
    available = {t.tile_id: t for t in list_tiles(ensure_utc(slot_time_utc), band=band,
                                                   settings=settings)}  # fmt: skip
    if not available:
        return []
    missing = [t for t in wanted if t not in available]
    if missing:
        raise TileMissingError(
            f"slot {ensure_utc(slot_time_utc):%Y-%m-%dT%H:%MZ} is missing tiles {missing} "
            f"of {band}; it is probably still being published - retry shortly"
        )
    return [available[t] for t in wanted]
