"""Tests for rwi.satellite.cloudindex and rwi.satellite.retrieval. No network access.

Scenes and pixel arrays are constructed directly; nothing here imports the
satellite ingest modules.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from rwi.contracts import GeoGrid, SatelliteScene
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance, QualityFlag
from rwi.satellite.cloudindex import (
    KSTAR_CLIP_MAX,
    ReferenceEstimationError,
    clear_sky_index,
    clip_clear_sky_index,
    cloud_index,
    estimate_clear_sky_reflectance,
    estimate_max_reflectance,
    normalise_reflectance_by_sun,
)
from rwi.satellite.retrieval import (
    RetrievalStatus,
    compare_with_measurements,
    retrieve_field,
    retrieve_field_detailed,
    retrieve_ghi,
    retrieve_ghi_detailed,
)
from rwi.solar.clearsky import clear_sky_irradiance
from rwi.solar.geometry import solar_position

ALICE = GeoPoint(latitude_deg=-23.698, longitude_deg=133.8807, elevation_m=545.0)
SLOT_DAY = datetime(2027, 8, 24, 3, 0, tzinfo=UTC)  # about 12:30 local, Sun high
SLOT_NIGHT = datetime(2027, 8, 24, 14, 0, tzinfo=UTC)  # about 23:30 local
SHAPE = (5, 5)


def _grid() -> GeoGrid:
    lats = [-23.60 - 0.01 * i for i in range(SHAPE[0])]
    lons = [133.78 + 0.01 * j for j in range(SHAPE[1])]
    return GeoGrid(
        latitudes=lats,
        longitudes=lons,
        shape=SHAPE,
        resolution_km=1.0,
        bounds=(min(lats), min(lons), max(lats), max(lons)),
    )


def _scene(
    *,
    slot: datetime = SLOT_DAY,
    quantity: str = "reflectance",
    zenith_range: tuple[float, float] | None = (30.0, 32.0),
) -> SatelliteScene:
    prov = Provenance(
        provider="noaa-himawari9",
        dataset="AHI-L2-FLDK-ISatSS",
        kind=DataKind.OBSERVATION,  # the radiance is an observation; the retrieval is not
        retrieved_at_utc=slot + timedelta(minutes=14),
        observation_time_utc=slot,
    )
    return SatelliteScene(
        satellite="Himawari-9",
        band="C03",
        slot_time_utc=slot,
        grid=_grid(),
        quantity=quantity,  # type: ignore[arg-type]
        unit="1",
        solar_zenith_range_deg=zenith_range,
        provenance=prov,
    )


def _point_in_grid() -> GeoPoint:
    g = _grid()
    return GeoPoint(latitude_deg=g.latitudes[2], longitude_deg=g.longitudes[3])


def _day_inputs(slot: datetime = SLOT_DAY):  # type: ignore[no-untyped-def]
    pt = _point_in_grid()
    return pt, solar_position(pt, slot), clear_sky_irradiance(pt, slot)


# --- cloud index: exact arithmetic -------------------------------------------


def test_cloud_index_exact_endpoints_and_midpoint_representable() -> None:
    # Binary-exact values so the arithmetic is truly exact, not merely close.
    n = cloud_index(
        np.array([0.125, 0.875, 0.5]), clear_sky_reflectance=0.125, max_reflectance=0.875
    )
    assert n[0] == 0.0
    assert n[1] == 1.0
    assert n[2] == 0.5


def test_cloud_index_spec_values_clear_0p1_max_0p8() -> None:
    n = cloud_index(np.array([0.1, 0.8, 0.45]), clear_sky_reflectance=0.1, max_reflectance=0.8)
    assert n[0] == pytest.approx(0.0, abs=1e-12)
    assert n[1] == pytest.approx(1.0, abs=1e-12)
    assert n[2] == pytest.approx(0.5, abs=1e-12)


def test_cloud_index_is_not_clipped_and_handles_arrays_of_references() -> None:
    clear = np.array([0.1, 0.2])
    n = cloud_index(np.array([0.0, 1.0]), clear_sky_reflectance=clear, max_reflectance=0.8)
    assert n[0] < 0.0  # darker than clear ground
    assert n[1] > 1.0  # brighter than the cloud reference


def test_cloud_index_nan_in_nan_out_and_does_not_poison_neighbours() -> None:
    rho = np.array([0.1, np.nan, 0.45, 0.8])
    n = cloud_index(rho, clear_sky_reflectance=0.1, max_reflectance=0.8)
    assert math.isnan(n[1])
    assert np.isfinite(n[[0, 2, 3]]).all()
    assert np.nanmean(n) == pytest.approx((0.0 + 0.5 + 1.0) / 3.0, abs=1e-12)


def test_cloud_index_refuses_degenerate_reference_contrast() -> None:
    # rho_max == rho_clear would divide by zero; a tiny span would make n noise.
    rho = np.array([0.3, 0.31])
    assert np.isnan(cloud_index(rho, clear_sky_reflectance=0.3, max_reflectance=0.3)).all()
    assert np.isnan(cloud_index(rho, clear_sky_reflectance=0.3, max_reflectance=0.31)).all()
    assert np.isnan(cloud_index(rho, clear_sky_reflectance=0.5, max_reflectance=0.3)).all()
    assert np.isnan(
        cloud_index(rho, clear_sky_reflectance=np.nan, max_reflectance=0.8)
    ).all()


def test_normalise_reflectance_by_sun() -> None:
    out = normalise_reflectance_by_sun(np.array([0.25, 0.25]), np.array([60.0, 95.0]))
    assert out[0] == pytest.approx(0.5, abs=1e-12)  # cos 60 = 0.5
    assert math.isnan(out[1])  # Sun below horizon: never inf, never a number


# --- clear-sky index: the published piecewise relation -----------------------


@pytest.mark.science
def test_piecewise_relation_breakpoints_hand_computed() -> None:
    """Hammer et al. (2003) / Rigollier et al. (2004) Heliosat-2 relation.

    k* = 1.2                                   n <= -0.2
    k* = 1 - n                         -0.2 <  n <=  0.8
    k* = 2.0667 - 3.6667 n + 1.6667 n^2  0.8 <  n <=  1.1
    k* = 0.05                                  n >   1.1

    Hand-computed values:
      n = -0.5 -> 1.2
      n = -0.2 -> 1.2          (linear segment gives 1 - (-0.2) = 1.2: continuous)
      n =  0.0 -> 1.0
      n =  0.5 -> 0.5
      n =  0.8 -> 0.2          (linear 1 - 0.8 = 0.2; quadratic
                                2.0667 - 2.93336 + 1.066688 = 0.200058)
      n =  0.9 -> 2.0667 - 3.30003 + 1.350027 = 0.116657
      n =  1.0 -> 2.0667 - 3.6667 + 1.6667 = 0.0667
      n =  1.1 -> 2.0667 - 4.03337 + 2.016707 = 0.050037 ~ 0.05 (continuous)
      n =  1.5 -> 0.05
    """
    expected = {
        -0.5: 1.2,
        -0.2: 1.2,
        0.0: 1.0,
        0.5: 0.5,
        0.8: 0.2,
        0.9: 0.116657,
        1.0: 0.0667,
        1.1: 0.050037,
        1.5: 0.05,
    }
    for n, k in expected.items():
        got = float(clear_sky_index(np.array(n)))
        assert got == pytest.approx(k, abs=1e-4), f"n={n}"


@pytest.mark.science
def test_piecewise_relation_is_continuous_at_breakpoints() -> None:
    eps = 1e-9
    for bp in (-0.2, 0.8, 1.1):
        below = float(clear_sky_index(np.array(bp - eps)))
        above = float(clear_sky_index(np.array(bp + eps)))
        assert abs(below - above) < 1e-3, f"jump at n={bp}: {below} vs {above}"


@pytest.mark.science
def test_clear_sky_gives_unity_and_thick_cloud_gives_small() -> None:
    assert float(clear_sky_index(np.array(0.0))) == pytest.approx(1.0, abs=1e-12)
    # n at or below the lower breakpoint: clear sky (published value 1.2, an
    # allowance for enhancement) - at least 1.0 and no more than the published cap.
    for n in (-0.2, -1.0, -5.0):
        assert 1.0 <= float(clear_sky_index(np.array(n))) <= 1.2
    for n in (1.0, 1.1, 2.0, 10.0):
        assert float(clear_sky_index(np.array(n))) < 0.1


@pytest.mark.science
def test_clear_sky_index_monotonically_non_increasing_in_n() -> None:
    sweep = np.linspace(-2.0, 3.0, 50_001)
    k = clear_sky_index(sweep)
    assert np.isfinite(k).all()
    assert (np.diff(k) <= 1e-12).all(), "cloud-to-irradiance curve must never rise with cloud"
    assert k.max() <= 1.2 + 1e-12
    assert k.min() >= 0.05 - 1e-12


def test_clear_sky_index_propagates_nan() -> None:
    k = clear_sky_index(np.array([0.5, np.nan, 1.5]))
    assert k[0] == pytest.approx(0.5)
    assert math.isnan(k[1])
    assert k[2] == pytest.approx(0.05)


# --- clipping / cloud enhancement --------------------------------------------


def test_cloud_enhancement_slightly_above_one_survives() -> None:
    res = clip_clear_sky_index(np.array([1.0, 1.05, 1.1, 1.2]))
    assert res.values.tolist() == [1.0, 1.05, 1.1, 1.2]
    assert res.n_clipped == 0
    assert not res.was_clipped


def test_absurd_kstar_is_clipped_and_the_clip_is_recorded() -> None:
    res = clip_clear_sky_index(np.array([0.5, 1.2, 1.31, 2.5, -0.1, np.nan]))
    assert res.values[0] == 0.5
    assert res.values[1] == 1.2
    assert res.values[2] == KSTAR_CLIP_MAX
    assert res.values[3] == KSTAR_CLIP_MAX
    assert res.values[4] == 0.0
    assert math.isnan(res.values[5])  # NaN is never clipped into a number
    assert res.n_clipped_high == 2
    assert res.n_clipped_low == 1
    assert res.n_finite == 5
    assert "clipped 2 high" in res.describe()


def test_clip_rejects_bad_bounds() -> None:
    with pytest.raises(ValueError):
        clip_clear_sky_index(np.array([1.0]), lower=1.0, upper=0.5)


def test_clip_is_recorded_in_retrieved_provenance() -> None:
    # Force an absurd k* through a tightened ceiling: 1.0 < max_clear_sky_index < k*.
    scene = _scene(quantity="cloud_index")
    values = np.full(SHAPE, -1.0)  # n = -1 -> k* = 1.2
    pt, sp, cs = _day_inputs()
    res = retrieve_ghi_detailed(
        scene, values, pt, solar_position=sp, clear_sky_ghi=cs, max_clear_sky_index=1.1
    )
    assert res.available and res.clipped
    assert res.clear_sky_index == pytest.approx(1.1)
    assert res.measured.value == pytest.approx(1.1 * cs.ghi_w_m2)
    assert res.measured.provenance.notes is not None
    assert "clipped 1 high" in res.measured.provenance.notes


# --- reference estimation ----------------------------------------------------


def test_clear_sky_reference_is_per_pixel_low_percentile() -> None:
    rng = np.random.default_rng(0)
    ground = np.array([[0.20, 0.30], [0.35, 0.25]])
    cloud = rng.uniform(0.0, 0.5, size=(40, 2, 2))
    stack = ground + cloud
    stack[0] = ground  # at least one clear look at every pixel
    ref = estimate_clear_sky_reflectance(stack, percentile=0.0)
    np.testing.assert_allclose(ref, ground, atol=1e-12)
    ref5 = estimate_clear_sky_reflectance(stack)  # 5th percentile
    assert (ref5 >= ground - 1e-12).all()
    assert (ref5 < ground + 0.1).all()


def test_too_few_scenes_for_clear_ground_reference_raises() -> None:
    with pytest.raises(ReferenceEstimationError, match="at least 10"):
        estimate_clear_sky_reflectance(np.full((3, 4, 4), 0.3))
    with pytest.raises(ReferenceEstimationError, match="no default constant"):
        estimate_max_reflectance(np.full((3, 4, 4), 0.8))


def test_reference_estimation_with_no_valid_data_raises() -> None:
    with pytest.raises(ReferenceEstimationError):
        estimate_clear_sky_reflectance(np.full((12, 3, 3), np.nan))
    with pytest.raises(ReferenceEstimationError):
        estimate_max_reflectance(np.full((12, 3, 3), np.nan))


def test_pixels_with_too_few_valid_samples_are_nan_not_a_constant() -> None:
    stack = np.full((12, 2, 2), 0.3)
    stack[:, 0, 0] = np.nan
    stack[:9, 1, 1] = np.nan  # only 3 valid < 5
    ref = estimate_clear_sky_reflectance(stack)
    assert math.isnan(ref[0, 0])
    assert math.isnan(ref[1, 1])
    assert ref[0, 1] == pytest.approx(0.3)


def test_max_reflectance_pooled_and_per_pixel() -> None:
    stack = np.linspace(0.2, 0.9, 12)[:, None, None] * np.ones((12, 3, 3))
    pooled = estimate_max_reflectance(stack, percentile=100.0)
    assert pooled == pytest.approx(0.9)
    per_pixel = estimate_max_reflectance(stack, percentile=100.0, pooled=False)
    np.testing.assert_allclose(per_pixel, 0.9)


# --- retrieval: provenance ---------------------------------------------------


def _clear_scene_inputs() -> tuple[SatelliteScene, np.ndarray]:
    scene = _scene()
    return scene, np.full(SHAPE, 0.30)


def test_retrieved_ghi_is_derived_not_an_observation_and_carries_lineage() -> None:
    scene, rho = _clear_scene_inputs()
    pt, sp, cs = _day_inputs()
    now = SLOT_DAY + timedelta(minutes=14)
    m = retrieve_ghi(
        scene, rho, pt, solar_position=sp, clear_sky_ghi=cs,
        clear_sky_reflectance=0.30, max_reflectance=0.80, now=now,
    )
    assert m.variable == "ghi" and m.unit == "W/m2"
    p = m.provenance
    assert p.kind is DataKind.DERIVED
    assert p.kind is not DataKind.OBSERVATION
    assert not p.is_measurement and not p.is_prediction
    assert p.observation_time_utc is None
    assert p.valid_time_utc == SLOT_DAY
    assert p.dataset == "Himawari-9/C03"
    assert p.model_name is not None and "hammer" in p.model_name.lower()
    assert p.notes is not None
    assert "NOT A MEASUREMENT" in p.notes
    assert "slot_time_utc=2027-08-24T03:00:00+00:00" in p.notes
    assert "band=C03" in p.notes and "satellite=Himawari-9" in p.notes
    assert "image_age_at_issue_s=840" in p.notes  # 14 min
    # Clear ground (n = 0, k* = 1): GHI equals the clear-sky GHI.
    assert m.value == pytest.approx(cs.ghi_w_m2, rel=1e-9)
    assert m.quality is QualityFlag.UNVALIDATED
    assert m.uncertainty is None  # error unknown: no invented error bar
    # A test asserting this was an observation must fail.
    with pytest.raises(AssertionError):
        assert m.provenance.kind is DataKind.OBSERVATION


def test_cloudy_pixel_reduces_ghi_by_the_published_relation() -> None:
    scene = _scene()
    rho = np.full(SHAPE, 0.30)
    pt, sp, cs = _day_inputs()
    g = _grid()
    # Pixel at (2, 3) is the point's pixel: make n = 0.5 there -> k* = 0.5.
    rho[2, 3] = 0.30 + 0.5 * (0.80 - 0.30)
    res = retrieve_ghi_detailed(
        scene, rho, pt, solar_position=sp, clear_sky_ghi=cs,
        clear_sky_reflectance=0.30, max_reflectance=0.80,
    )
    assert g.latitudes[2] == pt.latitude_deg
    assert res.cloud_index == pytest.approx(0.5)
    assert res.clear_sky_index == pytest.approx(0.5)
    assert res.measured.value == pytest.approx(0.5 * cs.ghi_w_m2)


def test_accepts_bare_clear_sky_float_and_cloud_index_scene() -> None:
    scene = _scene(quantity="cloud_index")
    pt, sp, _ = _day_inputs()
    m = retrieve_ghi(scene, np.full(SHAPE, 0.2), pt, solar_position=sp, clear_sky_ghi=1000.0)
    assert m.value == pytest.approx(800.0)
    assert m.provenance.kind is DataKind.DERIVED


def test_mismatched_clear_sky_time_is_refused() -> None:
    scene, rho = _clear_scene_inputs()
    pt, sp, _ = _day_inputs()
    stale_cs = clear_sky_irradiance(pt, SLOT_DAY + timedelta(hours=3))
    with pytest.raises(ValueError, match="scan slot"):
        retrieve_ghi(scene, rho, pt, solar_position=sp, clear_sky_ghi=stale_cs,
                     clear_sky_reflectance=0.3, max_reflectance=0.8)


# --- night and low sun --------------------------------------------------------


def test_night_scene_returns_unavailable_with_reason_not_zero() -> None:
    scene = _scene(slot=SLOT_NIGHT, zenith_range=(110.0, 112.0))
    assert not scene.is_usable_visible
    pt, sp, cs = _day_inputs(SLOT_NIGHT)
    assert cs.ghi_w_m2 == 0.0  # physics: zero irradiance at night ...
    res = retrieve_ghi_detailed(
        scene, np.full(SHAPE, 0.3), pt, solar_position=sp, clear_sky_ghi=cs,
        clear_sky_reflectance=0.3, max_reflectance=0.8,
    )
    # ... but the retrieval is unavailable, which is a different statement.
    assert res.status is RetrievalStatus.NIGHT_OR_LOW_SUN
    assert res.measured.value is None
    assert res.measured.value != 0.0
    assert res.measured.quality is QualityFlag.MISSING
    assert "not 'zero irradiance'" in res.reason
    assert res.measured.provenance.kind is DataKind.DERIVED
    assert res.measured.provenance.notes is not None
    assert "night_or_low_sun" in res.measured.provenance.notes
    assert retrieve_ghi(
        scene, np.full(SHAPE, 0.3), pt, solar_position=sp, clear_sky_ghi=cs,
        clear_sky_reflectance=0.3, max_reflectance=0.8,
    ).value is None


def test_low_sun_at_the_point_is_refused_even_if_scene_range_is_usable() -> None:
    scene = _scene(zenith_range=(60.0, 85.0))  # scene min < 80 => is_usable_visible
    assert scene.is_usable_visible
    pt, _, cs = _day_inputs()
    sp = solar_position(pt, SLOT_DAY)
    low = sp.model_copy(update={"apparent_zenith_deg": 85.0, "zenith_deg": 85.0,
                                "apparent_elevation_deg": 5.0, "elevation_deg": 5.0})
    res = retrieve_ghi_detailed(
        scene, np.full(SHAPE, 0.3), pt, solar_position=low, clear_sky_ghi=cs,
        clear_sky_reflectance=0.3, max_reflectance=0.8,
    )
    assert res.status is RetrievalStatus.NIGHT_OR_LOW_SUN
    assert res.measured.value is None


def test_scene_without_declared_zenith_is_not_assumed_daylight() -> None:
    scene = _scene(zenith_range=None)
    pt, sp, cs = _day_inputs()
    res = retrieve_ghi_detailed(
        scene, np.full(SHAPE, 0.3), pt, solar_position=sp, clear_sky_ghi=cs,
        clear_sky_reflectance=0.3, max_reflectance=0.8,
    )
    assert res.status is RetrievalStatus.NIGHT_OR_LOW_SUN
    assert "no solar zenith range" in res.reason


def test_night_field_is_all_nan_with_reason_never_zero() -> None:
    scene = _scene(slot=SLOT_NIGHT, zenith_range=(110.0, 112.0))
    res = retrieve_field_detailed(
        scene, np.full(SHAPE, 0.3), clear_sky_ghi_field=np.zeros(SHAPE),
        clear_sky_reflectance=0.3, max_reflectance=0.8,
    )
    assert res.status is RetrievalStatus.NIGHT_OR_LOW_SUN
    assert np.isnan(res.ghi).all()
    assert not (res.ghi == 0.0).any()
    assert res.reason
    assert np.isnan(
        retrieve_field(scene, np.full(SHAPE, 0.3), clear_sky_ghi_field=np.zeros(SHAPE),
                       clear_sky_reflectance=0.3, max_reflectance=0.8)
    ).all()


def test_field_straddling_the_zenith_limit_needs_a_zenith_field() -> None:
    scene = _scene(zenith_range=(70.0, 88.0))
    cs = np.full(SHAPE, 200.0)
    rho = np.full(SHAPE, 0.3)
    refused = retrieve_field_detailed(
        scene, rho, clear_sky_ghi_field=cs, clear_sky_reflectance=0.3, max_reflectance=0.8
    )
    assert refused.status is RetrievalStatus.NIGHT_OR_LOW_SUN
    zen = np.full(SHAPE, 70.0)
    zen[:, 4] = 85.0
    ok = retrieve_field_detailed(
        scene, rho, clear_sky_ghi_field=cs, clear_sky_reflectance=0.3, max_reflectance=0.8,
        solar_zenith_deg_field=zen,
    )
    assert ok.available
    assert np.isnan(ok.ghi[:, 4]).all()
    assert np.allclose(ok.ghi[:, :4], 200.0)


def test_brightness_temperature_scene_is_refused() -> None:
    scene = _scene(quantity="brightness_temperature")
    pt, sp, cs = _day_inputs()
    res = retrieve_ghi_detailed(scene, np.full(SHAPE, 290.0), pt, solar_position=sp,
                                clear_sky_ghi=cs)
    assert res.status is RetrievalStatus.UNSUPPORTED_QUANTITY
    assert res.measured.value is None


# --- missing references / points / pixels -------------------------------------


def test_missing_references_refuse_rather_than_substitute_a_constant() -> None:
    scene, rho = _clear_scene_inputs()
    pt, sp, cs = _day_inputs()
    res = retrieve_ghi_detailed(scene, rho, pt, solar_position=sp, clear_sky_ghi=cs)
    assert res.status is RetrievalStatus.MISSING_REFERENCE
    assert res.measured.value is None
    assert "clear_sky_reflectance and max_reflectance" in res.reason
    half = retrieve_ghi_detailed(scene, rho, pt, solar_position=sp, clear_sky_ghi=cs,
                                 clear_sky_reflectance=0.3)
    assert half.status is RetrievalStatus.MISSING_REFERENCE
    fld = retrieve_field_detailed(scene, rho, clear_sky_ghi_field=np.full(SHAPE, 900.0))
    assert fld.status is RetrievalStatus.MISSING_REFERENCE
    assert np.isnan(fld.ghi).all()


def test_point_outside_scene_is_unavailable() -> None:
    scene, rho = _clear_scene_inputs()
    far = GeoPoint(latitude_deg=-20.0, longitude_deg=130.0)
    sp = solar_position(far, SLOT_DAY)
    res = retrieve_ghi_detailed(scene, rho, far, solar_position=sp, clear_sky_ghi=900.0,
                                clear_sky_reflectance=0.3, max_reflectance=0.8)
    assert res.status is RetrievalStatus.OUTSIDE_SCENE
    assert res.measured.value is None


def test_nan_pixel_gives_unavailable_and_nan_does_not_spread_in_field() -> None:
    scene, rho = _clear_scene_inputs()
    rho = rho.copy()
    rho[2, 3] = np.nan
    pt, sp, cs = _day_inputs()
    res = retrieve_ghi_detailed(scene, rho, pt, solar_position=sp, clear_sky_ghi=cs,
                                clear_sky_reflectance=0.3, max_reflectance=0.8)
    assert res.status is RetrievalStatus.NO_DATA
    assert res.measured.value is None

    field = retrieve_field(scene, rho, clear_sky_ghi_field=np.full(SHAPE, 800.0),
                           clear_sky_reflectance=0.3, max_reflectance=0.8)
    assert math.isnan(field[2, 3])
    assert np.isfinite(np.delete(field.ravel(), 2 * SHAPE[1] + 3)).all()
    assert float(np.nanmean(field)) == pytest.approx(800.0)


def test_field_uses_per_pixel_references_and_clips() -> None:
    scene = _scene()
    rho = np.full(SHAPE, 0.3)
    clear = np.full(SHAPE, 0.3)
    clear[0, 0] = np.nan  # no reference there
    out = retrieve_field(scene, rho, clear_sky_ghi_field=np.full(SHAPE, 1000.0),
                         clear_sky_reflectance=clear, max_reflectance=0.8)
    assert math.isnan(out[0, 0])
    assert out[1, 1] == pytest.approx(1000.0)
    det = retrieve_field_detailed(scene, rho, clear_sky_ghi_field=np.full(SHAPE, 1000.0),
                                  clear_sky_reflectance=clear, max_reflectance=0.8)
    assert det.clip is not None and det.clip.n_finite == SHAPE[0] * SHAPE[1] - 1


def test_shape_mismatch_raises() -> None:
    scene = _scene()
    with pytest.raises(ValueError, match="shape"):
        retrieve_field(scene, np.zeros((2, 2)), clear_sky_ghi_field=np.zeros(SHAPE))


# --- error metrics -------------------------------------------------------------


def test_perfect_match_gives_zero_error() -> None:
    x = [0.0, 100.0, 450.5, 900.0]
    m = compare_with_measurements(x, x)
    assert (m.bias_w_m2, m.mae_w_m2, m.rmse_w_m2) == (0.0, 0.0, 0.0)
    assert m.n_pairs == 4 and m.n_discarded == 0


def test_constant_offset_gives_exactly_that_bias() -> None:
    measured = np.array([100.0, 300.0, 600.0, 900.0])
    m = compare_with_measurements(measured + 25.0, measured)
    assert m.bias_w_m2 == pytest.approx(25.0)
    assert m.mae_w_m2 == pytest.approx(25.0)
    assert m.rmse_w_m2 == pytest.approx(25.0)
    m2 = compare_with_measurements(measured - 10.0, measured)
    assert m2.bias_w_m2 == pytest.approx(-10.0)


def test_mixed_errors_distinguish_bias_mae_rmse() -> None:
    m = compare_with_measurements([110.0, 290.0], [100.0, 300.0])
    assert m.bias_w_m2 == pytest.approx(0.0)
    assert m.mae_w_m2 == pytest.approx(10.0)
    assert m.rmse_w_m2 == pytest.approx(10.0)
    m = compare_with_measurements([100.0, 100.0], [100.0, 120.0])
    assert m.bias_w_m2 == pytest.approx(-10.0)
    assert m.rmse_w_m2 == pytest.approx(math.sqrt(200.0))


def test_metrics_discard_unavailable_pairs_and_refuse_empty() -> None:
    m = compare_with_measurements([None, 120.0, float("nan")], [0.0, 100.0, 50.0])  # type: ignore[list-item]
    assert m.n_pairs == 1 and m.n_discarded == 2
    assert m.bias_w_m2 == pytest.approx(20.0)
    assert "never been run on real data" in m.note
    with pytest.raises(ValueError):
        compare_with_measurements([float("nan")], [1.0])
    with pytest.raises(ValueError):
        compare_with_measurements([1.0, 2.0], [1.0])


# --------------------------------------------------------------------------- n limits


def test_the_cloud_index_limits_match_heliosat_2() -> None:
    """Mines ParisTech's Heliosat-2 software description limits n to [-0.5, 1.5]."""
    from rwi.satellite.cloudindex import N_CLAMP_HIGH, N_CLAMP_LOW

    assert N_CLAMP_LOW == -0.5
    assert N_CLAMP_HIGH == 1.5


def test_clamping_bounds_n_without_touching_the_plausible_range() -> None:
    import numpy as np

    from rwi.satellite.cloudindex import clamp_cloud_index

    raw = np.array([-40.0, -0.5, -0.2, 0.0, 0.5, 1.0, 1.5, 9.0])
    clamped = clamp_cloud_index(raw)
    assert clamped[0] == -0.5
    assert clamped[-1] == 1.5
    # Everything already inside the range is untouched, bit for bit.
    np.testing.assert_array_equal(clamped[1:-1], raw[1:-1])


def test_clamping_preserves_nan_rather_than_inventing_a_value() -> None:
    """Unknown is not a value to be clamped into range."""
    import numpy as np

    from rwi.satellite.cloudindex import clamp_cloud_index

    out = clamp_cloud_index(np.array([np.nan, 2.0]))
    assert np.isnan(out[0])
    assert out[1] == 1.5


def test_the_raw_index_is_still_unclamped_so_bad_references_stay_visible() -> None:
    """A clear-ground reference that is wrong produces a wild n. That is
    diagnostic information, and cloud_index must not hide it."""
    import numpy as np

    from rwi.satellite.cloudindex import cloud_index

    # Contrast of 0.2 is above min_contrast, so this is a real index, not a
    # refusal: a bright pixel far beyond the max reference gives n = 4.0.
    n = cloud_index(np.array([0.9]), clear_sky_reflectance=0.1, max_reflectance=0.3)
    assert float(n[0]) == pytest.approx(4.0)
    # And too little contrast is refused outright rather than amplified.
    refused = cloud_index(np.array([0.9]), clear_sky_reflectance=0.1, max_reflectance=0.12)
    assert np.isnan(refused[0])
