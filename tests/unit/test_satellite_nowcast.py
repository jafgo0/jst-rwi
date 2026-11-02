"""Tests for rwi.satellite.nowcast (advection nowcast and persistence). No network.

Every field is synthetic. These tests check mechanics - signs, alignment,
refusals, the effective-lead arithmetic. They say nothing about whether the
nowcast has skill on real clouds: nothing in this platform can show that yet.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from scipy import ndimage

from rwi.contracts import CloudMotion, GeoGrid, IrradianceNowcast, SatelliteScene
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance
from rwi.satellite.motion import estimate_motion
from rwi.satellite.nowcast import (
    DEFAULT_IMAGE_LATENCY_S,
    MAX_EFFECTIVE_LEAD_S,
    NowcastRefusedError,
    SatelliteNowcast,
    advect,
    default_clear_sky_index,
    nowcast_point,
    nowcast_with_baseline,
    parallax_displacement_km,
    persistence_nowcast,
)

T_IMG = datetime(2027, 8, 24, 3, 0, tzinfo=UTC)
T_PREV = T_IMG - timedelta(minutes=10)
SIDE = 256
MARGIN = 64
RES_KM = 0.5
LAT0, LON0 = -25.0, 134.0
PITCH_M = RES_KM * 1000.0


def make_grid(*, lat_descending: bool = True, n: int = SIDE) -> GeoGrid:
    dlat = RES_KM * 1000.0 / 111_195.0
    dlon = RES_KM * 1000.0 / (111_320.0 * math.cos(math.radians(LAT0)))
    lats = [LAT0 - i * dlat if lat_descending else LAT0 + i * dlat for i in range(n)]
    lons = [LON0 + j * dlon for j in range(n)]
    return GeoGrid(
        latitudes=lats,
        longitudes=lons,
        shape=(n, n),
        resolution_km=RES_KM,
        bounds=(min(lats), min(lons), max(lats), max(lons)),
    )


def prov() -> Provenance:
    return Provenance(
        provider="test",
        dataset="synthetic",
        kind=DataKind.DERIVED,
        retrieved_at_utc=T_IMG,
    )


def make_scene(
    when: datetime,
    grid: GeoGrid,
    *,
    zenith: tuple[float, float] | None = (30.0, 40.0),
    quantity: str = "cloud_index",
) -> SatelliteScene:
    return SatelliteScene(
        satellite="himawari-9",
        band="C03",
        slot_time_utc=when,
        grid=grid,
        quantity=quantity,  # type: ignore[arg-type]
        unit="1",
        solar_zenith_range_deg=zenith,
        provenance=prov(),
    )


def cloud_canvas(seed: int = 1, n: int = SIDE + 2 * MARGIN) -> np.ndarray:
    rng = np.random.default_rng(seed)
    spec = np.fft.fft2(rng.standard_normal((n, n)))
    k = np.fft.fftfreq(n)
    kk = np.sqrt(k[:, None] ** 2 + k[None, :] ** 2)
    kk[0, 0] = 1.0
    field = np.fft.ifft2(spec / kk**1.5).real
    field = (field - field.mean()) / field.std()
    return np.clip(0.5 + 0.35 * field, 0.0, 1.0)


def crop(canvas: np.ndarray) -> np.ndarray:
    return canvas[MARGIN : MARGIN + SIDE, MARGIN : MARGIN + SIDE].copy()


def frames(d_row: float = 0.0, d_col: float = 6.0) -> tuple[np.ndarray, np.ndarray]:
    """Two frames ten minutes apart; the content moves by (d_row, d_col) pixels."""
    canvas = cloud_canvas()
    moved = ndimage.shift(canvas, (d_row, d_col), order=3, mode="wrap")
    return crop(canvas), crop(moved)


def mk_motion(u: float, v: float, confidence: float = 0.95) -> CloudMotion:
    speed = math.hypot(u, v)
    return CloudMotion(
        from_time_utc=T_PREV,
        to_time_utc=T_IMG,
        u_east_m_s=u,
        v_north_m_s=v,
        speed_m_s=speed,
        direction_to_deg=math.degrees(math.atan2(u, v)) % 360.0,
        confidence=confidence,
        method="test",
        provenance=prov(),
    )


def clear_sky_800(_when: datetime) -> float:
    return 800.0


def scenes_and_values(
    d_row: float = 0.0, d_col: float = 6.0, **scene_kw: object
) -> tuple[list[SatelliteScene], list[np.ndarray], GeoGrid]:
    grid = make_grid()
    a, b = frames(d_row, d_col)
    scenes = [
        make_scene(T_PREV, grid, **scene_kw),  # type: ignore[arg-type]
        make_scene(T_IMG, grid, **scene_kw),  # type: ignore[arg-type]
    ]
    return scenes, [a, b], grid


def point_at(grid: GeoGrid, row: int, col: int) -> GeoPoint:
    return GeoPoint(latitude_deg=grid.latitudes[row], longitude_deg=grid.longitudes[col])


# --- advect ------------------------------------------------------------------


def test_advect_zero_seconds_returns_input_unchanged() -> None:
    grid = make_grid()
    field = crop(cloud_canvas())
    field[5, 5] = np.nan
    out = advect(field, grid, mk_motion(7.0, -3.0), seconds=0.0)
    np.testing.assert_array_equal(out, field)
    assert out is not field  # a copy, not an alias


def test_advect_zero_motion_returns_input_unchanged() -> None:
    grid = make_grid()
    field = crop(cloud_canvas())
    np.testing.assert_array_equal(advect(field, grid, mk_motion(0.0, 0.0), seconds=900.0), field)


def test_advect_one_pixel_interval_matches_manual_roll_and_marks_entering_edge_nan() -> None:
    grid = make_grid()
    field = crop(cloud_canvas())
    seconds = 600.0
    u = PITCH_M / seconds  # exactly one pixel eastward in 600 s
    out = advect(field, grid, mk_motion(u, 0.0), seconds=seconds)
    rolled = np.roll(field, 1, axis=1)
    np.testing.assert_allclose(out[:, 1:], rolled[:, 1:], rtol=0, atol=1e-12)
    # The column that came in from outside the domain is unknown, not zero and not wrapped.
    assert np.isnan(out[:, 0]).all()
    assert not np.isnan(out[:, 1:]).any()


def test_advect_northward_moves_content_to_lower_rows_on_north_up_grid() -> None:
    grid = make_grid(lat_descending=True)
    field = crop(cloud_canvas())
    out = advect(field, grid, mk_motion(0.0, PITCH_M / 600.0), seconds=600.0)
    np.testing.assert_allclose(out[:-1, :], field[1:, :], atol=1e-12)
    assert np.isnan(out[-1, :]).all()  # southern edge: upstream is unknown
    # And on a south-up grid the same northward motion moves content to higher rows.
    grid_s = make_grid(lat_descending=False)
    out_s = advect(field, grid_s, mk_motion(0.0, PITCH_M / 600.0), seconds=600.0)
    np.testing.assert_allclose(out_s[1:, :], field[:-1, :], atol=1e-12)
    assert np.isnan(out_s[0, :]).all()


def test_advect_eastward_and_diagonal_signs() -> None:
    grid = make_grid()
    field = crop(cloud_canvas())
    # East and south: content moves right and down the array.
    out = advect(field, grid, mk_motion(2 * PITCH_M / 600.0, -3 * PITCH_M / 600.0), seconds=600.0)
    np.testing.assert_allclose(out[3:, 2:], field[:-3, :-2], atol=1e-12)
    assert np.isnan(out[:3, :]).all()
    assert np.isnan(out[:, :2]).all()


def test_advect_fractional_shift_is_exact_on_a_linear_ramp() -> None:
    grid = make_grid()
    cols = np.arange(SIDE, dtype=float)
    ramp = np.tile(cols * 0.002, (SIDE, 1))
    shift_px = 3.25
    out = advect(ramp, grid, mk_motion(shift_px * PITCH_M / 600.0, 0.0), seconds=600.0)
    expected = (cols - shift_px) * 0.002
    np.testing.assert_allclose(out[10, 4:], expected[4:], atol=1e-12)
    assert np.isnan(out[:, :3]).all()  # columns whose source lies left of the domain


def test_advect_shift_larger_than_domain_is_all_unknown() -> None:
    grid = make_grid()
    field = crop(cloud_canvas())
    out = advect(field, grid, mk_motion(1000.0, 0.0), seconds=3600.0)
    assert np.isnan(out).all()


def test_advect_does_not_turn_unknown_into_zero_and_keeps_input_nan() -> None:
    grid = make_grid()
    field = 0.1 + 0.8 * crop(cloud_canvas())  # strictly positive: any 0.0 in the output is a fill
    field[100:110, 100:110] = np.nan
    out = advect(field, grid, mk_motion(PITCH_M / 600.0 * 4, 0.0), seconds=600.0)
    assert np.isnan(out[100:110, 104:114]).all()  # the gap moved with the cloud
    assert np.isnan(out[:, :4]).all()  # entering region
    assert (out[np.isfinite(out)] > 0.0).all()  # no zero fill was invented


def test_advect_rejects_shape_mismatch() -> None:
    with pytest.raises(ValueError, match="grid shape"):
        advect(np.zeros((10, 10)), make_grid(), mk_motion(1.0, 0.0), seconds=60.0)


def test_advect_negative_seconds_reverses() -> None:
    grid = make_grid()
    field = crop(cloud_canvas())
    m = mk_motion(2 * PITCH_M / 600.0, 0.0)
    fwd = advect(field, grid, m, seconds=600.0)
    back = advect(fwd, grid, m, seconds=-600.0)
    np.testing.assert_allclose(back[:, :-4], field[:, :-4], atol=1e-12)


# --- nowcast_point: structure ---------------------------------------------


def test_nowcast_is_aligned_derived_never_observation_and_carries_limitations() -> None:
    scenes, values, grid = scenes_and_values()
    nc = nowcast_point(
        point_at(grid, 128, 150),
        scenes,
        values,
        horizon_s=1800.0,
        step_s=600.0,
        clear_sky=clear_sky_800,
    )
    assert isinstance(nc, IrradianceNowcast)
    assert isinstance(nc, SatelliteNowcast)
    assert nc.kind is not DataKind.OBSERVATION
    assert nc.kind is DataKind.DERIVED
    assert nc.provenance.kind is not DataKind.OBSERVATION
    assert len(nc.valid_times_utc) == len(nc.ghi_w_m2) == len(nc.clear_sky_index) == 4
    assert len(nc.clear_sky_ghi_w_m2) == 4
    assert nc.validation_status.startswith("NOT VALIDATED")
    assert nc.limitations
    assert nc.motion is not None and nc.method.startswith("satellite cloud-index advection")
    assert all(0.0 <= g <= 1.2 * 800.0 for g in nc.ghi_w_m2)
    for g, k, cs in zip(nc.ghi_w_m2, nc.clear_sky_index, nc.clear_sky_ghi_w_m2, strict=True):
        assert g == pytest.approx(k * cs)
    assert nc.valid_times_utc == sorted(nc.valid_times_utc)


def test_limitations_cover_the_known_failure_modes() -> None:
    scenes, values, grid = scenes_and_values()
    nc = nowcast_point(
        point_at(grid, 128, 150), scenes, values, horizon_s=1800.0, step_s=600.0,
        clear_sky=clear_sky_800,
    )
    text = " ".join(nc.limitations).lower()
    for needle in (
        "neither grow nor dissipate",
        "convective",
        "effective lead",
        "14-minute-old observation",
        "horizon cap",
        "parallax",
        "not corrected",
        "sunset",
        "not validated",
    ):
        assert needle in text, needle
    assert any("km" in lim and "2 km" in lim and "10 km" in lim for lim in nc.limitations)


# --- nowcast_point: effective lead time -------------------------------------


def test_effective_lead_is_image_age_plus_requested_lead_default_latency() -> None:
    scenes, values, grid = scenes_and_values()
    nc = nowcast_point(
        point_at(grid, 128, 150), scenes, values, horizon_s=1800.0, step_s=600.0,
        clear_sky=clear_sky_800,
    )
    assert DEFAULT_IMAGE_LATENCY_S == 840.0
    assert nc.image_time_utc == T_IMG
    assert nc.issued_at_utc == T_IMG + timedelta(seconds=840)
    assert nc.image_age_s == 840.0
    assert nc.requested_lead_s == [0.0, 600.0, 1200.0, 1800.0]
    assert nc.effective_lead_s == [840.0, 1440.0, 2040.0, 2640.0]
    # A "30 minute" request is a 44-minute forecast from the observation.
    assert nc.effective_lead_s[-1] / 60.0 == pytest.approx(44.0)
    assert nc.valid_times_utc[0] == nc.issued_at_utc
    assert nc.valid_times_utc[-1] == nc.issued_at_utc + timedelta(seconds=1800)
    assert nc.max_effective_lead_s == MAX_EFFECTIVE_LEAD_S == 7200.0


def test_effective_lead_uses_actual_issue_time_when_given() -> None:
    scenes, values, grid = scenes_and_values()
    issued = T_IMG + timedelta(minutes=20)
    nc = nowcast_point(
        point_at(grid, 128, 150), scenes, values, horizon_s=1200.0, step_s=600.0,
        clear_sky=clear_sky_800, issued_at_utc=issued,
    )
    assert nc.image_age_s == 1200.0
    assert nc.effective_lead_s == [1200.0, 1800.0, 2400.0]
    assert nc.issued_at_utc == issued


def test_horizon_beyond_the_cap_is_refused_not_truncated() -> None:
    scenes, values, grid = scenes_and_values()
    pt = point_at(grid, 128, 150)
    # 840 s age + 6360 s lead is exactly the 7200 s cap: allowed.
    ok = nowcast_point(pt, scenes, values, horizon_s=6360.0, step_s=600.0, clear_sky=clear_sky_800)
    assert ok.effective_lead_s[-1] <= MAX_EFFECTIVE_LEAD_S
    with pytest.raises(NowcastRefusedError, match="cap"):
        nowcast_point(pt, scenes, values, horizon_s=6361.0, step_s=600.0, clear_sky=clear_sky_800)
    with pytest.raises(NowcastRefusedError, match="cap"):
        persistence_nowcast(
            pt, scenes, values, horizon_s=7200.0, step_s=600.0, clear_sky=clear_sky_800
        )


# --- nowcast_point: physics of the result ----------------------------------


def test_advected_value_matches_manual_advection_of_the_field() -> None:
    scenes, values, grid = scenes_and_values()
    row, col = 128, 150
    nc = nowcast_point(
        point_at(grid, row, col), scenes, values, horizon_s=1200.0, step_s=600.0,
        clear_sky=clear_sky_800,
    )
    assert nc.motion is not None
    for lead, k in zip(nc.effective_lead_s, nc.clear_sky_index, strict=True):
        field = advect(values[-1], grid, nc.motion, seconds=lead)
        expected = float(default_clear_sky_index(field[row, col : col + 1])[0])
        assert k == pytest.approx(expected, abs=1e-9)


def test_cloud_arriving_from_the_west_is_seen_before_it_reaches_the_point() -> None:
    # A bright cloud strip moving east at 6 px/10 min; the point is ahead of it.
    grid = make_grid()
    base = np.full((SIDE + 2 * MARGIN, SIDE + 2 * MARGIN), 0.05)
    base[:, 60 + MARGIN : 100 + MARGIN] = 0.9
    base += 0.03 * cloud_canvas(3)  # texture so that the motion is observable
    moved = ndimage.shift(base, (0, 6), order=1, mode="nearest")
    values = [crop(base), crop(moved)]
    scenes = [make_scene(T_PREV, grid), make_scene(T_IMG, grid)]
    # Strip front edge at col ~106 in the later frame; point at col 125 is clear now.
    pt = point_at(grid, 128, 125)
    adv = nowcast_point(pt, scenes, values, horizon_s=3600.0, step_s=600.0, clear_sky=clear_sky_800)
    per = persistence_nowcast(
        pt, scenes, values, horizon_s=3600.0, step_s=600.0, clear_sky=clear_sky_800
    )
    assert adv.motion is not None and adv.motion.u_east_m_s > 0  # arriving, not leaving
    assert per.clear_sky_index[0] > 0.85  # persistence: clear now, clear forever
    assert all(abs(k - per.clear_sky_index[0]) < 1e-9 for k in per.clear_sky_index)
    assert min(adv.clear_sky_index) < 0.3  # advection: the strip arrives within the hour
    assert adv.clear_sky_index[0] > 0.85  # but not yet at the first valid time
    i = int(np.argmin(adv.clear_sky_index))
    assert i > 0  # shading comes later in the series, not at its start


def test_persistence_baseline_is_returned_and_differs_from_advection() -> None:
    scenes, values, grid = scenes_and_values()
    pt = point_at(grid, 128, 150)
    pair = nowcast_with_baseline(
        pt, scenes, values, horizon_s=3000.0, step_s=600.0, clear_sky=clear_sky_800
    )
    assert pair.advection.motion is not None and pair.advection.motion.speed_m_s > 1.0
    assert pair.persistence.motion is None
    assert pair.persistence.used_motion is False
    assert pair.persistence.method.startswith("persistence")
    assert pair.persistence.kind is not DataKind.OBSERVATION
    assert pair.advection.valid_times_utc == pair.persistence.valid_times_utc
    assert not np.allclose(pair.advection.ghi_w_m2, pair.persistence.ghi_w_m2)
    assert len(pair.ghi_difference_w_m2) == len(pair.advection.ghi_w_m2)
    assert max(abs(d) for d in pair.ghi_difference_w_m2) > 1.0
    # Persistence holds the clear-sky index fixed.
    assert len({round(k, 12) for k in pair.persistence.clear_sky_index}) == 1


def test_persistence_equals_advection_when_frames_are_identical() -> None:
    grid = make_grid()
    frame = crop(cloud_canvas())
    scenes = [make_scene(T_PREV, grid), make_scene(T_IMG, grid)]
    pt = point_at(grid, 128, 150)
    pair = nowcast_with_baseline(
        pt, scenes, [frame, frame.copy()], horizon_s=1800.0, step_s=600.0,
        clear_sky=clear_sky_800,
    )
    assert pair.advection.motion is not None and pair.advection.motion.speed_m_s < 1e-6
    np.testing.assert_allclose(pair.advection.ghi_w_m2, pair.persistence.ghi_w_m2, atol=1e-6)


def test_persistence_follows_the_clear_sky_curve() -> None:
    scenes, values, grid = scenes_and_values()
    decline = [800.0, 700.0, 600.0, 500.0]
    nc = persistence_nowcast(
        point_at(grid, 128, 150), scenes, values, horizon_s=1800.0, step_s=600.0,
        clear_sky=decline,
    )
    assert nc.clear_sky_ghi_w_m2 == decline
    k = nc.clear_sky_index[0]
    assert nc.ghi_w_m2 == pytest.approx([k * c for c in decline])


def test_persistence_needs_only_one_frame_advection_falls_back() -> None:
    scenes, values, grid = scenes_and_values()
    pt = point_at(grid, 128, 150)
    only = persistence_nowcast(
        pt, scenes[-1:], values[-1:], horizon_s=600.0, step_s=600.0, clear_sky=clear_sky_800
    )
    assert only.motion is None
    one_frame = nowcast_point(
        pt, scenes[-1:], values[-1:], horizon_s=600.0, step_s=600.0, clear_sky=clear_sky_800
    )
    assert one_frame.motion is None and one_frame.used_motion is False
    assert "only one frame" in " ".join(one_frame.limitations)
    assert one_frame.ghi_w_m2 == only.ghi_w_m2


def test_unusable_motion_falls_back_to_persistence_and_says_so() -> None:
    grid = make_grid()
    scenes = [make_scene(T_PREV, grid), make_scene(T_IMG, grid)]
    values = [crop(cloud_canvas(1)), crop(cloud_canvas(2))]  # unrelated: not a translation
    pt = point_at(grid, 128, 150)
    nc = nowcast_point(pt, scenes, values, horizon_s=1200.0, step_s=600.0, clear_sky=clear_sky_800)
    assert nc.motion is None and nc.used_motion is False
    assert "not applied" in nc.method
    assert "rejected" in " ".join(nc.limitations)
    per = persistence_nowcast(
        pt, scenes, values, horizon_s=1200.0, step_s=600.0, clear_sky=clear_sky_800
    )
    assert nc.ghi_w_m2 == per.ghi_w_m2


def test_uses_the_two_latest_frames_whatever_the_input_order() -> None:
    scenes, values, grid = scenes_and_values()
    pt = point_at(grid, 128, 150)
    fwd = nowcast_point(pt, scenes, values, horizon_s=1200.0, step_s=600.0, clear_sky=clear_sky_800)
    rev = nowcast_point(
        pt, scenes[::-1], values[::-1], horizon_s=1200.0, step_s=600.0, clear_sky=clear_sky_800
    )
    assert fwd.ghi_w_m2 == rev.ghi_w_m2


def test_series_is_cut_where_the_upstream_location_leaves_the_image() -> None:
    scenes, values, grid = scenes_and_values()
    # ~5 m/s east; at col 10 the upstream source passes the western edge within ~35 min.
    nc = nowcast_point(
        point_at(grid, 128, 10), scenes, values, horizon_s=4800.0, step_s=600.0,
        clear_sky=clear_sky_800,
    )
    assert nc.domain_truncated
    assert 0 < len(nc.ghi_w_m2) < 9
    assert all(math.isfinite(g) for g in nc.ghi_w_m2)  # nothing invented, nothing NaN
    assert nc.horizon_s == nc.requested_lead_s[-1]
    assert any("cut short" in lim for lim in nc.limitations)


# --- refusals ---------------------------------------------------------------


def test_night_or_unknown_zenith_scene_is_refused() -> None:
    pt = point_at(make_grid(), 128, 150)
    for zen in ((88.0, 95.0), None):
        scenes, values, _ = scenes_and_values(zenith=zen)
        with pytest.raises(NowcastRefusedError, match="visible"):
            nowcast_point(
                pt, scenes, values, horizon_s=600.0, step_s=600.0, clear_sky=clear_sky_800
            )
        with pytest.raises(NowcastRefusedError, match="visible"):
            persistence_nowcast(
                pt, scenes, values, horizon_s=600.0, step_s=600.0, clear_sky=clear_sky_800
            )


def test_wrong_quantity_and_mismatched_inputs_raise() -> None:
    scenes, values, grid = scenes_and_values()
    pt = point_at(grid, 128, 150)
    kw = {"horizon_s": 600.0, "step_s": 600.0, "clear_sky": clear_sky_800}
    refl = [
        make_scene(T_PREV, grid, quantity="reflectance"),
        make_scene(T_IMG, grid, quantity="reflectance"),
    ]
    with pytest.raises(ValueError, match="cloud_index"):
        nowcast_point(pt, refl, values, **kw)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="same length"):
        nowcast_point(pt, scenes, values[:1], **kw)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="identical grid"):
        nowcast_point(
            pt, [scenes[0], make_scene(T_IMG, make_grid(n=SIDE - 1))], values, **kw  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="distinct"):
        nowcast_point(pt, [scenes[1], scenes[1]], values, **kw)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="step_s"):
        nowcast_point(pt, scenes, values, horizon_s=600.0, step_s=10.0, clear_sky=clear_sky_800)
    with pytest.raises(ValueError, match="clear_sky sequence"):
        nowcast_point(pt, scenes, values, horizon_s=600.0, step_s=600.0, clear_sky=[800.0])
    with pytest.raises(ValueError, match="non-negative"):
        nowcast_point(pt, scenes, values, horizon_s=0.0, step_s=600.0, clear_sky=[-1.0])


def test_point_outside_the_grid_or_on_a_gap_is_refused() -> None:
    scenes, values, grid = scenes_and_values()
    kw = {"horizon_s": 600.0, "step_s": 600.0, "clear_sky": clear_sky_800}
    outside = GeoPoint(latitude_deg=-10.0, longitude_deg=134.5)
    with pytest.raises(NowcastRefusedError, match="outside"):
        nowcast_point(outside, scenes, values, **kw)  # type: ignore[arg-type]
    gap = [values[0], values[1].copy()]
    gap[1][120:140, 140:160] = np.nan
    with pytest.raises(NowcastRefusedError, match="unknown"):
        persistence_nowcast(point_at(grid, 128, 150), scenes, gap, **kw)  # type: ignore[arg-type]


def test_inputs_are_not_modified() -> None:
    scenes, values, grid = scenes_and_values()
    snapshot = [v.copy() for v in values]
    nowcast_point(
        point_at(grid, 128, 150), scenes, values, horizon_s=1200.0, step_s=600.0,
        clear_sky=clear_sky_800,
    )
    for v, s in zip(values, snapshot, strict=True):
        np.testing.assert_array_equal(v, s)


# --- motion + nowcast agree on direction -----------------------------------


@pytest.mark.science
def test_motion_estimate_feeds_advection_in_the_same_direction() -> None:
    # If the motion sign were wrong the advected field would move away from the truth.
    grid = make_grid()
    a, b = frames(0.0, 6.0)
    m = estimate_motion(a, b, grid, earlier_time=T_PREV, later_time=T_IMG)
    predicted = advect(b, grid, m, seconds=600.0)  # one more interval
    canvas = cloud_canvas()
    truth = crop(ndimage.shift(canvas, (0, 12), order=3, mode="wrap"))
    sl = (slice(20, -20), slice(20, -20))
    err_adv = np.nanmean(np.abs(predicted[sl] - truth[sl]))
    err_persist = np.mean(np.abs(b[sl] - truth[sl]))
    assert err_adv < 0.25 * err_persist


# --- helpers ------------------------------------------------------------------


def test_default_clear_sky_index_mapping() -> None:
    n = np.array([-0.5, 0.0, 0.5, 0.8, 1.0, 1.2])
    k = default_clear_sky_index(n)
    assert k[0] == pytest.approx(1.2)
    assert k[1] == pytest.approx(1.0)
    assert k[2] == pytest.approx(0.5)
    assert k[3] == pytest.approx(0.2, abs=0.01)  # continuity at n = 0.8
    assert k[4] == pytest.approx(0.0667, abs=0.01)
    assert k[5] == pytest.approx(0.05)


def test_parallax_is_kilometres_and_points_away_from_the_subsatellite_point() -> None:
    alice = GeoPoint(latitude_deg=-23.698, longitude_deg=133.8807)
    d10, bearing = parallax_displacement_km(alice, 10_000.0)
    d2, _ = parallax_displacement_km(alice, 2_000.0)
    assert d10 == pytest.approx(5.5, abs=0.5)  # satellite zenith about 29 degrees
    assert d2 == pytest.approx(d10 / 5.0)
    assert 180.0 < bearing < 270.0  # south-west of the sub-satellite point at 0N 140.7E
    adelaide = GeoPoint(latitude_deg=-34.9285, longitude_deg=138.6007)
    assert parallax_displacement_km(adelaide, 10_000.0)[0] > d10  # worse towards the south
    sub = GeoPoint(latitude_deg=0.0, longitude_deg=140.7)
    assert parallax_displacement_km(sub, 10_000.0)[0] == pytest.approx(0.0, abs=1e-6)
