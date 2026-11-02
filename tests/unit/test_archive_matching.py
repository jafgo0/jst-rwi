"""Matching forecasts to observations inside spatial and temporal tolerances."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from test_archive_support import (
    DARWIN,
    T0,
    forecast_series,
    new_archive,
    observation_series,
    offset_point,
)

from rwi.core.timebase import TimeWindow

VAR = "wind_speed_10m"


def test_station_20_km_away_and_10_minutes_off(tmp_path: Path) -> None:
    station = offset_point(DARWIN, 20_000.0, 45.0)
    with new_archive(tmp_path) as archive:
        archive.record_series(forecast_series(value=10.0))
        archive.record_series(
            observation_series(times=[T0 + timedelta(minutes=10)], value=9.0, point=station)
        )
        generous = archive.pairs(variable=VAR, max_separation_m=50_000.0, max_time_offset_s=1800.0)
        tight_space = archive.pairs(variable=VAR, max_separation_m=10_000.0)
        tight_time = archive.pairs(variable=VAR, max_separation_m=50_000.0, max_time_offset_s=300.0)
        exact = archive.pairs(variable=VAR, max_separation_m=20_100.0, max_time_offset_s=600.0)

    assert len(generous) == 1
    assert generous[0].separation_m == pytest.approx(20_000.0, abs=1.0)
    assert generous[0].time_offset_s == 600.0  # observation minus forecast valid time
    assert generous[0].observation_point == station
    assert tight_space == []
    assert tight_time == []
    assert len(exact) == 1  # tolerances are inclusive


def test_observation_before_the_forecast_has_negative_offset(tmp_path: Path) -> None:
    with new_archive(tmp_path) as archive:
        archive.record_series(forecast_series(value=10.0))
        archive.record_series(observation_series(times=[T0 - timedelta(minutes=20)], value=9.0))
        (pair,) = archive.pairs(variable=VAR)
    assert pair.time_offset_s == -1200.0


def test_nearest_in_time_wins_then_nearest_in_space(tmp_path: Path) -> None:
    near_space = offset_point(DARWIN, 1_000.0, 0.0)
    far_space = offset_point(DARWIN, 30_000.0, 90.0)
    with new_archive(tmp_path) as archive:
        archive.record_series(forecast_series(value=10.0))
        archive.record_series(
            observation_series(times=[T0 + timedelta(minutes=5)], value=1.0, point=near_space,
                               provider="a")
        )
        archive.record_series(
            observation_series(times=[T0], value=2.0, point=far_space, provider="b")
        )
        archive.record_series(
            observation_series(times=[T0], value=3.0, point=near_space, provider="c")
        )
        pairs = archive.pairs(variable=VAR)
    assert len(pairs) == 1  # ONE pair per forecast, not one per candidate observation
    assert pairs[0].observed_value == 3.0  # on-time and nearest


def test_three_model_runs_make_three_pairs_with_three_lead_times(tmp_path: Path) -> None:
    with new_archive(tmp_path) as archive:
        archive.record_series(observation_series(value=9.0))
        for hours_ahead, forecast_value in ((24, 7.0), (12, 8.0), (3, 9.5)):
            archive.record_series(
                forecast_series(
                    value=forecast_value,
                    init=T0 - timedelta(hours=hours_ahead),
                    retrieved=T0 - timedelta(hours=hours_ahead - 1),
                )
            )
        pairs = archive.pairs(variable=VAR)
    assert len(pairs) == 3  # not 1 (collapsed), not 9 (cross product)
    assert sorted(p.lead_time_s for p in pairs) == [3 * 3600.0, 12 * 3600.0, 24 * 3600.0]
    assert {p.valid_time_utc for p in pairs} == {T0}


def test_two_stations_do_not_duplicate_a_forecast(tmp_path: Path) -> None:
    with new_archive(tmp_path) as archive:
        archive.record_series(forecast_series(value=10.0))
        archive.record_series(observation_series(value=9.0, provider="a"))
        archive.record_series(observation_series(value=8.0, provider="b",
                                                 point=offset_point(DARWIN, 5_000.0)))
        assert len(archive.pairs(variable=VAR)) == 1


def test_filters_by_period_provider_model_and_lead_range(tmp_path: Path) -> None:
    times = [T0 + timedelta(hours=h) for h in range(4)]
    with new_archive(tmp_path) as archive:
        archive.record_series(observation_series(times=times, value=[1.0, 2.0, 3.0, 4.0]))
        archive.record_series(
            forecast_series(valid_times=times, value=[1.5, 2.5, 3.5, 4.5], model_name="m1",
                            init=T0 - timedelta(hours=6), retrieved=T0 - timedelta(hours=5))
        )
        archive.record_series(
            forecast_series(valid_times=times, value=[0.0] * 4, model_name="m2",
                            provider="other", init=T0 - timedelta(hours=6),
                            retrieved=T0 - timedelta(hours=5))
        )
        everything = archive.pairs(variable=VAR)
        window = TimeWindow(start_utc=times[1], end_utc=times[3])  # half-open
        in_window = archive.pairs(variable=VAR, period=window)
        only_m1 = archive.pairs(variable=VAR, model_name="m1")
        only_other = archive.pairs(variable=VAR, provider="other")
        bucket = archive.pairs(variable=VAR, lead_time_range_s=(8 * 3600.0, 10 * 3600.0))
        nothing = archive.pairs(variable="ghi")

    assert len(everything) == 8
    assert sorted({p.valid_time_utc for p in in_window}) == times[1:3]
    assert {p.model_name for p in only_m1} == {"m1"} and len(only_m1) == 4
    assert {p.provider for p in only_other} == {"other"}
    assert len(bucket) == 4  # 2 valid hours x 2 models
    assert all(8 * 3600 <= p.lead_time_s < 10 * 3600 for p in bucket)
    assert nothing == []


def test_unusable_quality_is_not_paired(tmp_path: Path) -> None:
    from rwi.core.provenance import QualityFlag

    with new_archive(tmp_path) as archive:
        archive.record_series(observation_series(value=9.0))
        archive.record_series(forecast_series(value=10.0, quality=QualityFlag.OUT_OF_RANGE))
        assert archive.pairs(variable=VAR) == []


def test_negative_tolerance_is_a_programming_error(tmp_path: Path) -> None:
    with new_archive(tmp_path) as archive, pytest.raises(ValueError, match="non-negative"):
        archive.pairs(variable=VAR, max_time_offset_s=-1.0)
