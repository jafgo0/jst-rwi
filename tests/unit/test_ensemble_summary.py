"""Tests for rwi.ensemble.summary."""

from __future__ import annotations

import math

import pytest
from test_ensemble_support import at_one_time, ensemble, hour

from rwi.core.errors import UnknownVariableError
from rwi.core.provenance import DataKind
from rwi.ensemble import spread_skill_note, summarise
from rwi.ensemble.summary import PERCENTILE_METHOD, STD_DDOF


def test_documented_conventions() -> None:
    assert PERCENTILE_METHOD == "linear"
    assert STD_DDOF == 1


def test_exact_statistics_one_to_five() -> None:
    (s,) = summarise(at_one_time("temperature_2m", [1, 2, 3, 4, 5]), variable="temperature_2m")
    assert s.mean == 3.0
    assert s.median == 3.0
    assert s.minimum == 1.0
    assert s.maximum == 5.0
    # SAMPLE standard deviation (ddof=1): sqrt(sum((x-3)^2)/(n-1)) = sqrt(10/4) = sqrt(2.5).
    # The members are a small sample of the forecast distribution and we want that
    # distribution's spread; the population value sqrt(2) is asserted NOT to be used.
    assert s.standard_deviation == pytest.approx(math.sqrt(2.5), abs=1e-12)
    assert s.standard_deviation != pytest.approx(math.sqrt(2.0), abs=1e-6)
    assert s.member_count == 5
    assert s.unit == "degC"
    assert s.valid_time_utc == hour(0)


def test_percentile_50_equals_median_and_linear_interpolation() -> None:
    (s,) = summarise(at_one_time("temperature_2m", [1, 2, 3, 4, 5]), variable="temperature_2m")
    assert s.percentiles["50"] == s.median
    # linear (type 7): rank = p/100*(n-1): p10 -> 0.4 -> 1.4 ; p25 -> 1 -> 2 ; p90 -> 3.6 -> 4.6
    assert s.percentiles["10"] == pytest.approx(1.4)
    assert s.percentiles["25"] == pytest.approx(2.0)
    assert s.percentiles["75"] == pytest.approx(4.0)
    assert s.percentiles["90"] == pytest.approx(4.6)
    assert set(s.percentiles) == {"10", "25", "50", "75", "90"}


def test_p10_of_31_members_is_the_fourth_smallest() -> None:
    values = [float(v) for v in range(31)]
    (s,) = summarise(at_one_time("ghi", values), variable="ghi", percentiles=(10, 90))
    assert s.percentiles == {"10": 3.0, "90": 27.0}


def test_custom_percentiles_and_validation() -> None:
    (s,) = summarise(at_one_time("ghi", [1, 2, 3]), variable="ghi", percentiles=(0, 100))
    assert s.percentiles == {"0": 1.0, "100": 3.0}
    with pytest.raises(ValueError, match="percentile"):
        summarise(at_one_time("ghi", [1, 2, 3]), variable="ghi", percentiles=(101,))
    with pytest.raises(UnknownVariableError):
        summarise(at_one_time("ghi", [1, 2, 3]), variable="not_a_variable")


def test_provenance_names_model_members_and_statistic() -> None:
    (s,) = summarise(at_one_time("ghi", [100, 200, 300]), variable="ghi")
    p = s.provenance
    assert p.kind is DataKind.FORECAST
    assert p.valid_time_utc == hour(0)
    assert p.model_name == "ecmwf_ifs025"
    assert p.processing is not None
    assert "member_count=3" in p.processing
    assert "sample standard deviation" in p.processing
    assert "linear" in p.processing
    assert s.model_name == "ecmwf_ifs025"
    assert s.provider == "open-meteo"


def test_as_uncertainty_from_summary() -> None:
    (s,) = summarise(at_one_time("ghi", list(range(31))), variable="ghi")
    u = s.as_uncertainty()
    assert u.lower == s.percentiles["10"]
    assert u.upper == s.percentiles["90"]
    assert u.interval_probability == pytest.approx(0.8)
    assert u.standard_deviation == s.standard_deviation
    assert u.method is not None
    assert "31 members" in u.method


def test_one_summary_per_valid_time_sorted() -> None:
    fc = ensemble("ghi", [{2: 30.0, 0: 10.0, 1: 20.0}, {0: 12.0, 1: 22.0, 2: 32.0}])
    out = summarise(fc, variable="ghi")
    assert [s.valid_time_utc for s in out] == [hour(0), hour(1), hour(2)]
    assert [s.mean for s in out] == [11.0, 21.0, 31.0]


def test_missing_members_counted_and_noted() -> None:
    columns = [{0: float(i)} if i < 12 else {1: float(i)} for i in range(30)]
    out = summarise(ensemble("ghi", columns), variable="ghi")
    first, second = out
    assert first.valid_time_utc == hour(0)
    assert first.member_count == 12
    assert first.provenance.notes is not None
    assert "12 of 30" in first.provenance.notes
    assert "18 were missing" in first.provenance.notes
    assert first.provenance.processing is not None
    assert "member_count=12" in first.provenance.processing
    assert second.member_count == 18
    # statistics use only the members present
    assert first.mean == pytest.approx(sum(range(12)) / 12)


def test_complete_ensemble_has_no_missing_note() -> None:
    (s,) = summarise(at_one_time("ghi", list(range(15))), variable="ghi")
    assert s.provenance.notes is None


def test_none_values_are_missing_not_zero() -> None:
    fc = ensemble("ghi", [{0: 100.0}, {0: None}, {0: 300.0}])
    (s,) = summarise(fc, variable="ghi")
    assert s.member_count == 2
    assert s.mean == 200.0
    assert s.minimum == 100.0  # a None must not become a 0


def test_valid_time_with_no_members_produces_no_summary() -> None:
    fc = ensemble("ghi", [{0: None, 1: 5.0}, {0: None, 1: 7.0}])
    out = summarise(fc, variable="ghi")
    assert [s.valid_time_utc for s in out] == [hour(1)]
    assert all(s.member_count >= 1 for s in out)


def test_empty_ensemble_gives_no_summaries() -> None:
    assert summarise(ensemble("ghi", []), variable="ghi") == []


def test_single_member_has_zero_spread_and_says_so() -> None:
    (s,) = summarise(at_one_time("ghi", [420.0]), variable="ghi")
    assert s.member_count == 1
    assert s.standard_deviation == 0.0
    assert s.mean == s.median == s.minimum == s.maximum == 420.0
    assert s.provenance.notes is not None
    assert "one member carries no spread information" in s.provenance.notes
    note = spread_skill_note(s)
    assert "no spread information" in note
    assert "not 'certain'" in note


def test_small_ensemble_note() -> None:
    (s,) = summarise(at_one_time("ghi", [1, 2, 3]), variable="ghi")
    assert s.provenance.notes is not None
    assert "very coarse" in s.provenance.notes


# --------------------------------------------------------------------------
# Circular variables
# --------------------------------------------------------------------------


def _circ(values: list[float]):  # type: ignore[no-untyped-def]
    (s,) = summarise(at_one_time("wind_direction_10m", values), variable="wind_direction_10m")
    return s


def _angdiff(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


@pytest.mark.science
def test_circular_mean_wind_direction_across_north() -> None:
    values = [350.0, 355.0, 0.0, 5.0, 10.0]
    arithmetic = sum(values) / len(values)
    assert arithmetic == pytest.approx(144.0)  # the wrong answer, for reference
    s = _circ(values)
    # correct: ~0 deg (== 360). Compare on the circle so 359.99999 and 0.00001 both pass.
    assert _angdiff(s.mean, 0.0) < 1e-9
    assert 0.0 <= s.mean < 360.0
    # and explicitly NOT the arithmetic mean
    assert _angdiff(s.mean, arithmetic) > 100.0
    assert s.mean != pytest.approx(arithmetic, abs=1.0)


@pytest.mark.science
def test_circular_spread_and_median_and_arc() -> None:
    s = _circ([350.0, 355.0, 0.0, 5.0, 10.0])
    # R = mean of cos(0,5,10,..) : (1 + 2cos5deg + 2cos10deg)/5 ; sigma = sqrt(-2 ln R)
    r = (1 + 2 * math.cos(math.radians(5)) + 2 * math.cos(math.radians(10))) / 5
    expected = math.degrees(math.sqrt(-2 * math.log(r)))
    assert s.standard_deviation == pytest.approx(expected, rel=1e-9)
    assert 5.0 < s.standard_deviation < 10.0  # nothing like the ~160 deg linear std
    assert _angdiff(s.median, 0.0) < 1e-9
    # smallest arc containing the members: 350 -> 10 (crosses north, so max < min)
    assert s.minimum == 350.0
    assert s.maximum == 10.0


@pytest.mark.science
def test_circular_has_no_percentiles_and_says_so() -> None:
    s = _circ([350.0, 355.0, 0.0, 5.0, 10.0])
    assert s.percentiles == {}
    assert s.provenance.notes is not None
    assert "No percentiles" in s.provenance.notes
    assert "circular" in (s.provenance.processing or "").lower()
    u = s.as_uncertainty()
    assert u.lower is None and u.upper is None and u.interval_probability is None
    assert u.standard_deviation == s.standard_deviation


@pytest.mark.science
def test_circular_ordinary_case_matches_linear_away_from_north() -> None:
    s = _circ([80.0, 90.0, 100.0])
    assert s.mean == pytest.approx(90.0)
    assert s.median == 90.0
    assert (s.minimum, s.maximum) == (80.0, 100.0)


@pytest.mark.science
def test_circular_opposed_members_flag_dispersion() -> None:
    s = _circ([0.0, 90.0, 180.0, 270.0])
    assert math.isfinite(s.standard_deviation)
    assert s.provenance.notes is not None
    assert "dispersed all around the circle" in s.provenance.notes


@pytest.mark.science
def test_circular_note_names_circular_sd() -> None:
    assert "circular standard deviation" in spread_skill_note(_circ([350.0, 5.0, 10.0]))
