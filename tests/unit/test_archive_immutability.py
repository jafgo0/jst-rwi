"""A recorded forecast is never overwritten; the same retrieval is never duplicated."""

from __future__ import annotations

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


def test_same_retrieval_with_different_values_is_refused_not_overwritten(tmp_path: Path) -> None:
    first = forecast_series(value=5.0)
    changed = forecast_series(value=6.0)  # same identity, different number
    with new_archive(tmp_path) as archive:
        archive.record_series(first)
        with pytest.raises(ArchiveConflictError):
            archive.record_series(changed)
        archive.record_series(observation_series(value=5.0))
        (pair,) = archive.pairs(variable="wind_speed_10m")
    assert pair.forecast_value == 5.0


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


def test_retrieval_of_same_run_later_is_a_distinct_series(tmp_path: Path) -> None:
    a = forecast_series(retrieved=T0 - timedelta(hours=5))
    b = forecast_series(retrieved=T0 - timedelta(hours=4))  # same init, fetched an hour later
    with new_archive(tmp_path) as archive:
        assert archive.record_series(a) == 1
        assert archive.record_series(b) == 1
        assert archive.coverage()["series_count"] == 2
