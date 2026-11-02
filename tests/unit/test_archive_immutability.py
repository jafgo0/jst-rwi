"""A recorded forecast is never overwritten; the same retrieval is never duplicated."""

from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from test_archive_support import T0, forecast_series, new_archive, observation_series

from rwi.archive import ArchiveConflictError, SqliteArchive


def test_second_model_run_for_same_valid_time_is_a_new_row(tmp_path: Path) -> None:
    older = forecast_series(value=8.0, init=T0 - timedelta(hours=12),
                            retrieved=T0 - timedelta(hours=11))
    newer = forecast_series(value=9.5, init=T0 - timedelta(hours=6),
                            retrieved=T0 - timedelta(hours=5))
    with new_archive(tmp_path) as archive:
        assert archive.record_series(older) == 1
        assert archive.record_series(newer) == 1
        archive.record_series(observation_series(value=9.0))
        pairs = archive.pairs(variable="wind_speed_10m")
        cov = archive.coverage()

    assert cov["forecast_value_count"] == 2
    assert sorted((p.forecast_value, p.lead_time_s) for p in pairs) == [
        (8.0, 12 * 3600.0),
        (9.5, 6 * 3600.0),
    ]  # the old forecast still holds its old value


def test_recording_identical_series_twice_writes_nothing(tmp_path: Path) -> None:
    series = forecast_series(valid_times=[T0, T0 + timedelta(hours=1)], value=[1.0, 2.0])
    with new_archive(tmp_path) as archive:
        assert archive.record_series(series) == 2
        assert archive.record_series(series) == 0
        assert archive.record_series(series.model_copy()) == 0
        cov = archive.coverage()
    assert cov["series_count"] == 1
    assert cov["value_count"] == 2


def test_same_cell_with_a_different_value_never_overwrites(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    first = forecast_series(value=5.0)
    changed = forecast_series(value=6.0)  # same model run, different number
    with new_archive(tmp_path) as archive:
        archive.record_series(first)
        with caplog.at_level(logging.WARNING, logger="rwi.archive"):
            assert archive.record_series(changed) == 0
        assert archive.last_record_report.conflicting == 1
        archive.record_series(observation_series(value=5.0))
        (pair,) = archive.pairs(variable="wind_speed_10m")
    assert pair.forecast_value == 5.0  # the first-archived value stands
    assert "already archived with a different value" in caplog.text


def test_a_series_contradicting_itself_is_refused(tmp_path: Path) -> None:
    good = forecast_series(valid_times=[T0, T0], value=[5.0, 6.0])
    with new_archive(tmp_path) as archive, pytest.raises(ArchiveConflictError):
        archive.record_series(good)


def test_database_refuses_updates_of_recorded_values(tmp_path: Path) -> None:
    path = tmp_path / "a.sqlite3"
    with SqliteArchive(path) as archive:
        archive.record_series(forecast_series(value=5.0))
    raw = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            raw.execute("UPDATE value SET value = 99.0")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            raw.execute("UPDATE series SET provider = 'x'")
    finally:
        raw.close()


def test_refetching_the_same_model_run_is_not_a_second_forecast(tmp_path: Path) -> None:
    """Hourly capture of a 6-hourly model must not count one forecast six times."""
    init = T0 - timedelta(hours=6)
    times = [T0, T0 + timedelta(hours=1)]
    first = forecast_series(valid_times=times, init=init, retrieved=T0 - timedelta(hours=5))
    again = forecast_series(valid_times=times, init=init, retrieved=T0 - timedelta(hours=4))
    with new_archive(tmp_path) as archive:
        assert archive.record_series(first) == 2
        assert archive.record_series(again) == 0
        assert archive.last_record_report.already_present == 2
        archive.record_series(observation_series(times=times))
        pairs = archive.pairs(variable="wind_speed_10m")
        assert archive.coverage()["series_count"] == 2  # one forecast run + one station
    assert len(pairs) == 2
    # the EARLIEST retrieval stands
    assert {p.forecast_recorded_at_utc for p in pairs} == {T0 - timedelta(hours=5)}


def test_a_refetch_extends_the_run_and_each_cell_keeps_its_own_retrieval_time(
    tmp_path: Path,
) -> None:
    """A late re-fetch must not smuggle hindsight in under the first fetch's timestamp."""
    init = T0 - timedelta(hours=6)
    early = [T0, T0 + timedelta(hours=2)]
    with new_archive(tmp_path) as archive:
        archive.record_series(
            forecast_series(valid_times=early, init=init, retrieved=T0 - timedelta(hours=5))
        )
        # Fetched at T0+90min, the same run now also supplies T0+1h: already in the past.
        late = forecast_series(
            valid_times=[T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2)],
            init=init,
            retrieved=T0 + timedelta(minutes=90),
        )
        assert archive.record_series(late) == 1
        archive.record_series(
            observation_series(times=[T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2)])
        )
        pairs, report = archive.pairs_with_report(variable="wind_speed_10m")
    assert sorted(p.valid_time_utc for p in pairs) == early
    assert report.dropped_recorded_after_valid == 1  # the T0+1h cell: fetched after the fact


def test_new_run_has_a_new_identity_even_at_the_same_valid_times(tmp_path: Path) -> None:
    with new_archive(tmp_path) as archive:
        a = forecast_series(init=T0 - timedelta(hours=12), retrieved=T0 - timedelta(hours=11))
        b = forecast_series(init=T0 - timedelta(hours=6), retrieved=T0 - timedelta(hours=11))
        assert archive.record_series(a) == 1
        assert archive.record_series(b) == 1


def test_observations_accumulate_in_one_series_per_station(tmp_path: Path) -> None:
    t = [T0 + timedelta(minutes=30 * i) for i in range(4)]
    with new_archive(tmp_path) as archive:
        assert archive.record_series(observation_series(times=t[:3], retrieved=t[2])) == 3
        # the next hourly poll overlaps the previous window and adds one reading
        assert archive.record_series(observation_series(times=t[1:], retrieved=t[3])) == 1
        assert archive.coverage()["series_count"] == 1
        assert archive.coverage()["observation_value_count"] == 4
