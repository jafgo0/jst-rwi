"""The archive pair query's observation-provider filter (ADR 0011 §3)."""

from pathlib import Path

from test_archive_support import (
    DARWIN,
    STATION,
    T0,
    forecast_series,
    new_archive,
    observation_series,
)

from rwi.archive.store import SqliteArchive


def _seed(tmp_path: Path) -> SqliteArchive:
    archive = new_archive(tmp_path)
    archive.record_series(forecast_series(variable="ghi", value=600.0))
    archive.record_series(observation_series(variable="ghi", value=500.0, point=STATION))
    archive.record_series(
        observation_series(variable="ghi", value=520.0, point=DARWIN, provider="jst-onboard")
    )
    return archive


def test_filter_restricts_the_truth_side(tmp_path: Path) -> None:
    with _seed(tmp_path) as archive:
        [bom] = archive.pairs(variable="ghi", observation_provider="bom")
        [onboard] = archive.pairs(variable="ghi", observation_provider="jst-onboard")
        assert archive.pairs(variable="ghi", observation_provider="nobody") == []
    assert (bom.observed_value, bom.observation_point) == (500.0, STATION)
    assert (onboard.observed_value, onboard.observation_point) == (520.0, DARWIN)
    assert bom.valid_time_utc == onboard.valid_time_utc == T0


def test_no_filter_keeps_the_old_behaviour(tmp_path: Path) -> None:
    with _seed(tmp_path) as archive:
        unfiltered = archive.pairs(variable="ghi")
        report = archive.pairs_with_report(variable="ghi", observation_provider="bom")
    assert {p.observed_value for p in unfiltered} <= {500.0, 520.0}
    assert unfiltered
    assert [p.observed_value for p in report[0]] == [500.0]
