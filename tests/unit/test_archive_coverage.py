"""coverage() and prune()."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_archive_support import T0, forecast_series, new_archive, observation_series

from rwi.archive import SCHEMA_VERSION, SqliteArchive

VAR = "wind_speed_10m"


def test_empty_archive_returns_zeros_and_empty_lists(tmp_path: Path) -> None:
    with new_archive(tmp_path) as archive:
        cov = archive.coverage()
    assert cov is not None
    assert cov["series_count"] == 0
    assert cov["value_count"] == 0
    assert cov["forecast_value_count"] == 0
    assert cov["observation_value_count"] == 0
    assert cov["variables"] == []
    assert cov["providers"] == []
    assert cov["by_variable"] == []
    assert cov["daily_aggregate_rows"] == 0
    assert cov["earliest_valid_time_utc"] is None
    assert cov["latest_valid_time_utc"] is None
    assert cov["schema_version"] == SCHEMA_VERSION
    assert isinstance(cov["file_size_bytes"], int) and cov["file_size_bytes"] > 0
    assert cov["pairs"] == {
        "pair_count": 0, "forecast_values_considered": 0, "dropped_recorded_after_valid": 0,
        "dropped_no_init_time": 0, "dropped_negative_lead": 0, "unmatched_forecasts": 0,
        "max_separation_m": 50_000.0, "max_time_offset_s": 1800.0,
    }


def test_coverage_is_actionable(tmp_path: Path) -> None:
    times = [T0 + timedelta(hours=h) for h in range(3)]
    with new_archive(tmp_path) as archive:
        archive.record_series(forecast_series(valid_times=times, value=[1.0, 2.0, 3.0]))
        archive.record_series(forecast_series(valid_times=times, provider="bom-nwp",
                                              variable="temperature_2m", value=[20.0] * 3))
        archive.record_series(observation_series(times=times, value=[1.0, 2.0, 3.0]))
        cov = archive.coverage()
        no_pairs = archive.coverage(include_pair_counts=False)

    assert cov["series_count"] == 3
    assert (cov["value_count"], cov["forecast_value_count"], cov["observation_value_count"]) == (
        9, 6, 3,
    )
    assert cov["variables"] == ["temperature_2m", "wind_speed_10m"]
    assert cov["earliest_valid_time_utc"] == times[0].isoformat()
    assert cov["latest_valid_time_utc"] == times[-1].isoformat()
    providers = {p["provider"]: p for p in cov["providers"]}
    assert providers["open-meteo"] == {
        "provider": "open-meteo", "series_count": 1, "value_count": 3,
    }
    assert providers["bom"]["value_count"] == 3
    by_var = {v["variable"]: v for v in cov["by_variable"]}
    assert by_var["wind_speed_10m"]["pair_count"] == 3
    assert by_var["temperature_2m"]["pair_count"] == 0
    assert by_var["wind_speed_10m"]["forecast_value_count"] == 3
    assert cov["pairs"]["pair_count"] == 3
    assert no_pairs["pairs"] is None


def _populate_old_and_new(archive_path: Path) -> tuple[SqliteArchive, datetime]:
    old_times = [T0 + timedelta(hours=h) for h in range(4)]
    new_times = [T0 + timedelta(days=10, hours=h) for h in range(4)]
    archive = new_archive(archive_path)
    for times in (old_times, new_times):
        archive.record_series(observation_series(times=times, value=[10.0] * 4))
        archive.record_series(
            forecast_series(valid_times=times, value=[12.0, 11.0, 9.0, 10.0],
                            init=times[0] - timedelta(hours=6),
                            retrieved=times[0] - timedelta(hours=5))
        )
    return archive, T0 + timedelta(days=5)


def test_prune_dry_run_deletes_nothing_and_reports_the_count(tmp_path: Path) -> None:
    archive, cutoff = _populate_old_and_new(tmp_path)
    try:
        before = archive.coverage()
        would = archive.prune(older_than_utc=cutoff, dry_run=True)
        after = archive.coverage()
        assert would == 8  # 4 forecast + 4 observation values older than the cut-off
        assert after == before
        assert archive.daily_aggregates() == []
    finally:
        archive.close()


def test_prune_folds_old_pairs_into_daily_aggregates_then_deletes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    archive, cutoff = _populate_old_and_new(tmp_path)
    try:
        with caplog.at_level(logging.INFO, logger="rwi.archive"):
            deleted = archive.prune(older_than_utc=cutoff)
        assert deleted == 8
        assert "deleted 8 value rows" in caplog.text
        cov = archive.coverage()
        assert cov["value_count"] == 8  # only the recent day remains
        assert cov["series_count"] == 2  # emptied series removed
        assert len(archive.pairs(variable=VAR)) == 4
        (agg,) = archive.daily_aggregates(variable=VAR)
        assert agg["pair_count"] == 4
        # errors are 2, 1, -1, 0
        assert agg["sum_error"] == 2.0
        assert agg["sum_abs_error"] == 4.0
        assert agg["sum_sq_error"] == 6.0
        assert agg["lead_bucket_s"] == (6 * 3600, 12 * 3600)
        assert agg["model_name"] == "ecmwf_ifs025"
        # a second prune with the same cut-off has nothing left to do
        assert archive.prune(older_than_utc=cutoff) == 0
    finally:
        archive.close()


def test_prune_only_above_bytes_is_a_noop_for_a_small_archive(tmp_path: Path) -> None:
    archive, cutoff = _populate_old_and_new(tmp_path)
    try:
        assert archive.prune(older_than_utc=cutoff, only_above_bytes=10**12) == 0
        assert archive.coverage()["value_count"] == 16
    finally:
        archive.close()


def test_prune_keeps_observations_needed_for_boundary_forecasts(tmp_path: Path) -> None:
    cutoff = T0
    with new_archive(tmp_path) as archive:
        archive.record_series(observation_series(
            times=[T0 - timedelta(minutes=40), T0 - timedelta(minutes=10)], value=[1.0, 2.0]))
        archive.prune(older_than_utc=cutoff)
        # the 10-minute-old observation is inside the 30-minute matching margin: kept
        assert archive.coverage()["observation_value_count"] == 1


def test_schema_is_versioned_and_wal(tmp_path: Path) -> None:
    path = tmp_path / "rwi.sqlite3"
    with new_archive(tmp_path) as archive:
        archive.coverage()
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        raw.close()
