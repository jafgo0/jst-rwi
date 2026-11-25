"""Synoptic engine (#10, #14): tendencies, meridional profile, front signatures, speed.

Synthetic hourly series with a known, hand-built front so every number the
tests assert follows from the construction, not from a fitted model.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from rwi.alerts.synoptic import (
    MIN_POINTS,
    RoutePointSeries,
    SynopticThresholds,
    analyse_route,
    detect_front_passages,
    front_speed,
    meridional_profile,
    pressure_tendency,
)
from rwi.contracts import WeatherSeries, WeatherStep
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance, measured

NOW = datetime(2026, 11, 25, 0, 0, tzinfo=UTC)
Fn = Callable[[int], float | None]


def _pt(lat: float, lon: float = 134.0) -> GeoPoint:
    return GeoPoint(latitude_deg=lat, longitude_deg=lon, elevation_m=100.0)


def _series(point: GeoPoint, hours: int, **fns: Fn) -> WeatherSeries:
    steps = []
    for h in range(hours):
        t = NOW + timedelta(hours=h)
        prov = Provenance(
            provider="openmeteo",
            dataset="forecast",
            kind=DataKind.FORECAST,
            retrieved_at_utc=NOW,
            valid_time_utc=t,
            point=point,
        )
        values = {}
        for var, fn in fns.items():
            v = fn(h)
            if v is not None:
                values[var] = measured(var, v, prov)
        steps.append(WeatherStep(valid_time_utc=t, values=values))
    return WeatherSeries(
        point=point,
        requested_point=point,
        provider="openmeteo",
        dataset="forecast",
        kind=DataKind.FORECAST,
        model_name="ecmwf_ifs025",
        retrieved_at_utc=NOW,
        temporal_resolution_s=3600.0,
        steps=steps,
    )


def _diurnal(h: int) -> float:
    return 25.0 + 6.0 * math.sin((h - 6) / 24.0 * 2.0 * math.pi)


def _front(point: GeoPoint, at: int, hours: int = 60) -> WeatherSeries:
    """Ideal SH cold front at hour ``at``: V-shaped trough, NW->SW, -6 K, -8 K dewpoint."""
    return _series(
        point,
        hours,
        pressure_msl=lambda h: 101000.0 + 50.0 * abs(h - at),
        wind_direction_10m=lambda h: 330.0 if h < at else 220.0,
        wind_speed_10m=lambda h: 6.0,
        temperature_2m=lambda h: _diurnal(h) - (6.0 if h > at else 0.0),
        dew_point_2m=lambda h: 10.0 if h < at else 2.0,
    )


def _ridge(point: GeoPoint, hours: int = 60) -> WeatherSeries:
    return _series(
        point,
        hours,
        pressure_msl=lambda h: 102000.0 + 2.0 * h,
        wind_direction_10m=lambda h: 120.0,
        wind_speed_10m=lambda h: 5.0,
        temperature_2m=_diurnal,
        dew_point_2m=lambda h: 5.0,
    )


def _rp(name: str, km: float, series: WeatherSeries | None) -> RoutePointSeries:
    point = series.point if series is not None else _pt(-12.0 - km / 111.0)
    return RoutePointSeries(name=name, route_distance_m=km * 1000.0, point=point, series=series)


# ----------------------------------------------------------------------------- fronts


def test_ideal_front_is_signature_with_all_four_criteria() -> None:
    fronts = detect_front_passages(_rp("A", 0, _front(_pt(-30.0), at=30)), now=NOW)
    assert len(fronts) == 1
    f = fronts[0]
    assert f.strength == "signature"
    assert set(f.criteria_met) == {
        "pressure_trough", "wind_shift", "temperature_drop", "dewpoint_drop"
    }
    assert f.passage_time_utc == NOW + timedelta(hours=30)
    assert f.dp_rise_pa == pytest.approx(150.0)  # raw MSLP rise, not the detrended one
    assert f.wind_shift_deg == pytest.approx(110.0)
    assert f.dt_24h_k == pytest.approx(-6.0)
    assert f.dtd_k == pytest.approx(-8.0)
    assert f.provenance.kind is DataKind.DERIVED
    assert "openmeteo" in (f.provenance.processing or "")


def _tide_with_front(at: int = 40) -> WeatherSeries:
    """Wind shift + drying at ``at`` with no synoptic trough; MSLP is a pure 12 h
    tide (1.5 hPa amplitude) whose minimum falls on ``at``."""
    return _series(
        _pt(-20.0),
        70,
        pressure_msl=lambda h: 101000.0 - 150.0 * math.cos((h - at) / 12.0 * 2.0 * math.pi),
        wind_direction_10m=lambda h: 330.0 if h < at else 220.0,
        wind_speed_10m=lambda h: 6.0,
        temperature_2m=_diurnal,
        dew_point_2m=lambda h: 10.0 if h < at else 2.0,
    )


def test_detrend_rejects_semidiurnal_tide_trough() -> None:
    """Calibration finding (docs/validation/synoptic-calibration.md): the 12 h tide
    alone makes a 'trough' on raw MSLP; the 24 h difference cancels it."""
    raw = SynopticThresholds(pressure_detrend_24h=False)
    (f_raw,) = detect_front_passages(_rp("A", 0, _tide_with_front()), raw, now=NOW)
    assert "pressure_trough" in f_raw.criteria_met
    (f,) = detect_front_passages(_rp("A", 0, _tide_with_front()), now=NOW)
    assert "pressure_trough" in f.criteria_evaluated
    assert "pressure_trough" not in f.criteria_met
    assert set(f.criteria_met) == {"wind_shift", "dewpoint_drop"}


def test_detrended_trough_not_evaluated_without_24h_history() -> None:
    """Unknown, not 'no trough': too little history for p(t) - p(t-24h)."""
    fronts = detect_front_passages(_rp("A", 0, _front(_pt(-30.0), at=10)), now=NOW)
    assert fronts
    assert all("pressure_trough" not in f.criteria_evaluated for f in fronts)
    assert all(f.dp_rise_pa is None for f in fronts)


def test_stable_ridge_has_no_front() -> None:
    assert detect_front_passages(_rp("A", 0, _ridge(_pt(-30.0))), now=NOW) == []


def test_two_kinematic_criteria_is_possible() -> None:
    s = _series(
        _pt(-30.0),
        60,
        pressure_msl=lambda h: 101000.0 + 50.0 * abs(h - 30),
        wind_direction_10m=lambda h: 330.0 if h < 30 else 220.0,
        wind_speed_10m=lambda h: 6.0,
        temperature_2m=_diurnal,
        dew_point_2m=lambda h: 8.0,
    )
    fronts = detect_front_passages(_rp("A", 0, s), now=NOW)
    assert [f.strength for f in fronts] == ["possible"]
    assert set(fronts[0].criteria_met) == {"pressure_trough", "wind_shift"}


def test_thermodynamic_only_drop_is_not_a_front() -> None:
    """Cooling + drying without trough or wind shift is no front (kinematic criterion needed)."""
    s = _series(
        _pt(-30.0),
        60,
        pressure_msl=lambda h: 101500.0,
        wind_direction_10m=lambda h: 120.0,
        wind_speed_10m=lambda h: 5.0,
        temperature_2m=lambda h: _diurnal(h) - (6.0 if h > 30 else 0.0),
        dew_point_2m=lambda h: 10.0 if h < 30 else 2.0,
    )
    assert detect_front_passages(_rp("A", 0, s), now=NOW) == []


def test_temperature_not_evaluated_without_24h_history() -> None:
    # Raw MSLP so the trough is evaluable this early; the detrended trough would
    # also lack its 24 h history (see test_detrended_trough_not_evaluated_...).
    raw = SynopticThresholds(pressure_detrend_24h=False)
    fronts = detect_front_passages(_rp("A", 0, _front(_pt(-30.0), at=10)), raw, now=NOW)
    assert len(fronts) == 1
    assert "temperature_drop" not in fronts[0].criteria_evaluated
    assert fronts[0].dt_24h_k is None
    assert fronts[0].strength == "signature"  # 3 of 3 evaluated


def test_light_wind_direction_is_not_used() -> None:
    s = _series(
        _pt(-30.0),
        60,
        pressure_msl=lambda h: 101000.0 + 50.0 * abs(h - 30),
        wind_direction_10m=lambda h: 330.0 if h < 30 else 220.0,
        wind_speed_10m=lambda h: 0.5,
        temperature_2m=_diurnal,
        dew_point_2m=lambda h: 8.0,
    )
    # pressure trough alone (1 criterion) -> nothing; wind not evaluated at all
    assert detect_front_passages(_rp("A", 0, s), now=NOW) == []


def _shift_only(d_before: float, d_after: float) -> WeatherSeries:
    """Flat MSLP (no trough), cooling and drying at hour 30, wind d_before -> d_after."""
    return _series(
        _pt(-30.0),
        60,
        pressure_msl=lambda h: 101000.0,
        wind_direction_10m=lambda h: d_before if h < 30 else d_after,
        wind_speed_10m=lambda h: 6.0,
        temperature_2m=lambda h: _diurnal(h) - (6.0 if h > 30 else 0.0),
        dew_point_2m=lambda h: 10.0 if h < 30 else 2.0,
    )


def test_backing_wind_shift_is_met() -> None:
    # NW (300) -> SW (200): -100 deg, counter-clockwise through W
    fronts = detect_front_passages(_rp("A", 0, _shift_only(300.0, 200.0)), now=NOW)
    assert fronts and all("wind_shift" in f.criteria_met for f in fronts)
    assert all("pressure_trough" not in f.criteria_met for f in fronts)


def test_veering_wind_shift_between_same_sectors_is_rejected() -> None:
    # NNE (20) -> SSE (160): both in the sectors, 140 deg, but clockwise through E
    fronts = detect_front_passages(_rp("A", 0, _shift_only(20.0, 160.0)), now=NOW)
    assert fronts == []  # no kinematic criterion left


def test_missing_pressure_is_unknown_not_zero() -> None:
    s = _series(_pt(-30.0), 30, temperature_2m=_diurnal)
    assert pressure_tendency("A", s, now=NOW) is None


# ----------------------------------------------------------------------------- tendencies / profile


def test_tendency_math() -> None:
    s = _series(_pt(-30.0), 30, pressure_msl=lambda h: 101000.0 + 10.0 * h)
    t = pressure_tendency("A", s, now=NOW)
    assert t is not None
    assert t.pressure_pa == pytest.approx(101000.0)
    assert t.dp_3h_pa == pytest.approx(30.0)
    assert t.dp_24h_pa == pytest.approx(240.0)


def test_short_window_has_no_24h_tendency() -> None:
    t = pressure_tendency("A", _series(_pt(-30.0), 6, pressure_msl=lambda h: 101000.0), now=NOW)
    assert t is not None
    assert t.dp_3h_pa == pytest.approx(0.0)
    assert t.dp_24h_pa is None


def test_meridional_profile_gradient() -> None:
    n = pressure_tendency("N", _series(_pt(-30.0), 4, pressure_msl=lambda h: 101000.0), now=NOW)
    s = pressure_tendency("S", _series(_pt(-31.0), 4, pressure_msl=lambda h: 101200.0), now=NOW)
    assert n is not None and s is not None
    (seg,) = meridional_profile([n, s])
    assert seg.dp_pa == pytest.approx(200.0)
    assert seg.distance_km == pytest.approx(110.9, abs=0.5)
    assert seg.gradient_pa_per_100km == pytest.approx(200.0 / seg.distance_km * 100.0)


# ----------------------------------------------------------------------------- speed / route


def test_front_speed_southward_from_two_points() -> None:
    a = detect_front_passages(_rp("A", 0, _front(_pt(-28.0), at=30)), now=NOW)
    b = detect_front_passages(_rp("B", 360, _front(_pt(-31.2), at=40)), now=NOW)
    speed, direction = front_speed([a, [], b])
    assert speed == pytest.approx(10.0)
    assert direction == "southward"


def test_front_speed_northward_when_south_point_first() -> None:
    a = detect_front_passages(_rp("A", 0, _front(_pt(-28.0), at=40)), now=NOW)
    b = detect_front_passages(_rp("B", 360, _front(_pt(-31.2), at=30)), now=NOW)
    assert front_speed([a, b]) == (pytest.approx(10.0), "northward")


def test_implausible_pairing_is_dropped() -> None:
    a = detect_front_passages(_rp("A", 0, _front(_pt(-28.0), at=30)), now=NOW)
    b = detect_front_passages(_rp("B", 360, _front(_pt(-31.2), at=31)), now=NOW)  # 100 m/s
    assert front_speed([a, b], SynopticThresholds()) == (None, None)


def test_single_point_front_gives_no_speed() -> None:
    a = detect_front_passages(_rp("A", 0, _front(_pt(-28.0), at=30)), now=NOW)
    assert front_speed([a, []]) == (None, None)


def test_analyse_route_end_to_end_with_one_failed_point() -> None:
    # 24 h look-back before "now", as the router fetches it, so the detrended
    # trough test has its history at both fronts.
    pts = [
        _rp("Port Augusta", 2700, _front(_pt(-32.5), at=64, hours=90)),
        _rp("Coober Pedy", 2180, _front(_pt(-29.0), at=50, hours=90)),
        _rp("Alice Springs", 1490, _ridge(_pt(-23.7))),
        RoutePointSeries("Tennant Creek", 986_000.0, _pt(-19.6), None, "HTTP 503"),
    ]
    a = analyse_route(pts, now=NOW + timedelta(hours=24))
    assert a.available
    assert a.points == ("Alice Springs", "Coober Pedy", "Port Augusta")
    assert a.missing == ("Tennant Creek",)
    assert a.calibrated is False
    assert [f.name for f in a.fronts] == ["Coober Pedy", "Port Augusta"]
    assert a.front_direction == "southward"
    assert a.front_speed_m_s == pytest.approx(520_000.0 / (14 * 3600.0))
    assert len(a.meridional_profile) == 2
    assert a.model_name == "ecmwf_ifs025"


def test_fewer_than_min_points_is_unavailable() -> None:
    pts = [_rp("A", 0, _ridge(_pt(-30.0))), _rp("B", 100, None)]
    a = analyse_route(pts, now=NOW)
    assert not a.available
    assert MIN_POINTS == 3
    assert a.fronts == () and a.tendencies == ()
    assert a.front_speed_m_s is None
    assert a.missing == ("B",)
    assert a.notes


def test_tendency_anchors_at_now_not_at_lookback_start() -> None:
    """A 24 h look-back must not turn yesterday's change into today's tendency."""
    s = _series(
        _pt(-30.0), 60, pressure_msl=lambda h: 101000.0 + (0.0 if h < 24 else 10.0 * (h - 24))
    )
    t = pressure_tendency("A", s, now=NOW + timedelta(hours=24, minutes=20))
    assert t is not None
    assert t.valid_time_utc == NOW + timedelta(hours=24)
    assert t.dp_3h_pa == pytest.approx(30.0)
    assert t.dp_24h_pa == pytest.approx(240.0)


def test_lookback_lets_temperature_criterion_run_for_an_early_front() -> None:
    """Front 6 h after 'now' with 24 h of look-back: all four criteria evaluated."""
    now = NOW + timedelta(hours=24)
    fronts = detect_front_passages(_rp("A", 0, _front(_pt(-30.0), at=30)), now=now)
    assert len(fronts) == 1
    assert "temperature_drop" in fronts[0].criteria_evaluated
