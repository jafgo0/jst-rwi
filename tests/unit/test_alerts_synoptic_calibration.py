"""Calibration scoring helpers (#10, #14, step B): matching, scores, phase locking.

Hand-built event lists so every expected count follows from the construction.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from rwi.alerts.synoptic_calibration import (
    EventMatch,
    event_scores,
    local_solar_hour,
    match_events,
    phase_locking,
)

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
TOL = timedelta(hours=6)


def _t(h: float) -> datetime:
    return T0 + timedelta(hours=h)


# ----------------------------------------------------------------------------- match_events


def test_match_within_tolerance_and_misses_false_alarms() -> None:
    m = match_events([_t(10), _t(50)], [_t(12), _t(30)], TOL)
    assert m.hits == ((_t(10), _t(12)),)
    assert m.misses == (_t(30),)
    assert m.false_alarms == (_t(50),)
    assert m.timing_errors_h == (pytest.approx(-2.0),)


def test_match_is_one_to_one_nearest_first() -> None:
    # Both forecasts are within tolerance of the single reference; the nearer wins.
    m = match_events([_t(10), _t(13)], [_t(14)], TOL)
    assert m.hits == ((_t(13), _t(14)),)
    assert m.false_alarms == (_t(10),)
    assert m.misses == ()


def test_match_greedy_does_not_steal_a_closer_pair() -> None:
    # f=10 is 1 h from r=11 and 5 h from r=15; f=14 is 1 h from r=15.
    m = match_events([_t(10), _t(14)], [_t(11), _t(15)], TOL)
    assert m.hits == ((_t(10), _t(11)), (_t(14), _t(15)))


def test_match_tolerance_is_inclusive() -> None:
    assert len(match_events([_t(6)], [_t(0)], TOL).hits) == 1
    assert len(match_events([_t(6.5)], [_t(0)], TOL).hits) == 0


def test_match_empty_inputs() -> None:
    m = match_events([], [_t(1)], TOL)
    assert m.hits == () and m.misses == (_t(1),) and m.false_alarms == ()


# ----------------------------------------------------------------------------- event_scores


def test_scores_pool_over_matchings() -> None:
    a = match_events([_t(10), _t(50)], [_t(12), _t(30)], TOL)  # H1 M1 F1
    b = match_events([_t(5)], [_t(4)], TOL)  # H1
    s = event_scores([a, b])
    assert (s.n_hits, s.n_misses, s.n_false_alarms) == (2, 1, 1)
    assert s.pod == pytest.approx(2 / 3)
    assert s.far == pytest.approx(1 / 3)
    assert s.csi == pytest.approx(2 / 4)
    assert s.mean_timing_error_h == pytest.approx((-2.0 + 1.0) / 2)
    assert s.mean_abs_timing_error_h == pytest.approx(1.5)


def test_undefined_ratios_are_none_not_zero() -> None:
    s = event_scores([EventMatch(hits=(), misses=(), false_alarms=())])
    assert s.pod is None and s.far is None and s.csi is None
    assert s.mean_timing_error_h is None and s.mean_abs_timing_error_h is None
    only_misses = event_scores([match_events([], [_t(1)], TOL)])
    assert only_misses.pod == 0.0 and only_misses.far is None and only_misses.csi == 0.0


# ----------------------------------------------------------------------------- local time


def test_local_solar_hour() -> None:
    assert local_solar_hour(_t(0), 0.0) == pytest.approx(0.0)
    assert local_solar_hour(_t(3.5), 135.0) == pytest.approx(12.5)
    assert local_solar_hour(_t(22), 30.0) == pytest.approx(0.0)
    assert local_solar_hour(_t(1), -30.0) == pytest.approx(23.0)


# ----------------------------------------------------------------------------- phase locking


def test_uniform_hours_are_not_locked() -> None:
    r = phase_locking([float(h) for h in range(24)])
    assert r.n == 24
    assert r.mean_resultant_length == pytest.approx(0.0, abs=1e-12)
    assert r.mean_local_hour is None
    assert r.p_value is not None and r.p_value > 0.9


def test_clustered_hours_are_locked() -> None:
    r = phase_locking([15.0] * 20)
    assert r.mean_resultant_length == pytest.approx(1.0)
    assert r.mean_local_hour == pytest.approx(15.0)
    assert r.p_value is not None and r.p_value < 1e-6


def test_mean_hour_wraps_midnight() -> None:
    r = phase_locking([23.0, 1.0])
    assert r.mean_local_hour is not None
    circ_dist = min(r.mean_local_hour, 24.0 - r.mean_local_hour)
    assert circ_dist == pytest.approx(0.0, abs=1e-9)


def test_second_harmonic_detects_bimodal_semidiurnal_locking() -> None:
    hours = [3.0] * 15 + [15.0] * 15  # two modes 12 h apart
    r1 = phase_locking(hours)
    assert r1.mean_resultant_length == pytest.approx(0.0, abs=1e-12)
    assert r1.p_value is not None and r1.p_value > 0.9
    r2 = phase_locking(hours, harmonic=2)
    assert r2.mean_resultant_length == pytest.approx(1.0)
    assert r2.mean_local_hour == pytest.approx(3.0)
    assert r2.p_value is not None and r2.p_value < 1e-6


def test_phase_locking_small_n() -> None:
    assert phase_locking([]).p_value is None and phase_locking([]).n == 0
    one = phase_locking([7.0])
    assert one.n == 1 and one.p_value is None
    assert one.mean_resultant_length == pytest.approx(1.0)
    assert one.mean_local_hour == pytest.approx(7.0)


def test_phase_locking_p_value_in_unit_interval() -> None:
    hours = [12.0 + 3.0 * math.sin(k) for k in range(40)]
    p = phase_locking(hours).p_value
    assert p is not None and 0.0 <= p <= 1.0


def test_harmonic_below_one_raises() -> None:
    with pytest.raises(ValueError):
        phase_locking([1.0, 2.0], harmonic=0)
