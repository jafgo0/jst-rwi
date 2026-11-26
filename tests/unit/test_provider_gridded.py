"""Gridded-run provider (ADR 0008 §1): interpolation, de-accumulation, hourly GHI/DNI.

All runs here are synthetic and labelled `DataKind.SIMULATION`; the irradiance
accumulations are built from pvlib clear sky times a known clear-sky index, so
the expected hourly answer is known by construction.
"""

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from rwi.config import Settings
from rwi.core.errors import CapabilityNotSupportedError, ProviderUnavailableError
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, QualityFlag
from rwi.core.timebase import TimeWindow
from rwi.providers.ecmwf_aifs import AifsOpenDataSource
from rwi.providers.gridded import (
    BBox,
    GriddedProvider,
    GriddedRun,
    bilinear_weights,
    deaccumulate,
    hourly_ghi_from_interval_means,
    series_from_run,
    wind_from_uv,
)
from rwi.solar.clearsky import clear_sky_series
from rwi.solar.geometry import solar_positions

# Test-only site in the Northern Territory; production code holds no coordinates.
POINT = GeoPoint(latitude_deg=-19.6, longitude_deg=134.2)
INIT = datetime(2027, 8, 23, 0, tzinfo=UTC)
K_TRUE = 0.7
LATS = np.array([-21.0, -20.0, -19.0, -18.0])
LONS = np.array([133.0, 134.0, 135.0, 136.0])


def _clear_sky_interval_energy(start: datetime, end: datetime) -> float:
    """Clear-sky GHI energy [J/m2] over [start, end] at POINT (1-minute midpoint rule)."""
    n = int((end - start).total_seconds() // 60)
    times = [start + timedelta(minutes=m + 0.5) for m in range(n)]
    return float(sum(c.ghi_w_m2 for c in clear_sky_series(POINT, times)) * 60.0)


def _run(steps_h: list[int] | None = None, kind: DataKind = DataKind.SIMULATION) -> GriddedRun:
    steps_h = steps_h if steps_h is not None else list(range(0, 49, 6))
    times = [INIT + timedelta(hours=h) for h in steps_h]
    nt, shape = len(times), (len(times), LATS.size, LONS.size)
    acc, total = [], 0.0
    prev = INIT
    for t in times:
        total += K_TRUE * _clear_sky_interval_energy(prev, t)
        acc.append(total)
        prev = t
    ssrd = np.broadcast_to(np.array(acc)[:, None, None], shape).copy()
    lat_grid = LATS[None, :, None] + 0 * LONS[None, None, :]
    lon_grid = 0 * LATS[None, :, None] + LONS[None, None, :]
    temp = np.broadcast_to(lat_grid + 0.1 * lon_grid, shape) + np.arange(nt)[:, None, None]
    return GriddedRun(
        provider="test",
        dataset="synthetic-grid",
        kind=kind,
        model_name="synthetic",
        model_version="1",
        init_time_utc=INIT,
        retrieved_at_utc=INIT,
        valid_times_utc=tuple(times),
        latitudes=LATS,
        longitudes=LONS,
        fields={
            "temperature_2m": temp,
            "wind_u_10m": np.full(shape, -3.0),
            "wind_v_10m": np.full(shape, -4.0),
            "precipitation_accum": np.broadcast_to(
                np.arange(nt, dtype=float)[:, None, None] * 6.0, shape
            ).copy(),
            "ssrd_accum": ssrd,
        },
        spatial_resolution_km=111.0,
        license="test",
        source_url=None,
    )


# -- pure numerics -----------------------------------------------------------


def test_bilinear_reproduces_a_linear_field_exactly():
    run = _run()
    got = run.at_point("temperature_2m", POINT)
    expected = POINT.latitude_deg + 0.1 * POINT.longitude_deg + np.arange(len(got))
    np.testing.assert_allclose(got, expected, atol=1e-9)


def test_bilinear_handles_descending_latitudes():
    run = _run()
    flipped = GriddedRun(
        **{
            **{k: getattr(run, k) for k in run.__dataclass_fields__ if not k.startswith("_")},
            "latitudes": LATS[::-1],
            "fields": {k: v[:, ::-1, :] for k, v in run.fields.items()},
        }
    )
    np.testing.assert_allclose(
        flipped.at_point("temperature_2m", POINT), run.at_point("temperature_2m", POINT)
    )


def test_bilinear_refuses_points_outside_the_grid():
    with pytest.raises(ValueError, match="outside the grid"):
        bilinear_weights(LATS, LONS, -22.0, 134.0)


def test_nan_corner_makes_the_value_missing():
    run = _run()
    run.fields["temperature_2m"][:, 1, 1] = np.nan
    assert np.all(np.isnan(run.at_point("temperature_2m", POINT)))


def test_wind_direction_is_where_the_wind_blows_from():
    speed, direction = wind_from_uv(np.array([0.0, -5.0, 3.0, 0.0]), np.array([-5.0, 0, 0, 0]))
    np.testing.assert_allclose(speed, [5.0, 5.0, 3.0, 0.0])
    np.testing.assert_allclose(direction, [0.0, 90.0, 270.0, 0.0])


def test_deaccumulate_intervals_noise_and_corruption():
    times = [INIT + timedelta(hours=h) for h in (6, 12, 18, 24)]
    got = deaccumulate(np.array([10.0, 25.0, 24.99999, 5.0]), times, INIT)
    assert got[0] == 10.0 and got[1] == 15.0 and got[2] == 0.0
    assert np.isnan(got[3])


def test_bbox_around_adds_margin_and_validates():
    box = BBox.around([POINT, GeoPoint(latitude_deg=-12.0, longitude_deg=131.0)], 1.0)
    assert (box.south, box.north, box.west, box.east) == (-20.6, -11.0, 130.0, 135.2)
    with pytest.raises(ValueError):
        BBox(south=1.0, north=0.0, west=0.0, east=1.0)


# -- hourly irradiance (physical invariants) -----------------------------------


@pytest.mark.science
def test_hourly_ghi_recovers_clear_sky_index_and_conserves_energy():
    # Reference: by construction, interval energy = K_TRUE x pvlib Ineichen clear sky,
    # so the hourly preceding-hour means must be ~K_TRUE x clear-sky hourly means and
    # each 6-hour interval's mean must equal the input interval mean.
    ends = [INIT + timedelta(hours=h) for h in range(6, 49, 6)]
    starts = [INIT, *ends[:-1]]
    means = np.array(
        [
            K_TRUE * _clear_sky_interval_energy(s, e) / (e - s).total_seconds()
            for s, e in zip(starts, ends, strict=True)
        ]
    )
    hours, ghi = hourly_ghi_from_interval_means(POINT, ends, INIT, means)
    assert len(hours) == 48
    assert np.all(np.isfinite(ghi)) and np.all(ghi >= 0.0)
    for i, (s, e) in enumerate(zip(starts, ends, strict=True)):
        sel = [s < h <= e for h in hours]
        assert ghi[sel].mean() == pytest.approx(means[i], rel=1e-9, abs=1e-9)
    cs = np.array([_clear_sky_interval_energy(h - timedelta(hours=1), h) / 3600.0 for h in hours])
    day = cs > 50.0
    np.testing.assert_allclose(ghi[day] / cs[day], K_TRUE, rtol=0.05)


@pytest.mark.science
def test_ghi_and_dni_are_exactly_zero_with_the_sun_down():
    # Hard rule 5: an hour whose clear sky is 0 all hour has GHI == 0.0, and DNI
    # is 0.0 whenever the Sun is below the horizon at the hour midpoint.
    run = _run()
    window = TimeWindow(start_utc=INIT + timedelta(hours=1), end_utc=INIT + timedelta(hours=48))
    series = series_from_run(run, POINT, window, ["ghi", "dni"])
    mids = solar_positions(POINT, [s.valid_time_utc - timedelta(minutes=30) for s in series.steps])
    saw_night = saw_day = False
    for step, sp in zip(series.steps, mids, strict=True):
        ghi, dni = step.values["ghi"].value, step.values["dni"].value
        assert ghi is not None and dni is not None
        assert ghi >= 0.0 and dni >= 0.0
        if sp.apparent_elevation_deg <= 0.0:
            assert dni == 0.0
            saw_night = True
        else:
            saw_day = True
    assert saw_night and saw_day
    night = [s for s in series.steps if s.valid_time_utc.hour in (12, 13, 14)]
    assert night and all(s.values["ghi"].value == 0.0 for s in night)


# -- provider surface -------------------------------------------------------


def test_series_provenance_and_quality():
    run = _run()
    window = TimeWindow(start_utc=INIT + timedelta(hours=1), end_utc=INIT + timedelta(hours=13))
    series = series_from_run(
        run,
        POINT,
        window,
        ["temperature_2m", "wind_speed_10m", "wind_direction_10m", "precipitation", "ghi"],
    )
    assert [s.valid_time_utc.hour for s in series.steps] == list(range(1, 13))
    assert series.kind is DataKind.SIMULATION and series.model_init_time_utc == INIT
    at_six = next(s for s in series.steps if s.valid_time_utc.hour == 6).values
    at_five = next(s for s in series.steps if s.valid_time_utc.hour == 5).values
    t5 = at_five["temperature_2m"]
    assert t5.quality is QualityFlag.INTERPOLATED
    assert t5.provenance.processing == "bilinear_grid_interp;linear_time_interp"
    assert t5.provenance.kind is DataKind.SIMULATION
    assert t5.provenance.model_name == "synthetic"
    assert t5.provenance.point == POINT
    assert t5.provenance.valid_time_utc == INIT + timedelta(hours=5)
    assert at_six["temperature_2m"].quality is QualityFlag.OK
    assert at_six["wind_speed_10m"].value == pytest.approx(5.0)
    assert at_six["wind_direction_10m"].value == pytest.approx(36.8699, abs=1e-3)
    assert at_five["precipitation"].value == pytest.approx(1.0)
    assert "deaccumulated" in (at_five["ghi"].provenance.processing or "")


def test_point_outside_run_is_refused_not_extrapolated():
    window = TimeWindow(start_utc=INIT, end_utc=INIT + timedelta(hours=6))
    far = GeoPoint(latitude_deg=-30.0, longitude_deg=134.0)
    with pytest.raises(ProviderUnavailableError, match="outside"):
        series_from_run(_run(), far, window, ["temperature_2m"])


def test_run_rejects_observation_kind_and_bad_shapes():
    with pytest.raises(ValueError, match="FORECAST or a SIMULATION"):
        _run(kind=DataKind.OBSERVATION)
    run = _run()
    with pytest.raises(ValueError, match="shape"):
        GriddedRun(
            **{
                **{k: getattr(run, k) for k in run.__dataclass_fields__ if not k.startswith("_")},
                "fields": {"temperature_2m": np.zeros((1, 2, 2))},
            }
        )


class _FakeSource:
    provider = "test"
    dataset = "synthetic-grid"
    fields = frozenset({"temperature_2m", "wind_u_10m", "wind_v_10m", "ssrd_accum"})
    max_forecast_hours = 48.0
    spatial_resolution_km = 111.0
    license = "test"
    documentation_url = "https://example.invalid"

    def latest_init_time(self, now: datetime) -> datetime:
        return INIT

    async def fetch_run(self, init_time, steps_h, fields, bbox):  # pragma: no cover
        raise AssertionError("tests never download")


async def test_provider_serves_registered_run_offline(tmp_path):
    settings = Settings(_env_file=None, allow_network=False, cache_dir=tmp_path)
    box = BBox(south=-21.0, north=-18.0, west=133.0, east=136.0)
    provider = GriddedProvider(_FakeSource(), box, settings, clock=lambda: INIT)
    assert provider.capabilities.variables == frozenset(
        {"temperature_2m", "wind_speed_10m", "wind_direction_10m", "ghi", "dni"}
    )
    window = TimeWindow(start_utc=INIT + timedelta(hours=1), end_utc=INIT + timedelta(hours=7))
    with pytest.raises(ProviderUnavailableError, match="refusing to invent"):
        await provider.fetch_forecast(POINT, window, ["temperature_2m"])
    provider.add_run(_run())
    series = await provider.fetch_forecast(POINT, window, ["temperature_2m", "ghi"])
    assert len(series.steps) == 6
    with pytest.raises(CapabilityNotSupportedError):
        await provider.fetch_forecast(POINT, window, ["precipitation"])


def test_aifs_latest_init_time_respects_publication_lag():
    src = AifsOpenDataSource()
    assert src.latest_init_time(datetime(2027, 8, 23, 6, 59, tzinfo=UTC)) == datetime(
        2027, 8, 22, 18, tzinfo=UTC
    )
    assert src.latest_init_time(datetime(2027, 8, 23, 13, 0, tzinfo=UTC)) == datetime(
        2027, 8, 23, 6, tzinfo=UTC
    )
