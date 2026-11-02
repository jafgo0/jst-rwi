"""Decoding, resampling and scene assembly (rwi.satellite.scene), on real-pixel fixtures.

The fixtures in ``tests/fixtures/satellite`` are 50-200 pixel crops of real Himawari-9
tiles of 2026-11-02 with their real attributes (``build_fixtures.py`` there derives them
from the 5 MB originals, which are not committed). Synthetic files are built from those
same attributes when a test needs chosen raw values. No network.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import h5py
import numpy as np
import pytest

from rwi.config import Settings
from rwi.contracts import GeoGrid, SatelliteScene
from rwi.core.errors import ProviderUnavailableError
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind
from rwi.satellite import scene as scene_mod
from rwi.satellite.geo import FullGridGeometry, reference_geometry
from rwi.satellite.scene import (
    REFLECTANCE_STATEMENT,
    REFLECTANCE_SUN_NORMALISED,
    decode_tile,
    load_scene,
    load_scene_async,
    read_native_tile,
    solar_zenith_field,
    solar_zenith_range,
    subset,
)
from rwi.satellite.tiles import SatelliteDataError, read_attrs
from rwi.solar import solar_position

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "satellite"
CENTRE = FIX / "satellite_T059_c03_0300_centre.nc"
SOUTH = FIX / "satellite_T059_c03_0300_south_edge.nc"
NORTH = FIX / "satellite_T069_c03_0300_north_edge.nc"
LIMB = FIX / "satellite_T044_c03_0300_limb.nc"
DESERT = {
    3: (FIX / "satellite_T069_c03_0300_desert.nc", datetime(2026, 11, 2, 3, 0, tzinfo=UTC)),
    6: (FIX / "satellite_T069_c03_0600_desert.nc", datetime(2026, 11, 2, 6, 0, tzinfo=UTC)),
    8: (FIX / "satellite_T069_c03_0800_desert.nc", datetime(2026, 11, 2, 8, 0, tzinfo=UTC)),
}

SLOT = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
OFFLINE = Settings(allow_network=False)
KEY_DIR = "AHI-L2-FLDK-ISatSS/2026/11/02/0300"
KEY_T059 = f"{KEY_DIR}/OR_HFD-005-B11-M1C03-T059_GH9_s20263060300000_c20263060309090.nc"
KEY_T069 = f"{KEY_DIR}/OR_HFD-005-B11-M1C03-T069_GH9_s20263060300000_c20263060309090.nc"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _h5_attrs(node: h5py.HLObject) -> dict[str, Any]:
    skip = {"DIMENSION_LIST", "REFERENCE_LIST", "_Netcdf4Dimid", "_Netcdf4Coordinates"}
    return {k: node.attrs[k] for k in node.attrs if k not in skip}


def write_tile(
    path: Path,
    raw: np.ndarray,
    *,
    row0: int,
    col0: int,
    tile_row_offset: int = 13200,
    tile_col_offset: int = 8800,
    cmi_attrs: dict[str, Any] | None = None,
    drop_cmi_attrs: tuple[str, ...] = (),
) -> Path:
    """A tile file with chosen raw int16 values, geolocated by the real T059 attributes.

    ``row0``/``col0`` are the full-disk indices of the first pixel (what ``y``/``x`` hold).
    """
    with h5py.File(CENTRE, "r") as src, h5py.File(path, "w") as dst:
        for k, v in _h5_attrs(src).items():
            dst.attrs[k] = v
        dst.attrs["tile_row_offset"] = tile_row_offset
        dst.attrs["tile_column_offset"] = tile_col_offset
        h, w = raw.shape
        for name, n, first in (("x", w, col0), ("y", h, row0)):
            d = dst.create_dataset(name, data=np.arange(first, first + n, dtype=np.int16))
            for k, v in _h5_attrs(src[name]).items():
                d.attrs[k] = v
        cmi = dst.create_dataset("Sectorized_CMI", data=raw.astype(np.int16))
        attrs = _h5_attrs(src["Sectorized_CMI"])
        for k in drop_cmi_attrs:
            attrs.pop(k, None)
        attrs.update(cmi_attrs or {})
        for k, v in attrs.items():
            cmi.attrs[k] = v
        p = dst.create_dataset("fixedgrid_projection", data=src["fixedgrid_projection"][()])
        for k, v in _h5_attrs(src["fixedgrid_projection"]).items():
            p.attrs[k] = v
    return path


def place_in_cache(cache_dir: Path, key: str, src: Path, retrieved: datetime) -> Path:
    """Put ``src`` into the download cache exactly as ``fetch_tile`` would have."""
    dest = cache_dir / "satellite" / key
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src.read_bytes())
    record = {
        "schema": 1,
        "key": key,
        "retrieved_at_utc": retrieved.isoformat(),
        "size_bytes": dest.stat().st_size,
        "source_url": f"https://noaa-himawari9.s3.amazonaws.com/{key}",
    }
    dest.with_name(dest.name + ".meta.json").write_text(json.dumps(record), encoding="utf-8")
    return dest


@pytest.fixture(scope="module")
def geometry() -> FullGridGeometry:
    return FullGridGeometry.from_file(CENTRE)


# ---------------------------------------------------------------------------
# decode: scale/offset and fill
# ---------------------------------------------------------------------------


def test_scale_and_offset_are_applied_to_known_raw_values(tmp_path: Path) -> None:
    """reflectance = raw * 0.00048828125 (= 1/2048) + 0: 2048 -> 1.0, 1024 -> 0.5, 0 -> 0."""
    raw = np.array([[0, 1, 1024, 2048], [4096, 100, 1766, 512]], dtype=np.int16)
    path = write_tile(tmp_path / "t.nc", raw, row0=14300, col0=9900)
    tile = read_native_tile(path)
    assert tile.values.dtype == np.float32
    assert tile.quantity == "reflectance" and tile.unit == "1"
    expected = raw.astype(np.float64) / 2048.0
    assert np.allclose(tile.values, expected, rtol=0, atol=1e-7)
    assert float(tile.values[0, 3]) == 1.0 and float(tile.values[0, 2]) == 0.5
    assert tile.metadata["scale_factor"] == 0.00048828125
    assert tile.metadata["add_offset"] == 0.0


def test_add_offset_is_applied(tmp_path: Path) -> None:
    """The IR bands use scale 0.064208984375 and offset 69 K (seen in a real C13 tile)."""
    raw = np.array([[0, 1000, 3000, 4095]], dtype=np.int16)
    path = write_tile(
        tmp_path / "ir.nc", raw, row0=14300, col0=9900,
        cmi_attrs={"standard_name": "toa_brightness_temperature", "units": "kelvin",
                   "scale_factor": 0.064208984375, "add_offset": 69.0},
    )  # fmt: skip
    tile = read_native_tile(path)
    assert tile.quantity == "brightness_temperature" and tile.unit == "K"
    assert np.allclose(tile.values[0], [69.0, 133.208984375, 261.626953125, 331.9], atol=0.05)
    assert float(tile.values[0, 0]) == pytest.approx(69.0)
    assert tile.metadata["sun_normalised"] is None  # meaningless for infrared
    assert tile.metadata["reflectance_statement"] is None


def test_fill_value_becomes_nan_never_a_reflectance(tmp_path: Path) -> None:
    raw = np.array([[-32767, 500, -999, 800]], dtype=np.int16)
    # 1. nothing declared: the netCDF default int16 fill (-32767) is still masked
    plain = read_native_tile(write_tile(tmp_path / "a.nc", raw, row0=14300, col0=9900))
    assert np.isnan(plain.values[0, 0]) and np.isfinite(plain.values[0, 1:]).all()
    # 2. _FillValue declared by the file is honoured
    declared = read_native_tile(
        write_tile(tmp_path / "b.nc", raw, row0=14300, col0=9900, cmi_attrs={"_FillValue": -999})
    )
    assert np.isnan(declared.values[0, 0]) and np.isnan(declared.values[0, 2])
    assert float(declared.values[0, 1]) == pytest.approx(500 / 2048)
    assert declared.metadata["n_fill_masked"] == 2
    assert -999 in declared.metadata["fill_values_masked"]
    # 3. a declared valid range is honoured too
    ranged = read_native_tile(
        write_tile(tmp_path / "c.nc", raw, row0=14300, col0=9900, cmi_attrs={"valid_max": 600})
    )
    assert np.isnan(ranged.values[0, 3]) and np.isfinite(ranged.values[0, 1])


@pytest.mark.science
def test_space_pixels_are_masked_by_geometry_because_the_file_does_not_flag_them() -> None:
    """The product declares no _FillValue and its space pixels hold small noise integers
    (here -5..~60, i.e. 'reflectance' 0.0-0.03) that read exactly like dark ocean. Only the
    geometry can tell them apart; this fixture is a real window straddling the limb."""
    with h5py.File(LIMB, "r") as f:
        raw = f["Sectorized_CMI"][()]
        attrs = read_attrs(f["Sectorized_CMI"])
    assert "_FillValue" not in attrs and "missing_value" not in attrs
    tile = read_native_tile(LIMB)
    n_off = int(tile.metadata["n_off_disk_masked"])
    assert 0 < n_off < raw.size
    assert tile.metadata["n_fill_masked"] == 0  # nothing in the file said 'fill'
    nan = np.isnan(tile.values)
    assert int(nan.sum()) == n_off
    assert raw[nan].max() < 120 and raw[nan].min() > -50  # looks like valid dark pixels
    assert np.isfinite(tile.values[~nan]).all()
    # the masked region is the contiguous space side (east), not scattered
    first_nan_col = np.argmax(nan.any(axis=0))
    assert nan[:, first_nan_col + 2 :].all(axis=0).sum() >= 1


def test_negative_on_disk_noise_is_kept_not_clipped(tmp_path: Path) -> None:
    path = write_tile(tmp_path / "n.nc", np.array([[-3, 2]], dtype=np.int16), row0=14300, col0=9900)
    assert float(read_native_tile(path).values[0, 0]) < 0.0


def test_unsupported_quantity_and_corrupt_files_are_refused(tmp_path: Path) -> None:
    raw = np.ones((2, 2), dtype=np.int16)
    odd = write_tile(tmp_path / "odd.nc", raw, row0=14300, col0=9900,
                     cmi_attrs={"standard_name": "cloud_top_height"})  # fmt: skip
    with pytest.raises(SatelliteDataError, match="unsupported"):
        read_native_tile(odd)
    junk = tmp_path / "junk.nc"
    junk.write_bytes(b"not a netcdf file")
    with pytest.raises(SatelliteDataError, match="cannot open"):
        read_native_tile(junk)
    # x/y that are not consecutive indices
    broken = write_tile(tmp_path / "broken.nc", raw, row0=14300, col0=9900)
    with h5py.File(broken, "r+") as f:
        f["x"][1] = 5
    with pytest.raises(SatelliteDataError, match="consecutive"):
        read_native_tile(broken)


def test_a_tile_that_contradicts_its_own_name_is_refused(tmp_path: Path) -> None:
    """File named T069 but carrying T059's offsets: the numbering assumption is broken."""
    raw = np.ones((4, 4), dtype=np.int16)
    named = tmp_path / "OR_HFD-005-B11-M1C03-T069_GH9_s20263060300000_c20263060309090.nc"
    write_tile(named, raw, row0=14300, col0=9900, tile_row_offset=13200, tile_col_offset=8800)
    with pytest.raises(SatelliteDataError, match="numbering"):
        read_native_tile(named)


# ---------------------------------------------------------------------------
# decode_tile on real pixels
# ---------------------------------------------------------------------------


CENTRE_BOX = (-15.30, 135.45, -15.15, 135.60)  # inside the 200 x 200 px crop around pixel 1100


def test_decode_tile_returns_a_regular_grid_with_real_values() -> None:
    values, grid, meta = decode_tile(CENTRE, bounds=CENTRE_BOX, resolution_deg=0.005)
    assert values.dtype == np.float32
    assert values.shape == tuple(grid.shape)
    assert 0.0 < float(np.nanmin(values)) and float(np.nanmax(values)) < 1.6  # reflectance factor
    assert np.isfinite(values).all()
    lats, lons = np.asarray(grid.latitudes), np.asarray(grid.longitudes)
    assert (np.diff(lats) < 0).all() and (np.diff(lons) > 0).all()  # north -> south, west -> east
    assert grid.resolution_km == pytest.approx(0.005 * 111.32)
    south, west, north, east = grid.bounds
    assert south < lats.min() and north > lats.max() and west < lons.min() and east > lons.max()
    assert south == pytest.approx(-15.30) and north == pytest.approx(-15.15)
    assert meta["quantity"] == "reflectance" and meta["unit"] == "1"
    assert meta["standard_name"] == "toa_bidirectional_reflectance"
    assert meta["sun_normalised"] is False
    assert meta["tile_center_latitude_in_file"] == pytest.approx(-15.209781154137673)
    assert meta["scale_factor"] == pytest.approx(1 / 2048)


def test_default_grid_step_follows_the_native_footprint() -> None:
    """At 15 S, 135 E the viewing angle is ~25 deg, so a 0.5 km pixel covers ~0.55 km."""
    _, grid, meta = decode_tile(CENTRE, bounds=CENTRE_BOX)
    assert 0.5 < grid.resolution_km < 0.65
    assert meta["native_resolution_km"] == 0.5
    assert meta["grid_step_deg"] == pytest.approx(grid.resolution_km / 111.32)


def test_resampling_reproduces_native_pixels_and_averages_between_them() -> None:
    tile = read_native_tile(CENTRE)
    geom = tile.geometry
    row, col = 14300, 9900  # a pixel inside the crop (global indices)
    lat, lon = (float(v) for v in geom.pixel_to_latlon(row, col))
    one = scene_mod._resample([tile], geom, np.array([lat]), np.array([lon]))
    native = tile.values[row - tile.row0, col - tile.col0]
    assert float(one[0, 0]) == pytest.approx(float(native), abs=1e-6)
    # half way between two columns: the mean of the two (bilinear), up to the row drift
    lat2, lon2 = (float(v) for v in geom.pixel_to_latlon(row, col + 0.5))
    mid = scene_mod._resample([tile], geom, np.array([lat2]), np.array([lon2]))
    pair = tile.values[row - tile.row0, col - tile.col0 : col - tile.col0 + 2]
    assert float(mid[0, 0]) == pytest.approx(float(pair.mean()), abs=1e-6)


def test_a_missing_neighbour_makes_nan_not_a_guess() -> None:
    tile = read_native_tile(CENTRE)
    geom = tile.geometry
    # a point just outside the crop window: neighbours absent -> NaN, not nearest-value fill
    lat, lon = (float(v) for v in geom.pixel_to_latlon(tile.row0 - 0.5, tile.col0 + 50))
    out = scene_mod._resample([tile], geom, np.array([lat]), np.array([lon]))
    assert np.isnan(out[0, 0])
    # a NaN neighbour poisons the sample
    poisoned = read_native_tile(CENTRE)
    poisoned.values[50, 50] = np.nan
    lat, lon = (float(v) for v in geom.pixel_to_latlon(tile.row0 + 50.4, tile.col0 + 50.4))
    assert np.isnan(scene_mod._resample([poisoned], geom, np.array([lat]), np.array([lon]))[0, 0])


def test_a_mosaic_across_two_tiles_has_no_seam() -> None:
    """Row 15399 is the last of T059, row 15400 the first of T069; a sample between them
    must be their mean."""
    south, north = read_native_tile(SOUTH), read_native_tile(NORTH)
    assert south.row0 + south.values.shape[0] == north.row0 == 15400
    geom = south.geometry
    col = 9900
    lat, lon = (float(v) for v in geom.pixel_to_latlon(15399.5, col))
    out = scene_mod._resample([south, north], geom, np.array([lat]), np.array([lon]))
    a = south.values[-1, col - south.col0]
    b = north.values[0, col - north.col0]
    assert float(out[0, 0]) == pytest.approx(float((a + b) / 2), abs=1e-6)
    # with only one tile supplied the same point has no complete neighbourhood
    alone = scene_mod._resample([south], geom, np.array([lat]), np.array([lon]))
    assert np.isnan(alone[0, 0])


def test_decode_tile_with_bounds_reads_only_the_window() -> None:
    values, grid, _ = decode_tile(
        CENTRE, bounds=(-15.30, 135.45, -15.15, 135.60), resolution_deg=0.005
    )
    assert values.shape == (30, 30)
    assert grid.bounds[0] == pytest.approx(-15.30) and grid.bounds[3] == pytest.approx(135.60)
    assert np.isfinite(values).all()


def test_bounds_outside_the_disk_or_the_tile_do_not_return_numbers() -> None:
    with pytest.raises(SatelliteDataError, match="not visible"):
        decode_tile(CENTRE, bounds=(50.0, 0.0, 51.0, 1.0))  # Europe: not visible
    values, _, _ = decode_tile(CENTRE, bounds=(-34.0, 138.0, -33.9, 138.1), resolution_deg=0.01)
    assert np.isnan(values).all()  # visible, but not inside this crop: NaN, not zeros


def test_huge_requests_are_refused() -> None:
    with pytest.raises(ValueError, match="pixels"):
        decode_tile(CENTRE, bounds=(-40.0, 120.0, -10.0, 150.0), resolution_deg=0.0004)


# ---------------------------------------------------------------------------
# subset
# ---------------------------------------------------------------------------


def _grid(lats: np.ndarray, lons: np.ndarray, step: float) -> GeoGrid:
    return GeoGrid(
        latitudes=lats.tolist(), longitudes=lons.tolist(), shape=(lats.size, lons.size),
        resolution_km=step * 111.32,
        bounds=(lats.min() - step / 2, lons.min() - step / 2, lats.max() + step / 2,
                lons.max() + step / 2),
    )  # fmt: skip


def test_subset_cuts_the_right_pixels() -> None:
    lats = -10.0 - 0.1 * (np.arange(50) + 0.5)  # -10.05 ... -14.95, north first
    lons = 130.0 + 0.1 * (np.arange(80) + 0.5)
    grid = _grid(lats, lons, 0.1)
    values = (np.arange(50)[:, None] * 100 + np.arange(80)[None, :]).astype(np.float32)
    sub, sg = subset(values, grid, (-12.0, 132.0, -11.0, 133.0))
    assert sub.shape == sg.shape
    assert sg.latitudes[0] == pytest.approx(-11.05) and sg.latitudes[-1] == pytest.approx(-11.95)
    assert sg.longitudes[0] == pytest.approx(132.05) and sg.longitudes[-1] == pytest.approx(132.95)
    assert sub[0, 0] == values[10, 20] and sub[-1, -1] == values[19, 29]
    sub[0, 0] = -1.0
    assert values[10, 20] != -1.0  # a copy, not a view
    assert sg.bounds[0] == pytest.approx(-12.0) and sg.bounds[3] == pytest.approx(133.0)
    with pytest.raises(ValueError, match="do not overlap"):
        subset(values, grid, (-30.0, 132.0, -29.0, 133.0))
    with pytest.raises(ValueError, match="does not match"):
        subset(values[:10], grid, (-12.0, 132.0, -11.0, 133.0))


def test_subset_accepts_either_longitude_convention_across_the_antimeridian() -> None:
    lats = np.array([5.5, 4.5])
    lons = np.arange(178.5, 183.0, 1.0)  # unwrapped: 178.5 ... 182.5 (crosses 180)
    grid = _grid(lats, lons, 1.0)
    values = np.arange(10, dtype=np.float32).reshape(2, 5)
    a, ga = subset(values, grid, (4.0, 179.0, 6.0, 181.0))
    b, gb = subset(values, grid, (4.0, 179.0, 6.0, -179.0))  # same box, written the other way
    assert a.shape == b.shape == (2, 2) and ga.longitudes == gb.longitudes


# ---------------------------------------------------------------------------
# load_scene
# ---------------------------------------------------------------------------


def _corridor_bounds() -> tuple[float, float, float, float]:
    """A box straddling the T059/T069 boundary (global row 15400) inside both crops."""
    g = reference_geometry("C03")
    lat, lon = (float(v) for v in g.pixel_to_latlon(15400, 9900))
    return (lat - 0.08, lon - 0.08, lat + 0.08, lon + 0.08)


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    retrieved = datetime(2026, 11, 2, 3, 15, tzinfo=UTC)
    place_in_cache(tmp_path, KEY_T059, SOUTH, retrieved)
    place_in_cache(tmp_path, KEY_T069, NORTH, retrieved + timedelta(seconds=5))
    return tmp_path


def test_load_scene_assembles_two_tiles_from_the_cache_offline(cache: Path) -> None:
    now = datetime(2026, 11, 2, 3, 20, tzinfo=UTC)
    values, scene = load_scene(_corridor_bounds(), SLOT, cache_dir=cache, settings=OFFLINE, now=now)
    assert isinstance(scene, SatelliteScene)
    assert values.dtype == np.float32 and values.shape == tuple(scene.grid.shape)
    assert np.isfinite(values).mean() > 0.95  # no hole where the tiles meet
    assert scene.satellite == "Himawari-9" and scene.band == "C03"
    assert scene.slot_time_utc == SLOT
    assert scene.quantity == "reflectance" and scene.unit == "1"
    lo, hi = scene.solar_zenith_range_deg or (math.nan, math.nan)
    assert 0 < lo <= hi < 35  # 12:30 local, sun nearly overhead
    assert scene.is_usable_visible


def test_scene_provenance_is_complete_and_honest(cache: Path) -> None:
    now = datetime(2026, 11, 2, 3, 20, tzinfo=UTC)
    _, scene = load_scene(_corridor_bounds(), SLOT, cache_dir=cache, settings=OFFLINE, now=now)
    prov = scene.provenance
    assert prov.kind is not DataKind.OBSERVATION  # imagery is resampled, not a ground measurement
    assert prov.valid_time_utc == SLOT
    assert prov.retrieved_at_utc == datetime(2026, 11, 2, 3, 15, tzinfo=UTC)  # ORIGINAL retrieval
    assert prov.spatial_resolution_km == pytest.approx(scene.grid.resolution_km)
    assert prov.temporal_resolution_s == 600.0
    assert prov.license is not None and "attribution" in prov.license
    assert prov.source_url == "https://registry.opendata.aws/noaa-himawari"
    notes = prov.notes or ""
    assert KEY_T059 in notes and KEY_T069 in notes  # the object keys used
    assert "tiles=T059,T069" in notes
    assert "product_created_latency_s=549.0" in notes  # 9 min 9 s from slot to product
    assert "image_age_at_load_s=1200" in notes  # 20 min old at 03:20, stated, not hidden
    assert "NOT a ground measurement" in notes
    assert "JMA" in notes and "NOAA" in notes and "endorsement" in notes  # attribution
    assert prov.provider.startswith("NOAA STAR / JMA")


@pytest.mark.science
def test_the_scene_declares_that_reflectance_is_not_sun_normalised(cache: Path) -> None:
    """Pins the convention. Visible CMI is a reflectance FACTOR (toa_bidirectional_reflectance),
    not divided by cos(solar zenith) - verified on data in
    ``test_visible_reflectance_follows_the_sun_so_it_is_not_normalised``. If a decoder change
    ever flipped this, every retrieved irradiance would carry a sun-angle-dependent bias that
    no ground truth exists here to reveal; hence the statement is asserted in four places."""
    assert REFLECTANCE_SUN_NORMALISED is False
    assert "NOT sun-normalised" in REFLECTANCE_STATEMENT
    assert "cos(solar zenith)" in REFLECTANCE_STATEMENT

    _, scene = load_scene(_corridor_bounds(), SLOT, cache_dir=cache, settings=OFFLINE)
    assert scene.sun_normalised is False
    assert scene.sun_normalised == REFLECTANCE_SUN_NORMALISED  # field and constant agree
    assert scene.provenance.processing is not None
    assert scene.provenance.processing.startswith(REFLECTANCE_STATEMENT)
    assert REFLECTANCE_STATEMENT in (scene.provenance.notes or "")

    _, _, meta = decode_tile(CENTRE, bounds=CENTRE_BOX, resolution_deg=0.01)
    assert meta["sun_normalised"] is False
    assert meta["reflectance_statement"] == REFLECTANCE_STATEMENT


@pytest.mark.science
def test_visible_reflectance_follows_the_sun_so_it_is_not_normalised() -> None:
    """The evidence behind the declaration, from real pixels.

    The same 100 x 100 px window of the Australian interior (tile T069, ~26 S 135 E, clear
    land) at three sun angles on 2026-11-02. A sun-normalised albedo would stay roughly
    constant; a reflectance factor falls like cos(zenith). Measured medians: 0.175 (zenith
    12 deg), 0.098 (47 deg), 0.033 (74 deg). Ratios 74/12 deg: observed 0.19, cos ratio 0.28,
    normalised would be ~1.
    """
    medians: dict[int, float] = {}
    cosines: dict[int, float] = {}
    for hour, (path, when) in DESERT.items():
        tile = read_native_tile(path)
        medians[hour] = float(np.nanmedian(tile.values))
        r = tile.row0 + tile.values.shape[0] // 2
        c = tile.col0 + tile.values.shape[1] // 2
        lat, lon = (float(v) for v in tile.geometry.pixel_to_latlon(r, c))
        zen = solar_zenith_field(np.array([lat]), np.array([lon]), when)[0, 0]
        cosines[hour] = math.cos(math.radians(float(zen)))
    assert 0.15 < medians[3] < 0.20
    assert medians[3] > medians[6] > medians[8]
    observed = medians[8] / medians[3]
    expected_if_factor = cosines[8] / cosines[3]  # ~0.28
    assert 0.5 * expected_if_factor < observed < 1.2 * expected_if_factor
    assert observed < 0.4  # a normalised quantity would give ~1
    # dividing by cos brings the three back towards each other (still not identical: path
    # length, surface BRDF)
    spread_raw = max(medians.values()) / min(medians.values())
    normalised = [medians[h] / cosines[h] for h in medians]
    assert max(normalised) / min(normalised) < spread_raw / 3


def test_night_scene_is_not_usable_visible(cache: Path) -> None:
    """15:00Z is 00:30 in Darwin: the visible image is black, and must say it is unusable."""
    _, scene = load_scene(_corridor_bounds(), SLOT, cache_dir=cache, settings=OFFLINE)
    night = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
    lo, hi = solar_zenith_range(scene.grid, night)
    assert lo > 80.0 and hi > lo
    dark = scene.model_copy(update={"slot_time_utc": night, "solar_zenith_range_deg": (lo, hi)})
    assert dark.is_usable_visible is False
    assert scene.is_usable_visible is True


@pytest.mark.parametrize(
    ("zenith", "usable"),
    [((20.0, 40.0), True), ((79.9, 95.0), True), ((80.0, 95.0), False), ((85.0, 120.0), False),
     (None, False)],
)  # fmt: skip
def test_is_usable_visible_threshold(
    cache: Path, zenith: tuple[float, float] | None, usable: bool
) -> None:
    _, scene = load_scene(_corridor_bounds(), SLOT, cache_dir=cache, settings=OFFLINE)
    assert scene.model_copy(update={"solar_zenith_range_deg": zenith}).is_usable_visible is usable


def test_a_far_bound_still_does_not_crash_the_night_logic() -> None:
    lat = np.array([-12.0, -13.0])
    lon = np.array([130.0, 131.0])
    zen = solar_zenith_field(lat, lon, datetime(2026, 11, 2, 15, 0, tzinfo=UTC))
    assert zen.shape == (2, 2) and (zen > 90).all()  # well below the horizon


@pytest.mark.science
def test_solar_zenith_field_agrees_with_rwi_solar() -> None:
    """Reference: rwi.solar (NREL SPA via pvlib), geometric zenith (no refraction).
    Tolerance 0.05 deg over a 10 x 10 degree scene at three times of day."""
    lats = np.linspace(-14.0, -24.0, 5)
    lons = np.linspace(130.0, 140.0, 5)
    for when in (datetime(2026, 11, 2, 3, 0, tzinfo=UTC), datetime(2026, 11, 2, 7, 30, tzinfo=UTC),
                 datetime(2026, 11, 2, 22, 0, tzinfo=UTC)):  # fmt: skip
        field = solar_zenith_field(lats, lons, when)
        for i, la in enumerate(lats):
            for j, lo in enumerate(lons):
                spa = solar_position(
                    GeoPoint(latitude_deg=float(la), longitude_deg=float(lo)), when
                )
                assert field[i, j] == pytest.approx(spa.zenith_deg, abs=0.05)


def test_load_scene_refuses_what_it_cannot_do(cache: Path, tmp_path: Path) -> None:
    # a box nobody can see
    with pytest.raises(SatelliteDataError, match="outside"):
        load_scene((50.0, 0.0, 51.0, 1.0), SLOT, cache_dir=cache, settings=OFFLINE)
    # offline, empty cache: raises rather than inventing
    with pytest.raises(ProviderUnavailableError, match="not in the cache"):
        load_scene(_corridor_bounds(), SLOT, cache_dir=tmp_path / "empty", settings=OFFLINE)
    # offline, only one of the two tiles cached: no mosaic with a hole
    half = tmp_path / "half"
    place_in_cache(half, KEY_T059, SOUTH, datetime(2026, 11, 2, 3, 15, tzinfo=UTC))
    with pytest.raises(ProviderUnavailableError, match="T069"):
        load_scene(_corridor_bounds(), SLOT, cache_dir=half, settings=OFFLINE)


def test_a_cached_tile_without_a_retrieval_record_is_refused(cache: Path) -> None:
    (cache / "satellite" / (KEY_T069 + ".meta.json")).unlink()
    with pytest.raises(ProviderUnavailableError):
        load_scene(_corridor_bounds(), SLOT, cache_dir=cache, settings=OFFLINE)


async def test_load_scene_async_and_the_sync_wrapper_guard(cache: Path) -> None:
    values, scene = await load_scene_async(
        _corridor_bounds(), SLOT, cache_dir=cache, settings=OFFLINE
    )
    assert values.shape == tuple(scene.grid.shape)
    with pytest.raises(RuntimeError, match="running event loop"):
        load_scene(_corridor_bounds(), SLOT, cache_dir=cache, settings=OFFLINE)


def test_the_prior_geometry_is_cross_checked_against_the_files(cache: Path) -> None:
    """If the satellite's grid ever differed from the reference prior, the tiles chosen
    from the prior would be wrong; the files' own offsets must expose that."""
    ref = reference_geometry("C03")
    shifted = ref.__class__(**{**ref.__dict__, "x_offset_urad": ref.x_offset_urad + 5000.0})
    with pytest.raises(SatelliteDataError, match=r"inconsistent|reference geometry|own grid"):
        # emulate load_scene with the wrong prior
        from rwi.satellite.source import list_cached_tiles

        tiles = list_cached_tiles(SLOT, band="C03", cache_dir=cache)
        paths = [cache / "satellite" / t.key for t in tiles]
        scene_mod._assemble(_corridor_bounds(), SLOT, "C03", tiles, paths, shifted, None, None)
