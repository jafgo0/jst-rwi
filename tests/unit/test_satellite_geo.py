"""Geolocation of Himawari fixed-grid tiles (rwi.satellite.geo).

Fixtures: ``tests/fixtures/satellite/*.nc`` are small crops of real tiles (2026-11-02
03:00Z) with their real attributes - see ``build_fixtures.py`` there. No network.

The failures guarded here do not crash; they draw a plausible-looking image in the
wrong place: a factor 1e6 in the scan-angle unit, the wrong sweep axis, a mirrored
image, a coordinate invented for a pixel that looks at space.
"""

from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path

import h5py
import numpy as np
import pytest

from rwi.satellite import geo
from rwi.satellite.geo import (
    FullGridGeometry,
    GeostationaryProjection,
    reference_geometry,
    tile_ids_covering,
    tile_layout,
    wrap_longitude,
)
from rwi.satellite.tiles import SatelliteDataError, read_attrs

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "satellite"
T059 = FIX / "satellite_T059_c03_0300_centre.nc"
T069 = FIX / "satellite_T069_c03_0300_north_edge.nc"
T044 = FIX / "satellite_T044_c03_0300_limb.nc"
ROUTE = Path(__file__).resolve().parents[2] / "data" / "routes" / "bwsc-2027-stuart-highway.geojson"

SUBSAT_LON = 140.7
H = 35785831.0


@pytest.fixture(scope="module")
def geometry() -> FullGridGeometry:
    return FullGridGeometry.from_file(T059)


def _projection_attrs() -> dict[str, object]:
    with h5py.File(T059, "r") as f:
        return dict(read_attrs(f["fixedgrid_projection"]))


# ---------------------------------------------------------------------------
# Units: microradian
# ---------------------------------------------------------------------------


def test_microradian_conversion_matches_hand_value(geometry: FullGridGeometry) -> None:
    """1000 microradian x 35,785,831 m = 35,785.831 m; a x1e6 slip cannot pass.

    Hand calculation: 1000e-6 rad * 35 785 831 m = 35 785.831 m (the length on the
    perspective plane that subtends 1 milliradian at the satellite).
    """
    proj = geometry.projection
    assert float(proj.microradian_to_metres(1000.0)) == pytest.approx(35_785.831, rel=1e-12)
    assert float(proj.microradian_to_metres(-250.0)) == pytest.approx(-8_946.45775, rel=1e-12)
    # the inverse really is the inverse
    assert float(proj.metres_to_microradian(35_785.831)) == pytest.approx(1000.0, rel=1e-12)
    # and it is NOT the "raw microradian passed as metres" mistake, which would give 1000
    assert float(proj.microradian_to_metres(1000.0)) > 3.0e4


def _ray_to_latlon(x_urad: float, y_urad: float, *, sweep: str) -> tuple[float, float]:
    """Closed-form line-of-sight / ellipsoid intersection, sharing no code with PROJ.

    The unit vector from the satellite towards the Earth has components
    ``(cos x cos y, sin x cos y, sin y)`` for ``sweep='y'`` (CGMS LRIT/HRIT global
    specification, section 4.4.3.2: Meteosat, Himawari) and
    ``(cos x cos y, sin x, cos x sin y)`` for ``sweep='x'`` (GOES-R Product User's Guide
    vol. 3, section 4.2.8.2). Intersecting it with the ellipsoid ``X^2+Y^2+(a/b)^2 Z^2 = a^2``
    from a satellite at distance ``a + h`` gives the ground point.
    """
    attrs = _projection_attrs()
    a, b = float(attrs["semi_major"]), float(attrs["semi_minor"])  # type: ignore[arg-type]
    big_h = H + a
    k = (a * a) / (b * b)
    x, y = x_urad * 1e-6, y_urad * 1e-6
    if sweep == "y":
        dx, dy, dz = math.cos(x) * math.cos(y), math.sin(x) * math.cos(y), math.sin(y)
    else:
        dx, dy, dz = math.cos(x) * math.cos(y), math.sin(x), math.cos(x) * math.sin(y)
    qa = dx * dx + dy * dy + k * dz * dz
    qb = -2.0 * big_h * dx
    qc = big_h * big_h - a * a
    r = (-qb - math.sqrt(qb * qb - 4.0 * qa * qc)) / (2.0 * qa)
    sx, sy, sz = r * dx, -r * dy, r * dz
    lat = math.degrees(math.atan(k * sz / math.hypot(big_h - sx, sy)))
    lon = SUBSAT_LON - math.degrees(math.atan(sy / (big_h - sx)))
    return lat, lon


_OFF_AXIS = [(0, 0), (100_000, 0), (-80_000, 60_000), (30_000, -90_000), (-120_000, -50_000),
             (5_000, 140_000)]  # fmt: skip


@pytest.mark.science
def test_scan_angle_to_latlon_matches_independent_closed_form(
    geometry: FullGridGeometry,
) -> None:
    """PROJ ``geos`` (sweep y) against the closed-form ray-ellipsoid intersection above.

    Tolerance 1e-6 deg (~0.1 m): both are exact on the same ellipsoid, so a larger gap
    means a unit, sign or sweep error. The 100,000 urad case would be ~1e5 m off if the
    microradian were passed as metres.
    """
    for x_urad, y_urad in _OFF_AXIS:
        want_lat, want_lon = _ray_to_latlon(x_urad, y_urad, sweep="y")
        lat, lon = geometry.projection.scan_to_latlon(x_urad, y_urad)
        assert float(lat) == pytest.approx(want_lat, abs=1e-6)
        assert float(lon) == pytest.approx(want_lon, abs=1e-6)


def test_wrong_sweep_axis_gives_visibly_different_places(geometry: FullGridGeometry) -> None:
    """The GOES convention (sweep x) moves off-axis pixels by tens of kilometres, so a
    sweep mix-up cannot hide behind the tolerance of the previous test."""
    wrong = dataclasses.replace(geometry.projection, sweep_angle_axis="x")
    for x_urad, y_urad in _OFF_AXIS[2:]:
        lat_y, lon_y = geometry.projection.scan_to_latlon(x_urad, y_urad)
        lat_x, lon_x = wrong.scan_to_latlon(x_urad, y_urad)
        assert abs(float(lat_y) - float(lat_x)) + abs(float(lon_y) - float(lon_x)) > 0.01
        goes_lat, _ = _ray_to_latlon(x_urad, y_urad, sweep="x")
        assert float(lat_x) == pytest.approx(
            goes_lat, abs=1e-6
        )  # and the x formula is the GOES one


# ---------------------------------------------------------------------------
# Sub-satellite point and grid centre
# ---------------------------------------------------------------------------


@pytest.mark.science
def test_subsatellite_point_is_the_grid_centre(geometry: FullGridGeometry) -> None:
    """(0 N, 140.7 E) is where x = y = 0, which the file puts at full-disk pixel (11000.5,
    11000.5): the centre of a 22,000-pixel grid is 10999.5, so the product is shifted by
    exactly one pixel (0.5 km) from the arithmetic centre. Tolerance 1e-3 pixel for
    the reproduction of the file's own affine coefficients; within 1 pixel of 10999.5."""
    lat, lon = geometry.projection.scan_to_latlon(0.0, 0.0)
    assert float(lat) == pytest.approx(0.0, abs=1e-9)
    assert float(lon) == pytest.approx(SUBSAT_LON, abs=1e-9)

    pix = geometry.point_to_pixel(0.0, SUBSAT_LON)
    assert pix is not None
    row, col = pix
    assert row == pytest.approx(11000.5, abs=1e-3)
    assert col == pytest.approx(11000.5, abs=1e-3)
    assert abs(row - (geometry.n_rows - 1) / 2) <= 1.0
    assert abs(col - (geometry.n_cols - 1) / 2) <= 1.0


@pytest.mark.science
@pytest.mark.parametrize(("path", "tile_id"), [(T059, "T059"), (T069, "T069")])
def test_tile_centre_matches_the_producers_own_metadata(path: Path, tile_id: str) -> None:
    """Independent reference: NOAA's ``tile_center_latitude/longitude`` attributes.

    They are written by the producer's own geolocation software, not by PROJ. The tile
    centre is pixel index 1100 of 2200 (verified here, and the same on T044). Tolerance
    1e-4 deg (11 m); measured agreement is 1.4e-5 deg (T059) and 2.6e-5 deg (T069).
    """
    geom = FullGridGeometry.from_file(path)
    with h5py.File(path, "r") as f:
        attrs = read_attrs(f)
    row0, col0 = geom.tile_offsets(tile_id)
    lat, lon = geom.pixel_to_latlon(row0 + 1100, col0 + 1100)
    assert float(lat) == pytest.approx(float(attrs["tile_center_latitude"]), abs=1e-4)
    assert float(lon) == pytest.approx(float(attrs["tile_center_longitude"]), abs=1e-4)


@pytest.mark.science
def test_sweep_axis_y_is_honoured_and_x_would_be_wrong() -> None:
    """Himawari declares ``sweep_angle_axis = y``; GOES uses ``x``. Using the wrong one
    does not crash - it displaces every off-axis pixel.

    Reference: the producer's tile centre of T044 (latitude 5.460695, east side of the
    disk). With ``y`` we agree to 1e-4 deg; with ``x`` the same pixel lands 0.05 deg (6 km)
    away. T044's longitude attribute is not used: the file stores 19.06 where the true
    value is -160.94 (a 180-degree wrap bug in the producer's metadata).
    """
    geom = FullGridGeometry.from_file(T044)
    with h5py.File(T044, "r") as f:
        want_lat = float(read_attrs(f)["tile_center_latitude"])
    row0, col0 = geom.tile_offsets("T044")

    lat_y, _ = geom.pixel_to_latlon(row0 + 1100, col0 + 1100)
    flipped = dataclasses.replace(
        geom, projection=dataclasses.replace(geom.projection, sweep_angle_axis="x")
    )
    lat_x, _ = flipped.pixel_to_latlon(row0 + 1100, col0 + 1100)

    assert geom.projection.sweep_angle_axis == "y"
    assert abs(float(lat_y) - want_lat) < 1e-4
    assert abs(float(lat_x) - want_lat) > 0.04  # would pass a 'wrong sweep' bug through


def test_orientation_is_not_mirrored(geometry: FullGridGeometry) -> None:
    """East is to the right and north is up in pixel space (row 0 = north)."""
    row, col = 13_700.0, 8_900.0
    lat0, lon0 = (float(v) for v in geometry.pixel_to_latlon(row, col))
    lat_right, lon_right = (float(v) for v in geometry.pixel_to_latlon(row, col + 100))
    lat_up, lon_up = (float(v) for v in geometry.pixel_to_latlon(row - 100, col))
    assert lon_right > lon0  # increasing column = eastward
    assert lat_up > lat0  # decreasing row = northward
    # and the inverse agrees on the direction
    r_n, _ = geometry.latlon_to_pixel(lat0 + 0.5, lon0)
    _, c_e = geometry.latlon_to_pixel(lat0, lon0 + 0.5)
    assert float(r_n) < row
    assert float(c_e) > col
    # the scale is right too: 100 pixels = 50 km nominal, 0.45 deg of latitude at ~0.5 km/px
    assert 0.35 < abs(lat_up - lat0) < 0.70
    assert abs(lon_up - lon0) < abs(lat_up - lat0)  # N-S step is nearly meridional
    assert abs(lat_right - lat0) < abs(lon_right - lon0)  # E-W step is nearly zonal


def test_pixel_centres_are_on_integers_and_round_trip(geometry: FullGridGeometry) -> None:
    for name, lat, lon in [("Darwin", -12.4634, 130.8456), ("Alice", -23.698, 133.8807),
                           ("Adelaide", -34.9285, 138.6007)]:  # fmt: skip
        row, col = geometry.latlon_to_pixel(lat, lon)
        lat2, lon2 = geometry.pixel_to_latlon(row, col)
        assert float(lat2) == pytest.approx(lat, abs=1e-9), name
        assert float(lon2) == pytest.approx(lon, abs=1e-9), name
        row2, col2 = geometry.latlon_to_pixel(lat2, lon2)
        assert float(row2) == pytest.approx(float(row), abs=1e-6)
        assert float(col2) == pytest.approx(float(col), abs=1e-6)
    pix = geometry.point_to_pixel(-12.4634, 130.8456)
    assert pix is not None
    assert pix == pytest.approx((13714.0, 8883.3), abs=1.0)  # Darwin is in tile T059


# ---------------------------------------------------------------------------
# Off the disk
# ---------------------------------------------------------------------------


@pytest.mark.science
def test_off_disk_returns_nan_not_a_coordinate(geometry: FullGridGeometry) -> None:
    """The limb is at ~151,860 urad on the equator (asin(a/(a+h))); 160,000 urad looks at space."""
    lat, lon = geometry.projection.scan_to_latlon(160_000.0, 0.0)
    assert math.isnan(float(lat)) and math.isnan(float(lon))
    lat, lon = geometry.projection.scan_to_latlon(np.array([0.0, 160_000.0, 0.0]),
                                                 np.array([0.0, 0.0, 160_000.0]))  # fmt: skip
    assert np.isfinite(lat[0]) and np.isnan(lat[1:]).all() and np.isnan(lon[1:]).all()
    # corner of the full-disk grid
    assert geometry.pixel_to_point(0, 0) is None
    assert geometry.pixel_to_point(21_999, 21_999) is None
    assert geometry.pixel_to_point(11_000, 11_000) is not None

    # other direction: the far side of the Earth, a pole beyond the limb, an invalid latitude
    lats = np.array([0.0, 0.0, 91.0])
    lons = np.array([SUBSAT_LON - 180.0, SUBSAT_LON + 100.0, 140.0])
    row, col = geometry.latlon_to_pixel(lats, lons)
    assert np.isnan(row).all() and np.isnan(col).all()
    assert geometry.point_to_pixel(57.78, 14.16) is None  # Jonkoping: Europe, not visible


def test_longitude_wrapping() -> None:
    assert float(wrap_longitude(-160.0, SUBSAT_LON)) == pytest.approx(200.0)
    assert float(wrap_longitude(200.0, 0.0)) == pytest.approx(-160.0)
    assert float(wrap_longitude(140.7, SUBSAT_LON)) == pytest.approx(140.7)


# ---------------------------------------------------------------------------
# The CRS comes from the file, not from the code
# ---------------------------------------------------------------------------


def test_crs_is_built_from_the_attributes_given() -> None:
    attrs = _projection_attrs()
    here = GeostationaryProjection.from_attrs(attrs)
    # A hypothetical Himawari-10 at 145 E must be decoded at 145 E without a code change.
    moved = GeostationaryProjection.from_attrs({**attrs, "longitude_of_projection_origin": 145.0})
    assert float(here.scan_to_latlon(0.0, 0.0)[1]) == pytest.approx(140.7)
    assert float(moved.scan_to_latlon(0.0, 0.0)[1]) == pytest.approx(145.0)
    assert "+sweep=y" in here.proj_string and "+lon_0=140.7" in here.proj_string
    taller = GeostationaryProjection.from_attrs({**attrs, "perspective_point_height": 3.6e7})
    assert float(taller.microradian_to_metres(1000.0)) == pytest.approx(36_000.0)


@pytest.mark.parametrize(
    "change",
    [
        {"grid_mapping_name": "lambert_conformal_conic"},
        {"sweep_angle_axis": "z"},
        {"sweep_angle_axis": None},
        {"perspective_point_height": None},
        {"latitude_of_projection_origin": 10.0},
    ],
)
def test_unusable_projection_attributes_raise(change: dict[str, object]) -> None:
    attrs = {k: v for k, v in {**_projection_attrs(), **change}.items() if v is not None}
    with pytest.raises(SatelliteDataError):
        GeostationaryProjection.from_attrs(attrs)


def test_geometry_from_file_uses_the_files_own_numbers(geometry: FullGridGeometry) -> None:
    assert geometry.n_rows == geometry.n_cols == 22_000
    assert geometry.tile_rows == geometry.tile_cols == 2_200
    assert geometry.number_tiles == 88
    assert geometry.resolution_km == 0.5
    assert geometry.x_scale_urad == pytest.approx(13.97199774457005)
    assert geometry.y_scale_urad == pytest.approx(-13.97199774457005)
    assert geometry.projection.perspective_point_height_m == H


def test_missing_attribute_is_an_error_not_a_default(tmp_path: Path) -> None:
    bad = tmp_path / "bad.nc"
    with h5py.File(T059, "r") as src, h5py.File(bad, "w") as dst:
        for k in src.attrs:
            if k != "product_rows":
                dst.attrs[k] = src.attrs[k]
        for name in ("x", "y", "fixedgrid_projection"):
            d = dst.create_dataset(name, data=src[name][()])
            for k in src[name].attrs:
                if k not in ("DIMENSION_LIST", "REFERENCE_LIST"):
                    d.attrs[k] = src[name].attrs[k]
    with pytest.raises(SatelliteDataError, match="product_rows"):
        FullGridGeometry.from_file(bad)


# ---------------------------------------------------------------------------
# Tile layout and tiles_covering
# ---------------------------------------------------------------------------


def test_reference_layout_reproduces_the_offsets_seen_in_real_files() -> None:
    """tile_row_offset/tile_column_offset read from real files (2026-11-02):
    T001 (0, 4400), T044 (8800, 19800), T059 (13200, 8800), T069 (15400, 8800),
    T078 (17600, 8800), T088 (19800, 15400)."""
    g = reference_geometry("C03")
    assert g.tile_offsets("T001") == (0, 4400)
    assert g.tile_offsets("T044") == (8800, 19800)
    assert g.tile_offsets("T059") == (13200, 8800)
    assert g.tile_offsets("T069") == (15400, 8800)
    assert g.tile_offsets("T078") == (17600, 8800)
    assert g.tile_offsets("T088") == (19800, 15400)
    assert len({g.tile_cell(f"T{i:03d}") for i in range(1, 89)}) == 88
    with pytest.raises(ValueError, match="outside"):
        g.tile_offsets("T089")


def test_reference_geometry_agrees_with_the_real_file(geometry: FullGridGeometry) -> None:
    ref = reference_geometry("C03")
    assert ref.x_scale_urad == pytest.approx(geometry.x_scale_urad, rel=1e-12)
    assert ref.x_offset_urad == pytest.approx(geometry.x_offset_urad, abs=1e-6)
    assert ref.y_offset_urad == pytest.approx(geometry.y_offset_urad, abs=1e-6)
    assert (
        ref.signature == geometry.signature
        or abs(ref.x_offset_urad - geometry.x_offset_urad) < 1e-6
    )


def test_reference_geometry_scales_to_the_coarser_bands() -> None:
    """Values read from the real C01 (1 km) and C13 (2 km) tiles of 2026-11-02."""
    c01, c13 = reference_geometry("C01"), reference_geometry("C13")
    assert (c01.n_rows, c01.tile_rows, c01.x_scale_urad) == (
        11_000,
        1_100,
        pytest.approx(27.9439958),
    )
    assert c01.x_offset_urad == pytest.approx(-153705.94906556673, abs=5e-3)
    assert c01.tile_offsets("T059") == (6600, 4400)
    assert (c13.n_rows, c13.tile_rows) == (5_500, 550)
    assert c13.x_offset_urad == pytest.approx(-153719.91730803743, abs=5e-3)
    assert c13.tile_offsets("T059") == (3300, 2200)


def test_a_wrong_cell_count_is_refused() -> None:
    bad = dataclasses.replace(reference_geometry("C03"), number_tiles=100)
    with pytest.raises(SatelliteDataError, match="inconsistent"):
        bad.tile_offsets("T001")


def _route_points() -> np.ndarray:
    doc = json.loads(ROUTE.read_text(encoding="utf-8"))
    line = next(f for f in doc["features"] if f["geometry"]["type"] == "LineString")
    return np.asarray(line["geometry"]["coordinates"], dtype=float)  # lon, lat


RACE_TILES = ["T059", "T069", "T078"]


@pytest.mark.science
def test_darwin_to_adelaide_needs_exactly_three_tiles() -> None:
    """Every vertex of the bundled Stuart Highway route lies in T059, T069 or T078, and
    the route's bounding box selects exactly those three of the 88 tiles."""
    pts = _route_points()
    g = reference_geometry("C03")
    row, col = g.latlon_to_pixel(pts[:, 1], pts[:, 0])
    assert np.isfinite(row).all()
    cells = {
        g.tile_id_for_cell(int(r // 2200), int(c // 2200)) for r, c in zip(row, col, strict=True)
    }
    assert sorted(cells) == RACE_TILES  # type: ignore[type-var]

    box = (
        float(pts[:, 1].min()),
        float(pts[:, 0].min()),
        float(pts[:, 1].max()),
        float(pts[:, 0].max()),
    )
    found = tile_ids_covering(box)
    assert found == RACE_TILES
    assert len(found) < 88


def test_covering_handles_boxes_that_matter() -> None:
    assert tile_ids_covering((-12.5, 130.8, -12.4, 130.9)) == ["T059"]  # Darwin only
    assert tile_ids_covering((-35.0, 138.5, -34.9, 138.7)) == ["T078"]  # Adelaide only
    assert "T058" in tile_ids_covering((-13.0, 129.0, -12.0, 131.0))  # box straddling a tile edge
    # a box off the disk (Europe; mid-Atlantic) returns nothing
    assert tile_ids_covering((50.0, 0.0, 60.0, 10.0)) == []
    assert tile_ids_covering((-10.0, -60.0, 10.0, -20.0)) == []
    # the whole disk is all 88 tiles
    assert len(tile_ids_covering((-90.0, -180.0, 90.0, 180.0))) == 88


def test_covering_crosses_the_antimeridian_either_way_it_is_written() -> None:
    a = tile_ids_covering((0.0, 170.0, 10.0, -170.0))
    b = tile_ids_covering((0.0, 170.0, 10.0, 190.0))
    assert a == b
    assert "T044" in a
    with pytest.raises(ValueError, match="min_lat"):
        tile_ids_covering((10.0, 0.0, 0.0, 10.0))


def test_tile_bounds_contain_the_tile_centre_and_nothing_wild() -> None:
    g = reference_geometry("C03")
    south, west, north, east = g.tile_bounds("T059")
    assert south < -15.2 < north
    assert west < 135.5 < east
    assert -21 < south and north < -9  # ~11 degrees, not 80
    full = g.tile_bounds("T001")  # mostly space: bounds cover only the on-disk part
    assert full[0] > 45


def test_tile_map_is_cached_on_disk_and_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    g = reference_geometry("C03")
    first = tile_layout(g, cache_dir=tmp_path)
    files = list((tmp_path / "satellite" / "tile_maps").glob("tile_map-*.json"))
    assert len(files) == 1
    assert len(first) == 88
    doc = json.loads(files[0].read_text(encoding="utf-8"))
    assert doc["signature"] == g.signature

    def boom(_: FullGridGeometry) -> dict[str, geo.TileExtent]:
        raise AssertionError("tile map recomputed instead of read from disk")

    monkeypatch.setattr(geo, "_compute_layout", boom)
    second = tile_layout(g, cache_dir=tmp_path)
    assert second["T059"].bounds == pytest.approx(first["T059"].bounds)
    assert second["T059"].row_offset == 13200

    # a different satellite geometry never reads this map
    other = dataclasses.replace(g, x_offset_urad=g.x_offset_urad + 1.0)
    assert other.signature != g.signature


def test_tile_pixel_helpers_agree_with_full_grid(geometry: FullGridGeometry) -> None:
    lat, lon = geometry.tile_pixel_to_latlon("T059", 1100, 1100)
    row, col = geometry.latlon_to_tile_pixel("T059", float(lat), float(lon))
    assert float(row) == pytest.approx(1100.0, abs=1e-6)
    assert float(col) == pytest.approx(1100.0, abs=1e-6)
    # a point in the tile to the south is outside this tile's index range
    row_s, _ = geometry.latlon_to_tile_pixel("T059", -26.29, 135.07)
    assert float(row_s) > 2200
