"""Tests for rwi.ensemble.probability (member frequencies, not probabilities)."""

from __future__ import annotations

import pytest
from test_ensemble_support import at_one_time, ensemble, hour

from rwi.ensemble import (
    irradiance_stays_above,
    member_frequency_throughout,
    probability_above,
    probability_below,
    probability_in_sector,
    probability_note,
)
from rwi.ensemble._common import EnsembleDataError


def _above(fc, threshold, variable="ghi"):  # type: ignore[no-untyped-def]
    return probability_above(fc, variable=variable, threshold=threshold, valid_time_utc=hour(0))


def test_nine_of_thirty_is_exactly_point_three() -> None:
    values = [500.0] * 9 + [100.0] * 21
    fc = at_one_time("ghi", values)
    assert len(fc.members) == 30
    p = _above(fc, 300.0)
    assert p == 9 / 30
    assert p == 0.3  # exact ratio, not rounded or approximated


def test_all_and_none() -> None:
    assert _above(at_one_time("ghi", [900.0] * 30), 500.0) == 1.0
    assert _above(at_one_time("ghi", [100.0] * 30), 500.0) == 0.0


def test_boundary_member_equal_to_threshold_is_not_above() -> None:
    # 10 members: 4 greater, 2 exactly equal, 4 smaller
    fc = at_one_time("ghi", [600.0] * 4 + [500.0] * 2 + [400.0] * 4)
    assert _above(fc, 500.0) == 0.4  # strictly greater
    below = probability_below(fc, variable="ghi", threshold=500.0, valid_time_utc=hour(0))
    assert below == 0.4  # strictly smaller
    assert _above(fc, 500.0) + below + 0.2 == pytest.approx(1.0)


def test_denominator_is_members_with_a_value() -> None:
    columns = [{0: 900.0}] * 3 + [{0: None}] * 5 + [{0: 100.0}]
    assert _above(ensemble("ghi", columns), 500.0) == 3 / 4


def test_no_members_raises_instead_of_zero() -> None:
    fc = ensemble("ghi", [{0: 900.0}, {0: 800.0}])
    with pytest.raises(EnsembleDataError, match="no ensemble member"):
        probability_above(fc, variable="ghi", threshold=1.0, valid_time_utc=hour(10))
    with pytest.raises(EnsembleDataError):
        _above(ensemble("ghi", []), 1.0)


def test_time_tolerance() -> None:
    from datetime import timedelta

    fc = ensemble("ghi", [{0: 900.0}, {0: 100.0}])
    near = hour(0) + timedelta(minutes=20)
    assert probability_above(fc, variable="ghi", threshold=500.0, valid_time_utc=near) == 0.5
    with pytest.raises(EnsembleDataError):
        probability_above(
            fc, variable="ghi", threshold=500.0, valid_time_utc=near, tolerance_s=60.0
        )


def test_circular_variable_rejected_for_threshold_queries() -> None:
    fc = at_one_time("wind_direction_10m", [350.0, 10.0])
    with pytest.raises(ValueError, match="circular"):
        _above(fc, 180.0, "wind_direction_10m")
    with pytest.raises(ValueError, match="circular"):
        probability_below(
            fc, variable="wind_direction_10m", threshold=180.0, valid_time_utc=hour(0)
        )


def test_sector_probability_wraps_north() -> None:
    fc = at_one_time("wind_direction_10m", [350.0, 355.0, 0.0, 5.0, 10.0, 90.0, 180.0, 270.0])
    p = probability_in_sector(
        fc, variable="wind_direction_10m", centre_deg=0.0, half_width_deg=15.0,
        valid_time_utc=hour(0),
    )
    assert p == 5 / 8
    with pytest.raises(ValueError):
        probability_in_sector(
            at_one_time("ghi", [1.0]), variable="ghi", centre_deg=0.0, half_width_deg=1.0,
            valid_time_utc=hour(0),
        )


def test_note_says_member_frequency_not_calibrated() -> None:
    fc = at_one_time("ghi", [float(i) for i in range(30)])
    note = probability_note(fc, "ghi")
    assert "not a calibrated probability" in note
    assert "30" in note
    assert "3.3 percentage points" in note
    assert "ecmwf_ifs025" in note
    assert "not been verified" in note


def test_note_with_no_members() -> None:
    assert "No ecmwf_ifs025 ensemble member" in probability_note(ensemble("ghi", []), "ghi")


# --- window helpers -------------------------------------------------------


def _ghi_window() -> object:
    # 4 members over hours 0..3
    cols = [
        {0: 800.0, 1: 800.0, 2: 800.0, 3: 800.0},  # stays above 500
        {0: 800.0, 1: 450.0, 2: 800.0, 3: 800.0},  # dips
        {0: 600.0, 1: 600.0, 2: 600.0, 3: 600.0},  # stays above
        {0: 500.0, 1: 900.0, 2: 900.0, 3: 900.0},  # equals 500 once -> not above
    ]
    return ensemble("ghi", cols)


def test_irradiance_stays_above_over_window() -> None:
    fc = _ghi_window()
    f = irradiance_stays_above(
        fc, threshold_w_m2=500.0, start_utc=hour(0), end_utc=hour(3)  # type: ignore[arg-type]
    )
    assert f.frequency == 2 / 4
    assert f.members_meeting == 2
    assert f.members_counted == 4
    assert f.valid_times == 4
    assert "not a calibrated probability" in f.note


def test_window_excludes_members_with_gaps_and_says_so() -> None:
    cols = [
        {0: 800.0, 1: 800.0},
        {0: 800.0, 1: None},  # gap -> excluded
        {0: 100.0, 1: 800.0},
    ]
    f = member_frequency_throughout(
        ensemble("ghi", cols), variable="ghi", threshold=500.0, start_utc=hour(0), end_utc=hour(1)
    )
    assert (f.members_meeting, f.members_counted, f.members_total) == (1, 2, 3)
    assert f.frequency == 0.5
    assert "1 of 3 members were excluded" in f.note


def test_window_below_and_errors() -> None:
    fc = ensemble("precipitation", [{0: 0.0, 1: 0.0}, {0: 0.0, 1: 2.0}])
    f = member_frequency_throughout(
        fc, variable="precipitation", threshold=0.1, start_utc=hour(0), end_utc=hour(1),
        above=False,
    )
    assert f.frequency == 0.5
    with pytest.raises(EnsembleDataError):
        member_frequency_throughout(
            fc, variable="precipitation", threshold=0.1, start_utc=hour(5), end_utc=hour(6)
        )
    with pytest.raises(ValueError):
        member_frequency_throughout(
            fc, variable="precipitation", threshold=0.1, start_utc=hour(2), end_utc=hour(1)
        )


def test_irradiance_helper_rejects_non_irradiance() -> None:
    fc = at_one_time("temperature_2m", [20.0, 21.0])
    with pytest.raises(ValueError, match="irradiance"):
        irradiance_stays_above(
            fc, threshold_w_m2=1.0, start_utc=hour(0), end_utc=hour(1), variable="temperature_2m"
        )
    with pytest.raises(ValueError, match="irradiance"):
        irradiance_stays_above(
            fc, threshold_w_m2=1.0, start_utc=hour(0), end_utc=hour(1), variable="uv_index"
        )
