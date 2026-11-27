"""Race-week climatology: segments, hour selection, statistics (ADR 0016)."""

import itertools
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pvlib
import pytest

from rwi.climatology.raceweek import (
    SamplePoint,
    Segment,
    control_stop_segments,
    daily_energy_kwh,
    quantile_summary,
    race_week_window_utc,
    sample_distances_km,
    segment_climatology,
    tailwind_component,
    whole_hour_intervals,
    wind_rose_counts,
)
from rwi.contracts import WeatherSeries, WeatherStep
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Measured, Provenance
from rwi.core.variables import spec_for
from rwi.regulations import EVENT_TIMEZONE
from rwi.route.bwsc import OFFICIAL_2025_CONTROL_STOPS, OFFICIAL_2025_TOTAL_KM, ControlStopRef

RETRIEVED = datetime(2026, 11, 1, tzinfo=UTC)
POINT = GeoPoint(latitude_deg=-23.7, longitude_deg=133.875)


def test_segments_run_control_stop_to_control_stop() -> None:
    segs = control_stop_segments(OFFICIAL_2025_TOTAL_KM)
    assert len(segs) == len(OFFICIAL_2025_CONTROL_STOPS) + 1 == 10
    assert (segs[0].start_name, segs[0].end_name) == ("Darwin", "Katherine")
    assert (segs[-1].start_name, segs[-1].end_name) == ("Port Augusta", "Adelaide")
    assert segs[0].start_km == 0.0 and segs[-1].end_km == OFFICIAL_2025_TOTAL_KM
    for a, b in itertools.pairwise(segs):
        assert a.end_km == b.start_km and a.end_name == b.start_name
    assert [s.index for s in segs] == list(range(10))


def test_segments_reject_stop_outside_route() -> None:
    stop = ControlStopRef(1, "Nowhere", 5000.0, "x", "y")
    with pytest.raises(ValueError, match="strictly inside"):
        control_stop_segments(3000.0, stops=(stop,))


def test_sample_distances_are_cell_centred() -> None:
    seg = Segment(index=0, start_name="a", end_name="b", start_km=100.0, end_km=200.0)
    assert sample_distances_km(seg, 30.0) == pytest.approx(
        [100 + 100 / 3 * (i + 0.5) for i in range(3)]
    )
    assert sample_distances_km(seg, 500.0) == [150.0]  # at least the midpoint
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="spacing_km"):
            sample_distances_km(seg, bad)


def test_race_week_window_is_event_time_in_utc() -> None:
    start, end = race_week_window_utc(2024)
    assert start == datetime(2024, 8, 21, 14, 30, tzinfo=UTC)
    assert end == datetime(2024, 8, 29, 14, 30, tzinfo=UTC)


def test_whole_hour_intervals_drop_the_midnight_half_hours() -> None:
    ts = whole_hour_intervals(date(2024, 8, 22), EVENT_TIMEZONE)
    assert len(ts) == 23
    assert ts[0] == datetime(2024, 8, 21, 16, 0, tzinfo=UTC)
    assert ts[-1] == datetime(2024, 8, 22, 14, 0, tzinfo=UTC)


@pytest.mark.science
@pytest.mark.parametrize(
    ("lat", "lon"), [(-12.4634, 130.8456), (-34.9285, 138.6007)], ids=["darwin", "adelaide"]
)
@pytest.mark.parametrize("day", [22, 29])
def test_dropped_slivers_are_night(lat: float, lon: float, day: int) -> None:
    # The two half hours whole_hour_intervals leaves out sit at local midnight (ACST).
    # pvlib NREL SPA (Reda & Andreas 2004): the sun is far below the horizon there, so
    # daily GHI energy loses nothing by dropping them.
    ts = whole_hour_intervals(date(2024, 8, day), EVENT_TIMEZONE)
    gaps = [ts[0] - timedelta(hours=1) - timedelta(minutes=30), ts[-1]]
    times = pd.DatetimeIndex([g + timedelta(minutes=m) for g in gaps for m in range(0, 31, 5)])
    pos = pvlib.solarposition.get_solarposition(times, lat, lon, method="nrel_numpy")
    assert (pos["apparent_elevation"] < -30.0).all()


@pytest.mark.science
def test_tailwind_sign_convention() -> None:
    # Meteorological direction is where the wind comes FROM: from the south while
    # driving north pushes the car (tailwind, positive).
    assert tailwind_component(5.0, 180.0, 0.0) == pytest.approx(5.0)
    assert tailwind_component(5.0, 0.0, 0.0) == pytest.approx(-5.0)
    assert tailwind_component(5.0, 90.0, 0.0) == pytest.approx(0.0, abs=1e-12)
    assert tailwind_component(4.0, 315.0, 135.0) == pytest.approx(4.0)  # south-east bound


def test_wind_rose_bins_centred_on_north_and_skips_calm() -> None:
    counts, means, calm = wind_rose_counts([359.0, 11.0, 11.25, 180.0, 90.0], [2, 4, 6, 8, 0.0])
    assert calm == 1
    assert counts[0] == 2 and counts[1] == 1 and counts[8] == 1 and sum(counts) == 4
    assert means[0] == pytest.approx(3.0) and means[1] == pytest.approx(6.0)
    assert means[4] is None


@pytest.mark.science
def test_quantiles_are_numpy_type_7() -> None:
    # Hyndman & Fan (1996) type 7: for 1..11, Pk = 1 + 10k exactly.
    vals = [float(v) for v in range(1, 12)]
    q = quantile_summary(vals)
    assert (q["p10"], q["p25"], q["p50"], q["p75"], q["p90"]) == (2.0, 3.5, 6.0, 8.5, 10.0)
    assert (q["mean"], q["minimum"], q["maximum"]) == (6.0, 1.0, 11.0)
    rng = np.random.default_rng(7).normal(size=101)
    assert quantile_summary(list(rng))["p25"] == pytest.approx(float(np.quantile(rng, 0.25)))


def test_daily_energy_sums_hours_and_refuses_gaps() -> None:
    day = date(2024, 8, 22)
    hours: dict[datetime, float | None] = dict.fromkeys(
        whole_hour_intervals(day, EVENT_TIMEZONE), 100.0
    )
    assert daily_energy_kwh(day, dict(hours), EVENT_TIMEZONE) == pytest.approx(2.3)
    hours[next(iter(hours))] = None
    assert daily_energy_kwh(day, dict(hours), EVENT_TIMEZONE) is None


def _measured(var: str, value: float | None) -> Measured:
    prov = Provenance(
        provider="open-meteo", dataset="archive", kind=DataKind.ANALYSIS, retrieved_at_utc=RETRIEVED
    )
    return Measured(variable=var, value=value, unit=spec_for(var).unit, provenance=prov)


def _series(year: int, *, ghi_gap_at: datetime | None = None) -> WeatherSeries:
    """Full UTC days 21-29 Aug, as the archive returns them: constant values."""
    t0 = datetime(year, 8, 21, tzinfo=UTC)
    steps = []
    for i in range(9 * 24):
        t = t0 + timedelta(hours=i)
        values = {
            "ghi": 100.0 if t != ghi_gap_at else None,
            "cloud_cover": 20.0,
            "wind_speed_10m": 5.0,
            "wind_direction_10m": 180.0,
        }
        steps.append(
            WeatherStep(valid_time_utc=t, values={k: _measured(k, v) for k, v in values.items()})
        )
    return WeatherSeries(
        point=POINT,
        provider="open-meteo",
        dataset="archive",
        kind=DataKind.ANALYSIS,
        model_name="era5",
        retrieved_at_utc=RETRIEVED,
        spatial_resolution_km=25.0,
        steps=steps,
    )


def test_segment_climatology_selects_driving_hours_and_labels_climatology() -> None:
    seg = Segment(
        index=4, start_name="Barrow Creek", end_name="Alice Springs", start_km=0.0, end_km=10.0
    )
    points = [
        SamplePoint(distance_km=2.5, point=POINT, heading_deg=0.0),
        SamplePoint(distance_km=7.5, point=POINT, heading_deg=0.0),
    ]
    gap = datetime(2023, 8, 24, 2, 0, tzinfo=UTC)  # 11:30 ACST: inside driving hours
    series = [(i, y, _series(y, ghi_gap_at=gap)) for i in range(2) for y in (2023, 2024)]
    out = segment_climatology(seg, points, series, retrieved_at_utc=RETRIEVED)

    # 8 days x points x years; GHI hours end 09:30..16:30 (8), instants 08:30..16:30 (9).
    assert out.ghi is not None and out.ghi.n_samples == 8 * 8 * 4 - 2
    assert out.ghi.n_missing == 2
    assert out.cloud_cover is not None and out.cloud_cover.n_samples == 9 * 8 * 4
    assert out.cloud_cover.p50 == 20.0 and out.cloud_cover.unit == "percent"
    assert out.ghi.unit == "W/m2" and out.ghi.n_years == 2
    assert out.daily_ghi_energy is not None
    assert out.daily_ghi_energy.n_samples == 8 * 4 - 2 and out.daily_ghi_energy.n_missing == 2
    assert out.daily_ghi_energy.p50 == pytest.approx(2.3) and out.daily_ghi_energy.unit == "kWh/m2"
    assert out.tailwind_m_s is not None and out.tailwind_m_s.p50 == pytest.approx(5.0)
    assert out.wind_rose is not None and out.wind_rose.frequency[8] == 1.0
    assert out.wind_rose.n_samples == 9 * 8 * 4 and out.wind_rose.n_calm_excluded == 0
    for d in (out.ghi, out.cloud_cover, out.daily_ghi_energy, out.wind_rose):
        assert d.provenance.kind is DataKind.CLIMATOLOGY
        assert d.provenance.spatial_resolution_km == 25.0
        assert "2023-2024" in (d.provenance.processing or "")


def test_segment_climatology_needs_points() -> None:
    seg = Segment(index=0, start_name="a", end_name="b", start_km=0.0, end_km=1.0)
    with pytest.raises(ValueError, match="at least one sample point"):
        segment_climatology(seg, [], [], retrieved_at_utc=RETRIEVED)
