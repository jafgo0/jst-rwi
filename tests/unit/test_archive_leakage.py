"""THE leakage test: a forecast recorded after its valid time must never be scored."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import pytest
from test_archive_support import T0, forecast_series, new_archive, observation_series

from rwi.archive import SqliteArchive


def _seed(archive: SqliteArchive) -> None:
    archive.record_series(observation_series(value=9.0))
    # Honest forecast: retrieved 5 h before it is valid.
    archive.record_series(
        forecast_series(value=10.0, init=T0 - timedelta(hours=6),
                        retrieved=T0 - timedelta(hours=5), model_name="honest")
    )
    # "Forecast" retrieved 30 minutes AFTER the instant it claims to predict.
    archive.record_series(
        forecast_series(value=9.0, init=T0 - timedelta(hours=1),
                        retrieved=T0 + timedelta(minutes=30), model_name="hindsight")
    )
    # Retrieved at exactly the valid time: still not strictly before.
    archive.record_series(
        forecast_series(value=9.0, init=T0 - timedelta(hours=1), retrieved=T0,
                        model_name="same_instant")
    )


def test_forecast_recorded_after_valid_time_never_appears(tmp_path: Path) -> None:
    with new_archive(tmp_path) as archive:
        _seed(archive)
        pairs, report = archive.pairs_with_report(variable="wind_speed_10m")

    assert [p.model_name for p in pairs] == ["honest"]
    assert all(p.forecast_recorded_at_utc < p.valid_time_utc for p in pairs)
    # What was rejected is reported, not silently swallowed.
    assert report.dropped_recorded_after_valid == 2
    assert report.candidates == 3
    assert report.emitted == 1


def test_pairs_exposes_the_dropped_count_on_the_archive(tmp_path: Path) -> None:
    with new_archive(tmp_path) as archive:
        _seed(archive)
        archive.pairs(variable="wind_speed_10m")
        assert archive.last_pairs_report is not None
        assert archive.last_pairs_report.dropped_recorded_after_valid == 2
        pairs_info = archive.coverage()["pairs"]
    assert isinstance(pairs_info, dict)
    assert pairs_info["dropped_recorded_after_valid"] == 2
    assert pairs_info["pair_count"] == 1


def test_leaked_forecast_is_excluded_even_when_it_is_the_only_one(tmp_path: Path) -> None:
    with new_archive(tmp_path) as archive:
        archive.record_series(observation_series(value=9.0))
        archive.record_series(
            forecast_series(value=9.0, retrieved=T0 + timedelta(seconds=1))
        )
        pairs, report = archive.pairs_with_report(variable="wind_speed_10m")
    assert pairs == []
    assert report.dropped_recorded_after_valid == 1


def test_leakage_is_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with new_archive(tmp_path) as archive:
        _seed(archive)
        with caplog.at_level(logging.WARNING, logger="rwi.archive"):
            archive.pairs(variable="wind_speed_10m")
    assert "rejected 2 forecast value(s)" in caplog.text


def test_forecast_without_init_time_is_not_scored_with_a_guessed_lead(tmp_path: Path) -> None:
    series = forecast_series(value=9.0)
    no_init = series.model_copy(update={"model_init_time_utc": None})
    # the per-value provenance still names an init time; the series-level one is what is stored
    with new_archive(tmp_path) as archive:
        archive.record_series(observation_series(value=9.0))
        archive.record_series(no_init)
        pairs, report = archive.pairs_with_report(variable="wind_speed_10m")
    assert pairs == []
    assert report.dropped_no_init_time == 1
