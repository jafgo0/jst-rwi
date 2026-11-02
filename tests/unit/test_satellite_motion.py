"""Tests for rwi.satellite.motion (phase-correlation cloud motion). No network.

Every field is synthetic. These tests show that the arithmetic, signs and
refusals are right; they say nothing about whether real clouds translate.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from scipy import ndimage

from rwi.contracts import CloudMotion, GeoGrid
from rwi.core.errors import NaiveDatetimeError
from rwi.satellite.motion import (
    MAX_INTERVAL_S,
    MIN_INTERVAL_S,
    MIN_USABLE_CONFIDENCE,
    InsufficientStructureError,
    estimate_motion,
    estimate_motion_blocks,
    is_usable_motion,
)

T0 = datetime(2027, 8, 24, 3, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=10)
SIDE = 256
MARGIN = 64


def make_grid(
    n: int = SIDE, res_km: float = 0.5, *, lat_descending: bool = True, lon_ascending: bool = True
) -> GeoGrid:
    """A regular lat/lon grid whose spacing really is ``res_km`` near 25 S."""
    lat0, lon0 = -25.0, 134.0
    dlat = res_km * 1000.0 / 111_195.0
    dlon = res_km * 1000.0 / (111_320.0 * math.cos(math.radians(lat0)))
    lats = [lat0 - i * dlat if lat_descending else lat0 + i * dlat for i in range(n)]
    lons = [lon0 + j * dlon if lon_ascending else lon0 - j * dlon for j in range(n)]
    return GeoGrid(
        latitudes=lats,
        longitudes=lons,
        shape=(n, n),
        resolution_km=res_km,
        bounds=(min(lats), min(lons), max(lats), max(lons)),
    )


def cloud_canvas(seed: int = 1, n: int = SIDE + 2 * MARGIN, beta: float = 3.0) -> np.ndarray:
    """A power-law, cloud-like field in [0, 1] (smooth blobs of many sizes)."""
    rng = np.random.default_rng(seed)
    spec = np.fft.fft2(rng.standard_normal((n, n)))
    k = np.fft.fftfreq(n)
    kk = np.sqrt(k[:, None] ** 2 + k[None, :] ** 2)
    kk[0, 0] = 1.0
    field = np.fft.ifft2(spec / kk ** (beta / 2)).real
    field = (field - field.mean()) / field.std()
    return np.clip(0.5 + 0.35 * field, 0.0, 1.0)


def crop(canvas: np.ndarray) -> np.ndarray:
    return canvas[MARGIN : MARGIN + SIDE, MARGIN : MARGIN + SIDE].copy()


def pair(d_row: float, d_col: float, seed: int = 1) -> tuple[np.ndarray, np.ndarray]:
    """Frames where the content moves by (d_row, d_col) array pixels."""
    canvas = cloud_canvas(seed)
    moved = ndimage.shift(canvas, (d_row, d_col), order=3, mode="wrap")
    return crop(canvas), crop(moved)


def run(
    a: np.ndarray, b: np.ndarray, grid: GeoGrid | None = None, *, later: datetime = T1
) -> CloudMotion:
    return estimate_motion(a, b, grid or make_grid(), earlier_time=T0, later_time=later)


# --- synthetic translation: sign and direction ------------------------------


@pytest.mark.science
def test_eastward_shift_is_recovered_with_correct_sign_and_compass_direction() -> None:
    a, b = pair(0, 6)  # content moves 6 columns to the right = east
    m = run(a, b)
    assert m.u_east_m_s == pytest.approx(5.0, abs=0.1)
    assert m.v_north_m_s == pytest.approx(0.0, abs=0.1)
    assert m.direction_to_deg == pytest.approx(90.0, abs=1.5)  # TOWARDS the east
    assert m.u_east_m_s > 0  # a sign error would say the cloud is leaving, not arriving
    assert is_usable_motion(m)


@pytest.mark.science
def test_northward_shift_is_recovered_with_correct_sign_and_compass_direction() -> None:
    # Row 0 is the northern edge (latitudes descend), so moving north = fewer rows.
    a, b = pair(-6, 0)
    m = run(a, b)
    assert m.v_north_m_s == pytest.approx(5.0, abs=0.1)
    assert m.u_east_m_s == pytest.approx(0.0, abs=0.1)
    assert m.direction_to_deg == pytest.approx(0.0, abs=1.5) or m.direction_to_deg == pytest.approx(
        360.0, abs=1.5
    )
    assert m.v_north_m_s > 0
    assert is_usable_motion(m)


@pytest.mark.science
def test_orientation_follows_grid_coordinates_not_array_layout() -> None:
    # The same array shift means north on a north-up grid and south on a south-up grid.
    a, b = pair(-6, 0)
    north_up = run(a, b, make_grid(lat_descending=True))
    south_up = run(a, b, make_grid(lat_descending=False))
    assert north_up.v_north_m_s == pytest.approx(5.0, abs=0.1)
    assert south_up.v_north_m_s == pytest.approx(-5.0, abs=0.1)
    assert south_up.direction_to_deg == pytest.approx(180.0, abs=1.5)
    # Likewise for columns on a grid whose longitudes decrease with column.
    a, b = pair(0, 6)
    flipped = run(a, b, make_grid(lon_ascending=False))
    assert flipped.u_east_m_s == pytest.approx(-5.0, abs=0.1)
    assert flipped.direction_to_deg == pytest.approx(270.0, abs=1.5)


@pytest.mark.science
def test_southwest_diagonal_direction() -> None:
    a, b = pair(5, -5)  # down the array (south) and left (west)
    m = run(a, b)
    assert m.u_east_m_s < 0 < -m.v_north_m_s
    assert m.direction_to_deg == pytest.approx(225.0, abs=2.0)


@pytest.mark.science
def test_subpixel_shift_recovered_within_a_tenth_of_a_pixel() -> None:
    d_row, d_col = 3.4, -2.7
    a, b = pair(d_row, d_col)
    m = run(a, b)
    # Pixels per metre/second: 600 s * v / 500 m.
    rec_col = m.u_east_m_s * 600.0 / 500.0
    rec_row = -m.v_north_m_s * 600.0 / 500.0  # north-up grid: rows grow southwards
    assert rec_col == pytest.approx(d_col, abs=0.1)
    assert rec_row == pytest.approx(d_row, abs=0.1)
    # Reported precisely so a regression in accuracy is visible: see the docs.
    assert math.hypot(rec_row - d_row, rec_col - d_col) < 0.1


@pytest.mark.science
def test_large_shift_recovered() -> None:
    a, b = pair(12, 12)
    m = run(a, b)
    assert m.u_east_m_s * 1.2 == pytest.approx(12.0, abs=0.15)
    assert m.v_north_m_s * 1.2 == pytest.approx(-12.0, abs=0.15)


# --- unit conversion --------------------------------------------------------


def test_pixels_to_metres_per_second_is_exact_for_known_resolution_and_interval() -> None:
    # 8 px * 0.5 km = 4000 m over 800 s = exactly 5 m/s.
    a, b = pair(0, 8)
    m = run(a, b, later=T0 + timedelta(seconds=800))
    assert m.u_east_m_s == pytest.approx(5.0, abs=0.05)
    assert m.speed_m_s == pytest.approx(5.0, abs=0.05)
    assert m.interval_s == 800.0
    # 3 px at 2 km = 6000 m over 900 s = 6.6667 m/s (a coarser grid, longer interval).
    m2 = estimate_motion(
        *pair(0, 3), make_grid(res_km=2.0), earlier_time=T0, later_time=T0 + timedelta(seconds=900)
    )
    assert m2.speed_m_s == pytest.approx(6000.0 / 900.0, abs=0.1)
    # Same pixel shift, double the interval, half the speed.
    m3 = run(a, b, later=T0 + timedelta(seconds=1600))
    assert m3.speed_m_s == pytest.approx(2.5, abs=0.03)


def test_returned_vector_is_internally_consistent() -> None:
    a, b = pair(-4, 3)
    m = run(a, b)
    assert m.speed_m_s == pytest.approx(math.hypot(m.u_east_m_s, m.v_north_m_s))
    bearing = math.degrees(math.atan2(m.u_east_m_s, m.v_north_m_s)) % 360.0
    assert m.direction_to_deg == pytest.approx(bearing)
    assert m.from_time_utc == T0
    assert m.to_time_utc == T1
    assert m.provenance.kind.value == "derived"


# --- zero motion, noise, structure ------------------------------------------


def test_identical_frames_give_zero_speed_with_high_confidence() -> None:
    frame = crop(cloud_canvas())
    m = run(frame, frame.copy())
    assert m.speed_m_s == pytest.approx(0.0, abs=1e-9)
    assert m.u_east_m_s == pytest.approx(0.0, abs=1e-9)
    assert m.confidence > 0.95
    assert is_usable_motion(m)
    assert "direction_undefined_at_zero_speed" in m.method


def test_pure_noise_is_low_confidence_and_unusable() -> None:
    for seed in range(5):
        rng = np.random.default_rng(seed)
        m = run(rng.random((SIDE, SIDE)), rng.random((SIDE, SIDE)))
        assert m.confidence < MIN_USABLE_CONFIDENCE
        assert not is_usable_motion(m)


def test_unrelated_cloud_fields_are_unusable() -> None:
    m = run(crop(cloud_canvas(1)), crop(cloud_canvas(2)))
    assert not is_usable_motion(m)


def test_heavy_noise_on_a_translating_field_lowers_confidence() -> None:
    a, b = pair(0, 6)
    rng = np.random.default_rng(3)
    clean = run(a, b)
    noisy = run(a + 0.8 * rng.standard_normal(a.shape), b + 0.8 * rng.standard_normal(b.shape))
    assert noisy.confidence < clean.confidence
    assert not is_usable_motion(noisy)


def test_field_that_changed_shape_is_flagged() -> None:
    # 90 percent of the second frame is a different field: not a translation.
    canvas = cloud_canvas(1)
    moved = ndimage.shift(canvas, (0, 6), order=3, mode="wrap")
    other = cloud_canvas(9)
    b = crop(0.1 * moved + 0.9 * other)
    m = run(crop(canvas), b)
    assert not is_usable_motion(m)


def test_uniform_field_raises_instead_of_returning_zero_motion() -> None:
    flat = np.full((SIDE, SIDE), 0.2)
    with pytest.raises(InsufficientStructureError):
        run(flat, flat.copy())
    with pytest.raises(InsufficientStructureError):
        run(crop(cloud_canvas()), flat)


# --- NaN handling -----------------------------------------------------------


def test_nan_gaps_do_not_poison_the_estimate() -> None:
    a, b = pair(-6, 6)
    clean = run(a, b)
    a_gap, b_gap = a.copy(), b.copy()
    a_gap[40:90, 100:150] = np.nan
    b_gap[150:200, 20:80] = np.nan
    rng = np.random.default_rng(0)
    a_gap[rng.random(a.shape) < 0.02] = np.nan
    gapped = run(a_gap, b_gap)
    assert np.isfinite(gapped.speed_m_s)
    assert gapped.u_east_m_s == pytest.approx(clean.u_east_m_s, abs=0.15)
    assert gapped.v_north_m_s == pytest.approx(clean.v_north_m_s, abs=0.15)
    assert is_usable_motion(gapped)


def test_all_nan_field_raises() -> None:
    a, _ = pair(0, 3)
    nan = np.full_like(a, np.nan)
    with pytest.raises(ValueError, match="no pixel is finite"):
        run(nan, a)
    with pytest.raises(ValueError, match="no pixel is finite"):
        run(nan, nan.copy())


def test_mostly_nan_field_raises() -> None:
    a, b = pair(0, 3)
    a[:, : int(SIDE * 0.9)] = np.nan
    with pytest.raises(ValueError, match="valid"):
        run(a, b)


def test_disjoint_gaps_use_only_pixels_valid_in_both_frames() -> None:
    a, b = pair(0, 5)
    a[:, : SIDE // 2] = np.nan
    b[:, SIDE // 2 :] = np.nan  # nothing is valid in both
    with pytest.raises(ValueError, match="no pixel is finite"):
        run(a, b)


# --- refusals ---------------------------------------------------------------


def test_mismatched_inputs_raise() -> None:
    a, b = pair(0, 3)
    with pytest.raises(ValueError, match="shapes differ"):
        run(a, b[:-1, :])
    with pytest.raises(ValueError, match="grid shape"):
        run(a, b, make_grid(n=SIDE - 8))
    with pytest.raises(ValueError, match="2-D"):
        run(a[0], b[0])
    with pytest.raises(ValueError, match="at least"):
        estimate_motion(
            a[:8, :8], b[:8, :8], make_grid(n=8), earlier_time=T0, later_time=T1
        )


def test_resolution_that_contradicts_the_coordinates_raises() -> None:
    a, b = pair(0, 3)
    good = make_grid()
    liar = good.model_copy(update={"resolution_km": 5.0})
    with pytest.raises(ValueError, match="disagrees"):
        run(a, b, liar)


def test_bad_intervals_raise() -> None:
    a, b = pair(0, 3)
    with pytest.raises(ValueError, match="after"):
        run(a, b, later=T0)
    with pytest.raises(ValueError, match="after"):
        run(a, b, later=T0 - timedelta(minutes=10))
    with pytest.raises(ValueError, match="shorter"):
        run(a, b, later=T0 + timedelta(seconds=MIN_INTERVAL_S - 1))
    with pytest.raises(ValueError, match="longer"):
        run(a, b, later=T0 + timedelta(seconds=MAX_INTERVAL_S + 1))


def test_naive_datetimes_are_rejected() -> None:
    a, b = pair(0, 3)
    with pytest.raises(NaiveDatetimeError):
        estimate_motion(
            a, b, make_grid(), earlier_time=datetime(2027, 8, 24, 3, 0), later_time=T1
        )


def test_implausibly_fast_motion_is_returned_unusable() -> None:
    # 120 px * 0.5 km / 120 s = 500 m/s: an artefact, not weather.
    a, b = pair(0, 100)
    m = run(a, b, later=T0 + timedelta(seconds=MIN_INTERVAL_S))
    assert m.speed_m_s > 80.0
    assert m.confidence == 0.0
    assert not is_usable_motion(m)
    assert "REJECTED" in m.method


def test_confidence_threshold_is_documented_and_in_range() -> None:
    assert 0.0 < MIN_USABLE_CONFIDENCE < 1.0


def test_inputs_are_not_modified() -> None:
    a, b = pair(0, 4)
    a[10:20, 10:20] = np.nan
    a_copy, b_copy = a.copy(), b.copy()
    run(a, b)
    np.testing.assert_array_equal(a, a_copy)
    np.testing.assert_array_equal(b, b_copy)


# --- EXTRA: block-wise variant ----------------------------------------------


def test_block_variant_recovers_a_uniform_translation_in_each_block() -> None:
    a, b = pair(0, 4)
    blocks = estimate_motion_blocks(a, b, make_grid(), earlier_time=T0, later_time=T1, block_px=128)
    assert len(blocks) == 4
    usable = [blk for blk in blocks if blk.is_usable]
    assert len(usable) >= 3
    for blk in usable:
        assert blk.motion.u_east_m_s == pytest.approx(4 * 500.0 / 600.0, abs=0.4)
        assert blk.latitude_deg < -25.0 < blk.latitude_deg + 1.0
