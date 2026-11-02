"""Round trip: what goes in comes out, with provenance exact."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_archive_support import (
    DARWIN,
    STATION,
    T0,
    forecast_series,
    new_archive,
    observation_series,
)

from rwi.archive import SqliteArchive, from_epoch_s, to_epoch_s


def test_round_trip_preserves_provenance_exactly(tmp_path: Path) -> None:
    init = T0 - timedelta(hours=6)
    fc = forecast_series(value=12.5, init=init, retrieved=T0 - timedelta(hours=5))
    ob = observation_series(value=11.0)
    with new_archive(tmp_path) as archive:
        assert archive.record_series(fc) == 1
        assert archive.record_series(ob) == 1
        pairs = archive.pairs(variable="wind_speed_10m")

    assert len(pairs) == 1
    p = pairs[0]
    assert p.provider == "open-meteo"
    assert p.model_name == "ecmwf_ifs025"
    assert p.forecast_point == DARWIN  # latitude, longitude AND elevation
    assert p.observation_point == STATION
    assert p.valid_time_utc == T0
    assert p.valid_time_utc.tzinfo is not None
    assert p.valid_time_utc.utcoffset() == timedelta(0)
    assert p.forecast_recorded_at_utc == T0 - timedelta(hours=5)
    assert p.lead_time_s == 6 * 3600  # valid minus MODEL INIT, not minus retrieval
    assert (p.forecast_value, p.observed_value) == (12.5, 11.0)
    assert p.error == 1.5


def test_series_provenance_columns_are_stored_once(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "a.sqlite3"
    with SqliteArchive(path) as archive:
        archive.record_series(
            forecast_series(valid_times=[T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2)],
                            value=[1.0, 2.0, 3.0])
        )
    raw = sqlite3.connect(path)
    try:
        (n_series,) = raw.execute("SELECT COUNT(*) FROM series").fetchone()
        (n_values,) = raw.execute("SELECT COUNT(*) FROM value").fetchone()
        row = raw.execute(
            "SELECT provider, dataset, kind, model_name, model_version, model_init_time_utc, "
            "retrieved_at_utc, latitude_deg, longitude_deg, elevation_m, requested_latitude_deg, "
            "spatial_resolution_km, temporal_resolution_s, license, source_url FROM series"
        ).fetchone()
        mode = raw.execute("PRAGMA journal_mode").fetchone()[0]
        version = raw.execute("PRAGMA user_version").fetchone()[0]
    finally:
        raw.close()
    assert (n_series, n_values) == (1, 3)
    assert row == (
        "open-meteo", "forecast", "forecast", "ecmwf_ifs025", "1",
        to_epoch_s(T0 - timedelta(hours=6)),  # times are INTEGER epoch seconds
        to_epoch_s(T0 - timedelta(hours=5)),
        DARWIN.latitude_deg, DARWIN.longitude_deg, 30.0, DARWIN.latitude_deg,
        25.0, 3600.0, "CC-BY-4.0", "https://example.test/forecast",
    )
    assert mode == "wal"
    assert version == 1


def test_epoch_conversion_is_utc_and_floors() -> None:
    when = datetime(2027, 8, 20, 12, 0, 0, 999_999, tzinfo=UTC)
    assert to_epoch_s(when) == to_epoch_s(when.replace(microsecond=0))
    assert from_epoch_s(to_epoch_s(T0)) == T0
    assert from_epoch_s(0).tzinfo is not None
