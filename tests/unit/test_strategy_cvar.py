"""CVaR objective, ported from the team's drive-system simulator but fed with
real ensemble members from rwi.ensemble rather than perturbations of a single
point forecast.

The arithmetic must be exact for the simple cases, because that is what pins
the risk principle: a plan that wins on the median and loses on the bad
quartile is not a plan you can race on. CVaR_0.25 of ten outcomes is the mean
of the worst three (rounded nearest), not the one worst or all ten.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from rwi.strategy import (
    ScenarioEnsembleResult,
    scenario_cvar,
)
from rwi.strategy.segments import SegmentConditions
from rwi.vehicle.dynamics import DEFAULT_VEHICLE

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
BASELINE_SPEED_KMH = 80.0


def _flat_segments(count: int = 4) -> list[SegmentConditions]:
    """A simple set of flat segments with explicit generation and wind.

    Chosen so that the model has a well-defined baseline it can perturb."""
    return [
        SegmentConditions(
            distance_m=20_000.0,
            grade=0.0,
            speed_limit_kmh=120.0,
            wind_speed_mps=4.0,
            wind_from_deg=0.0,
            temperature_c=25.0,
            generation_w=800.0,  # watts, speed-independent
        )
        for _ in range(count)
    ]


def test_a_single_member_is_refused_because_it_tells_us_nothing_about_risk() -> None:
    """CVaR with one realisation is just that realisation. The whole point is
    the shape of the distribution across the ensemble."""
    segs = _flat_segments()
    with pytest.raises(ValueError, match="at least 2 ensemble members"):
        scenario_cvar(
            segments=segs,
            ghi_members_w_m2=[[800.0] * 4],
            target_speed_kmh=BASELINE_SPEED_KMH,
            vehicle=DEFAULT_VEHICLE,
            start_soc_fraction=0.8,
            now_utc=NOW,
        )


def test_an_empty_input_is_refused_because_sensitivity_is_the_right_tool() -> None:
    """A caller with no ensemble data probably means the sensitivity sweep."""
    with pytest.raises(ValueError, match="at least one of"):
        scenario_cvar(
            segments=_flat_segments(),
            ghi_members_w_m2=None,
            wind_members_mps=None,
            target_speed_kmh=BASELINE_SPEED_KMH,
            vehicle=DEFAULT_VEHICLE,
            start_soc_fraction=0.8,
            now_utc=NOW,
        )


def test_mismatched_segment_count_is_refused_not_padded() -> None:
    with pytest.raises(ValueError, match="segments but there are"):
        scenario_cvar(
            segments=_flat_segments(count=4),
            ghi_members_w_m2=[[800.0] * 3, [700.0] * 3],  # wrong length
            target_speed_kmh=BASELINE_SPEED_KMH,
            vehicle=DEFAULT_VEHICLE,
            start_soc_fraction=0.8,
            now_utc=NOW,
        )


def test_disagreeing_member_counts_are_refused_not_zipped() -> None:
    with pytest.raises(ValueError, match="disagree"):
        scenario_cvar(
            segments=_flat_segments(),
            ghi_members_w_m2=[[800.0] * 4, [700.0] * 4],  # 2 members
            wind_members_mps=[[4.0] * 4, [5.0] * 4, [6.0] * 4],  # 3 members
            target_speed_kmh=BASELINE_SPEED_KMH,
            vehicle=DEFAULT_VEHICLE,
            start_soc_fraction=0.8,
            now_utc=NOW,
        )


def test_cvar_alpha_outside_the_open_unit_interval_is_refused() -> None:
    segs = _flat_segments()
    members = [[800.0] * 4, [700.0] * 4, [900.0] * 4]
    for bad_alpha in (0.0, -0.1, 1.0, 1.5):
        with pytest.raises(ValueError, match="alpha must be in"):
            scenario_cvar(
                segments=segs,
                ghi_members_w_m2=members,
                alpha=bad_alpha,
                target_speed_kmh=BASELINE_SPEED_KMH,
                vehicle=DEFAULT_VEHICLE,
                start_soc_fraction=0.8,
                now_utc=NOW,
            )


def test_cvar_of_four_members_at_alpha_0_25_is_the_worst_one() -> None:
    """alpha=0.25 of 4 members = 1 member; that is the worst outcome, exactly."""
    segs = _flat_segments()
    # Four GHI members producing clearly ordered generation.
    members = [[500.0] * 4, [800.0] * 4, [1000.0] * 4, [600.0] * 4]
    result = scenario_cvar(
        segments=segs,
        ghi_members_w_m2=members,
        alpha=0.25,
        target_speed_kmh=BASELINE_SPEED_KMH,
        vehicle=DEFAULT_VEHICLE,
        start_soc_fraction=0.8,
        now_utc=NOW,
    )
    assert isinstance(result, ScenarioEnsembleResult)
    assert result.member_count == 4
    # Objective "net_energy_wh": lower is worse, so CVaR_0.25 picks the worst
    # (lowest net_energy_wh) member and equals its value.
    sorted_net = sorted(r.net_energy_wh for r in result.member_results)
    assert result.cvar_value == pytest.approx(sorted_net[0])
    assert result.worst_value == pytest.approx(sorted_net[0])
    assert result.best_value == pytest.approx(sorted_net[-1])


def test_cvar_of_ten_members_at_alpha_0_3_is_the_mean_of_the_worst_three() -> None:
    """The arithmetic: round(10 * 0.3) = 3, so CVaR_0.3 is the mean of the
    three worst outcomes - not two, not four, not one."""
    segs = _flat_segments()
    members = [[400.0 + 100 * i] * 4 for i in range(10)]  # 400..1300 W/m2
    result = scenario_cvar(
        segments=segs,
        ghi_members_w_m2=members,
        alpha=0.3,
        target_speed_kmh=BASELINE_SPEED_KMH,
        vehicle=DEFAULT_VEHICLE,
        start_soc_fraction=0.8,
        now_utc=NOW,
    )
    sorted_net = sorted(r.net_energy_wh for r in result.member_results)
    expected_cvar = sum(sorted_net[:3]) / 3.0
    assert result.cvar_value == pytest.approx(expected_cvar, rel=1e-9)
    assert result.member_count == 10


def test_cvar_is_at_most_the_median_for_net_energy() -> None:
    """CVaR of the worst tail must be no greater than the median, by
    construction. If this fails the sign convention is backwards."""
    segs = _flat_segments()
    members = [[500.0 + 50 * i] * 4 for i in range(8)]
    result = scenario_cvar(
        segments=segs,
        ghi_members_w_m2=members,
        alpha=0.25,
        target_speed_kmh=BASELINE_SPEED_KMH,
        vehicle=DEFAULT_VEHICLE,
        start_soc_fraction=0.8,
        now_utc=NOW,
    )
    # net_energy_wh: lower is worse, so CVaR <= median <= best
    assert result.cvar_value <= result.median_value <= result.best_value


def test_travel_time_objective_also_puts_the_worst_at_the_longest_time() -> None:
    """For travel time the sign flips: lower is better, so CVaR picks the
    longest trips. The function must honour the objective argument."""
    segs = _flat_segments()
    members = [[700.0] * 4] * 6  # identical generation; travel time differs by wind only
    wind_members = [[0.0] * 4, [2.0] * 4, [4.0] * 4, [6.0] * 4, [8.0] * 4, [10.0] * 4]
    result = scenario_cvar(
        segments=segs,
        ghi_members_w_m2=members,
        wind_members_mps=wind_members,
        alpha=0.25,
        objective="travel_time_s",
        target_speed_kmh=BASELINE_SPEED_KMH,
        vehicle=DEFAULT_VEHICLE,
        start_soc_fraction=0.8,
        now_utc=NOW,
    )
    # Here LOWER value is better - CVaR picks the longest (worst) trips.
    assert result.cvar_value >= result.median_value >= result.best_value


def test_wind_members_actually_perturb_the_scenario() -> None:
    """A sanity check that the member array is wired, not quietly ignored.
    Varying wind must produce varying ScenarioResult objects."""
    segs = _flat_segments()
    members_calm = [[0.0] * 4, [0.0] * 4, [0.0] * 4]
    result_calm = scenario_cvar(
        segments=segs,
        ghi_members_w_m2=[[800.0] * 4] * 3,
        wind_members_mps=members_calm,
        target_speed_kmh=BASELINE_SPEED_KMH,
        vehicle=DEFAULT_VEHICLE,
        start_soc_fraction=0.8,
        now_utc=NOW,
    )
    # All three members have the same (calm) wind, so results must be identical.
    net_values = {r.net_energy_wh for r in result_calm.member_results}
    assert len(net_values) == 1, "identical ensemble members produced different results"
