"""ISD row parsing and hourly gridding for the front-detector reference (#10, #14)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rwi.alerts.synoptic_obs import IsdReport, hourly_grid, parse_isd_row

# Real row shape from 94326099999 (Alice Springs), 2025-01-01T00:00 (values as published).
ROW = {
    "DATE": "2025-01-01T00:00:00",
    "REPORT_TYPE": "FM-12",
    "WND": "100,1,N,0077,1",
    "TMP": "+0327,1",
    "DEW": "-0004,1",
    "SLP": "10116,1",
}


def test_parse_scales_units() -> None:
    r = parse_isd_row(ROW)
    assert r is not None
    assert r.time_utc == datetime(2025, 1, 1, tzinfo=UTC)
    assert r.report_type == "FM-12"
    assert r.values["pressure_msl"] == pytest.approx(101160.0)
    assert r.values["temperature_2m"] == pytest.approx(32.7)
    assert r.values["dew_point_2m"] == pytest.approx(-0.4)
    assert r.values["wind_direction_10m"] == 100.0
    assert r.values["wind_speed_10m"] == pytest.approx(7.7)


def test_missing_codes_dropped() -> None:
    r = parse_isd_row(ROW | {"SLP": "99999,9", "TMP": "+9999,9", "WND": "999,9,9,9999,9"})
    assert r is not None
    assert set(r.values) == {"dew_point_2m"}


@pytest.mark.parametrize("qc", ["2", "3", "6", "7"])
def test_bad_quality_rejected(qc: str) -> None:
    r = parse_isd_row(ROW | {"SLP": f"10116,{qc}"})
    assert r is not None and "pressure_msl" not in r.values


def test_calm_wind_has_speed_zero_no_direction() -> None:
    r = parse_isd_row(ROW | {"WND": "999,9,C,0000,1"})
    assert r is not None
    assert r.values["wind_speed_10m"] == 0.0
    assert "wind_direction_10m" not in r.values


def test_variable_wind_keeps_speed_only() -> None:
    r = parse_isd_row(ROW | {"WND": "999,9,V,0031,1"})
    assert r is not None
    assert r.values["wind_speed_10m"] == pytest.approx(3.1)
    assert "wind_direction_10m" not in r.values


def test_row_without_values_or_date_is_none() -> None:
    assert parse_isd_row({"DATE": "2025-01-01T00:00:00", "SLP": "99999,9"}) is None
    assert parse_isd_row(ROW | {"DATE": "garbage"}) is None
    assert parse_isd_row({k: v for k, v in ROW.items() if k != "DATE"}) is None


def _rep(minute: int, typ: str, **vals: float) -> IsdReport:
    return IsdReport(datetime(2025, 1, 1, 0, tzinfo=UTC) + timedelta(minutes=minute), typ, vals)


def test_hourly_grid_picks_closest_and_prefers_synop_on_tie() -> None:
    reps = [
        _rep(0, "FM-15", temperature_2m=20.0, wind_speed_10m=5.0),
        _rep(0, "FM-12", temperature_2m=21.0, pressure_msl=101000.0),
        _rep(5, "FM-15", temperature_2m=25.0),
        _rep(30, "FM-15", temperature_2m=30.0),  # 30 min off both hours: outside tolerance
        _rep(55, "FM-15", temperature_2m=22.0),  # 5 min before 01:00
    ]
    g = hourly_grid(reps)
    h0, h1 = datetime(2025, 1, 1, 0, tzinfo=UTC), datetime(2025, 1, 1, 1, tzinfo=UTC)
    assert list(g) == [h0, h1]
    assert g[h0] == {"temperature_2m": 21.0, "pressure_msl": 101000.0, "wind_speed_10m": 5.0}
    assert g[h1] == {"temperature_2m": 22.0}


def test_hourly_grid_leaves_gaps_missing() -> None:
    g = hourly_grid([_rep(0, "FM-12", pressure_msl=1.0), _rep(180, "FM-12", pressure_msl=2.0)])
    assert len(g) == 2  # no interpolated 01:00 / 02:00
