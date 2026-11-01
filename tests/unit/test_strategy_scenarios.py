"""rwi.strategy.scenarios: constraints, candidates, recommendation, determinism.

All inputs are constructed here: segments, trajectories and generation series.
Nothing imports rwi.route / rwi.solar / rwi.energy and nothing uses the network
or the wall clock (every call passes an explicit ``now_utc``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rwi.contracts import (
    Provenance,
    RouteWaypoint,
    Trajectory,
    TrajectoryPoint,
    VehicleConfig,
    WaypointKind,
)
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind
from rwi.regulations import EventClass
from rwi.strategy.scenarios import (
    SAFETY_NOTES,
    compare_scenarios,
    evaluate_scenario,
    recommend,
    scenario_sensitivity,
    standard_candidates,
    weather_aware_profile,
)
from rwi.strategy.segments import SegmentConditions, segments_from_distance
from rwi.vehicle.dynamics import CHALLENGER_MAX_STORAGE_WH

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
START = datetime(2027, 8, 24, 22, 30, tzinfo=UTC)  # 08:00 ACST on a race day

VEHICLE = VehicleConfig(
    name="scenario-test-vehicle (assumed)",
    mass_kg=300.0,
    drag_area_cda_m2=0.10,
    rolling_resistance_coefficient=0.005,
    drivetrain_efficiency=0.92,
    auxiliary_power_w=25.0,
    battery_capacity_wh=CHALLENGER_MAX_STORAGE_WH,
    battery_reserve_fraction=0.10,
    battery_round_trip_efficiency=0.95,
    max_legal_speed_kmh=130.0,
    parameter_source="assumed",
)


def flat_leg(
    distance_m: float = 300_000.0, *, generation_w: float | None = 600.0, segments: int = 12
) -> list[SegmentConditions]:
    kwargs = {"generation_w": generation_w} if generation_w is not None else {}
    return segments_from_distance(distance_m, segment_count=segments, **kwargs)


def run(**kwargs):
    base = {
        "name": "candidate",
        "description": "test candidate",
        "vehicle": VEHICLE,
        "start_soc_fraction": 1.0,
        "now_utc": NOW,
    }
    base.update(kwargs)
    return evaluate_scenario(**base)


def blocking(result) -> set[str]:
    return {v.constraint for v in result.violations if v.severity == "blocking"}


def warnings(result) -> set[str]:
    return {v.constraint for v in result.violations if v.severity == "warning"}


# ---------------------------------------------------------------------------
# Energy feasibility
# ---------------------------------------------------------------------------


def test_scenario_that_cannot_finish_on_its_energy_is_infeasible():
    result = run(
        target_speed_kmh=90.0,
        segments=flat_leg(1_200_000.0, generation_w=None, segments=24),
    )
    assert result.feasible is False
    assert "battery_reserve" in blocking(result)
    assert "battery_empty" in blocking(result)
    assert result.end_soc_fraction == 0.0
    assert float(result.assumptions["energy_shortfall_wh"]) > 0.0
    assert result.energy_consumed_wh > VEHICLE.battery_capacity_wh
    assert result.energy_generated_wh == 0.0


def test_a_leg_within_the_energy_budget_is_feasible_and_balances():
    result = run(target_speed_kmh=80.0, segments=flat_leg())
    assert result.feasible is True
    assert result.violations == []
    assert result.net_energy_wh == pytest.approx(
        result.energy_generated_wh - result.energy_consumed_wh
    )
    assert 0.0 < result.end_soc_fraction < result.start_soc_fraction
    assert result.distance_m == pytest.approx(300_000.0)
    assert result.travel_time_s == pytest.approx(300.0 / 80.0 * 3600.0)


def test_slower_costs_less_energy_and_more_time():
    slow = run(target_speed_kmh=70.0, segments=flat_leg())
    fast = run(target_speed_kmh=85.0, segments=flat_leg())
    assert slow.energy_consumed_wh < fast.energy_consumed_wh
    assert slow.travel_time_s > fast.travel_time_s
    assert slow.end_soc_fraction > fast.end_soc_fraction


# ---------------------------------------------------------------------------
# Legal speed: blocking, and never recommended
# ---------------------------------------------------------------------------


def test_speed_above_the_vehicle_legal_ceiling_is_a_blocking_violation():
    result = run(target_speed_kmh=150.0, segments=flat_leg())
    assert result.feasible is False
    assert "legal_speed" in blocking(result)
    assert "never recommends breaking a speed limit" in result.violations[0].detail


def test_speed_above_a_supplied_speed_limit_is_a_blocking_violation():
    result = run(target_speed_kmh=120.0, segments=flat_leg(), speed_limit_kmh=110.0)
    assert result.feasible is False
    assert "speed_limit" in blocking(result)


def test_a_per_segment_speed_limit_is_enforced():
    segments = [
        SegmentConditions(distance_m=100_000.0, generation_w=600.0),
        SegmentConditions(distance_m=100_000.0, generation_w=600.0, speed_limit_kmh=60.0),
    ]
    result = run(target_speed_kmh=90.0, segments=segments)
    assert result.feasible is False
    assert "speed_limit" in blocking(result)
    assert "segment 1" in next(
        v.detail for v in result.violations if v.constraint == "speed_limit"
    )


def test_recommend_never_picks_an_illegal_scenario_even_when_it_is_fastest():
    legal = run(name="legal", target_speed_kmh=80.0, segments=flat_leg())
    illegal = run(name="illegal-and-faster", target_speed_kmh=150.0, segments=flat_leg())
    assert illegal.travel_time_s < legal.travel_time_s  # it really is faster
    assert illegal.feasible is False

    recommendation = recommend([illegal, legal], baseline_name="legal", now_utc=NOW)
    assert recommendation.recommended_scenario == "legal"
    assert "illegal-and-faster" in recommendation.alternatives
    assert "excluded as infeasible" in recommendation.rationale


# ---------------------------------------------------------------------------
# Deadline and power limit
# ---------------------------------------------------------------------------


def test_arriving_after_the_deadline_is_a_warning_not_a_blocker():
    result = run(
        target_speed_kmh=70.0,
        segments=flat_leg(),
        start_time_utc=START,
        deadline_utc=START + timedelta(hours=3),
    )
    assert result.feasible is True
    assert warnings(result) == {"arrival_deadline"}
    assert result.arrival_utc == START + timedelta(seconds=result.travel_time_s)


def test_arriving_before_the_deadline_raises_nothing():
    result = run(
        target_speed_kmh=85.0,
        segments=flat_leg(),
        start_time_utc=START,
        deadline_utc=START + timedelta(hours=6),
    )
    assert result.violations == []
    assert result.arrival_utc is not None


def test_a_deadline_without_a_start_time_is_reported_as_unchecked():
    result = run(
        target_speed_kmh=80.0, segments=flat_leg(), deadline_utc=START + timedelta(hours=1)
    )
    assert result.arrival_utc is None
    assert "arrival_deadline" in warnings(result)
    assert "NOT checked" in next(
        v.detail for v in result.violations if v.constraint == "arrival_deadline"
    )


def test_a_power_limit_blocks_by_default_and_can_be_downgraded_to_a_warning():
    # 80 km/h fits the energy budget, so the power limit is the only problem.
    blocked = run(target_speed_kmh=80.0, segments=flat_leg(), power_limit_w=500.0)
    assert blocked.feasible is False
    assert "power_limit" in blocking(blocked)

    warned = run(
        target_speed_kmh=80.0,
        segments=flat_leg(),
        power_limit_w=500.0,
        power_limit_severity="warning",
    )
    assert warned.feasible is True
    assert "power_limit" in warnings(warned)


def test_a_generous_power_limit_is_not_violated():
    result = run(target_speed_kmh=80.0, segments=flat_leg(), power_limit_w=5000.0)
    assert result.violations == []


# ---------------------------------------------------------------------------
# Wind and gradient reach the power model through the segments
# ---------------------------------------------------------------------------


def test_a_headwind_segment_costs_more_than_a_tailwind_segment():
    head = [
        SegmentConditions(
            distance_m=200_000.0, heading_deg=0.0, wind_speed_mps=8.0, wind_from_deg=0.0
        )
    ]
    tail = [
        SegmentConditions(
            distance_m=200_000.0, heading_deg=0.0, wind_speed_mps=8.0, wind_from_deg=180.0
        )
    ]
    into = run(target_speed_kmh=80.0, segments=head)
    with_it = run(target_speed_kmh=80.0, segments=tail)
    assert into.energy_consumed_wh > with_it.energy_consumed_wh
    assert into.travel_time_s == pytest.approx(with_it.travel_time_s)


def test_a_climb_costs_more_than_the_same_road_flat():
    climb = run(
        target_speed_kmh=80.0,
        segments=[SegmentConditions(distance_m=100_000.0, grade=0.01)],
    )
    flat = run(
        target_speed_kmh=80.0, segments=[SegmentConditions(distance_m=100_000.0, grade=0.0)]
    )
    assert climb.energy_consumed_wh > flat.energy_consumed_wh


# ---------------------------------------------------------------------------
# Generation conventions
# ---------------------------------------------------------------------------


def test_generation_in_watts_scales_with_the_time_spent_in_the_segment():
    slow = run(target_speed_kmh=60.0, segments=flat_leg())
    fast = run(target_speed_kmh=85.0, segments=flat_leg())
    assert slow.energy_generated_wh > fast.energy_generated_wh
    assert slow.assumptions["generation_attribution"].startswith("per-segment mean array power")


def test_generation_in_watt_hours_is_fixed_regardless_of_speed():
    segments = [SegmentConditions(distance_m=100_000.0, generation_wh=1200.0) for _ in range(2)]
    slow = run(target_speed_kmh=60.0, segments=segments)
    fast = run(target_speed_kmh=100.0, segments=segments)
    assert slow.energy_generated_wh == pytest.approx(2400.0)
    assert fast.energy_generated_wh == pytest.approx(2400.0)


def test_a_generation_series_can_be_attached_at_evaluation_time():
    result = run(
        target_speed_kmh=80.0,
        segments=flat_leg(generation_w=None),
        generation=[500.0] * 12,
    )
    assert result.energy_generated_wh > 0.0


def test_mixing_the_two_generation_conventions_is_rejected():
    with pytest.raises(ValueError, match="not both"):
        SegmentConditions(distance_m=1000.0, generation_w=500.0, generation_wh=10.0)


# ---------------------------------------------------------------------------
# Control-stop dwell
# ---------------------------------------------------------------------------


def test_dwell_adds_time_auxiliary_draw_and_generation():
    moving = [SegmentConditions(distance_m=100_000.0, generation_w=600.0)]
    with_stop = [SegmentConditions(distance_m=100_000.0, generation_w=600.0, dwell_s=1800.0)]
    a = run(target_speed_kmh=80.0, segments=moving)
    b = run(target_speed_kmh=80.0, segments=with_stop)
    assert b.travel_time_s == pytest.approx(a.travel_time_s + 1800.0)
    assert b.energy_consumed_wh == pytest.approx(
        a.energy_consumed_wh + VEHICLE.auxiliary_power_w * 0.5
    )
    assert b.energy_generated_wh == pytest.approx(a.energy_generated_wh + 600.0 * 0.5)
    assert float(b.assumptions["dwell_s_total"]) == 1800.0


# ---------------------------------------------------------------------------
# Trajectory input
# ---------------------------------------------------------------------------


def _trajectory() -> Trajectory:
    provenance = Provenance(
        provider="test",
        dataset="fixture",
        kind=DataKind.SIMULATION,
        retrieved_at_utc=NOW,
    )
    points = []
    for i in range(5):
        waypoint = (
            RouteWaypoint(
                point=GeoPoint(latitude_deg=-14.0 - i, longitude_deg=132.0),
                cumulative_distance_m=float(i) * 50_000.0,
                kind=WaypointKind.CONTROL_STOP,
                name="Katherine",
                mandatory_dwell_s=1800.0,
            )
            if i == 2
            else None
        )
        points.append(
            TrajectoryPoint(
                distance_m=float(i) * 50_000.0,
                point=GeoPoint(latitude_deg=-14.0 - i, longitude_deg=132.0),
                eta_utc=START + timedelta(hours=i),
                heading_deg=180.0,
                speed_mps=20.0,  # deliberately different from the scenario speed
                grade=0.002,
                waypoint=waypoint,
            )
        )
    return Trajectory(
        route_id="test-route", generated_at_utc=NOW, points=points, provenance=provenance
    )


def test_a_trajectory_supplies_geometry_and_dwell_but_not_the_speed():
    trajectory = _trajectory()
    result = run(target_speed_kmh=90.0, trajectory=trajectory, generation=[700.0] * 5)
    assert result.distance_m == pytest.approx(200_000.0)
    assert float(result.assumptions["segment_count"]) == 4.0
    assert float(result.assumptions["dwell_s_total"]) == 1800.0
    # Not the trajectory's own 20 m/s = 72 km/h.
    assert result.travel_time_s == pytest.approx(200.0 / 90.0 * 3600.0 + 1800.0)


def test_supplying_two_leg_descriptions_at_once_is_an_error():
    with pytest.raises(ValueError, match="exactly one of"):
        run(target_speed_kmh=80.0, segments=flat_leg(), distance_m=1000.0)
    with pytest.raises(ValueError, match="exactly one of"):
        evaluate_scenario(
            name="x",
            description="x",
            target_speed_kmh=80.0,
            vehicle=VEHICLE,
            start_soc_fraction=1.0,
            now_utc=NOW,
        )


# ---------------------------------------------------------------------------
# Candidate sets
# ---------------------------------------------------------------------------


def test_standard_candidates_are_the_four_from_the_brief_and_respect_the_cap():
    segments = flat_leg()
    specs = standard_candidates(
        vehicle=VEHICLE, segments=segments, baseline_speed_kmh=85.0, speed_limit_kmh=90.0
    )
    assert [s.name for s in specs] == [
        "conservative",
        "balanced",
        "faster-within-limits",
        "weather-aware adaptive",
    ]
    by_name = {s.name: s for s in specs}
    assert by_name["conservative"].target_speed_kmh == pytest.approx(75.0)
    assert by_name["balanced"].target_speed_kmh == pytest.approx(85.0)
    # 85 + 10 = 95, capped at the 90 km/h limit.
    assert by_name["faster-within-limits"].target_speed_kmh == pytest.approx(90.0)
    assert by_name["weather-aware adaptive"].speed_profile is not None
    assert by_name["weather-aware adaptive"].assumptions["heuristic_not_optimum"] is True


def test_compare_scenarios_evaluates_every_candidate_in_order():
    segments = flat_leg()
    specs = standard_candidates(vehicle=VEHICLE, segments=segments, baseline_speed_kmh=80.0)
    results = compare_scenarios(
        specs, vehicle=VEHICLE, start_soc_fraction=1.0, segments=segments, now_utc=NOW
    )
    assert [r.name for r in results] == [s.name for s in specs]
    assert all(r.distance_m == pytest.approx(300_000.0) for r in results)
    # The candidate roles survive into the result assumptions.
    assert results[0].assumptions["candidate_role"] == "energy margin first"


def test_compare_scenarios_requires_candidates():
    with pytest.raises(ValueError, match="must not be empty"):
        compare_scenarios(
            [], vehicle=VEHICLE, start_soc_fraction=1.0, segments=flat_leg(), now_utc=NOW
        )


# ---------------------------------------------------------------------------
# The weather-aware heuristic actually varies speed
# ---------------------------------------------------------------------------


def test_weather_aware_profile_slows_into_a_headwind_and_speeds_up_with_a_tailwind():
    segments = [
        SegmentConditions(
            distance_m=100_000.0, heading_deg=0.0, wind_speed_mps=10.0, wind_from_deg=0.0
        ),
        SegmentConditions(
            distance_m=100_000.0, heading_deg=0.0, wind_speed_mps=10.0, wind_from_deg=180.0
        ),
    ]
    profile = weather_aware_profile(segments, max_speed_kmh=130.0)
    into = profile(0, segments[0], 80.0)
    with_it = profile(1, segments[1], 80.0)
    assert into < 80.0 < with_it
    # 0.5 * 10 m/s = 18 km/h of the along-track wind is given up / taken.
    assert into == pytest.approx(80.0 - 0.5 * 36.0)
    assert with_it == pytest.approx(80.0 + 0.5 * 36.0)

    result = run(target_speed_kmh=80.0, segments=segments, speed_profile=profile)
    assert float(result.assumptions["min_speed_kmh"]) < 80.0
    assert float(result.assumptions["max_speed_kmh"]) > 80.0
    assert result.assumptions["speed_rule"] == "per-segment profile"


def test_weather_aware_profile_speeds_up_where_generation_is_high():
    segments = [
        SegmentConditions(distance_m=100_000.0, generation_w=300.0),
        SegmentConditions(distance_m=100_000.0, generation_w=900.0),
    ]
    profile = weather_aware_profile(segments, max_speed_kmh=130.0)
    dull = profile(0, segments[0], 80.0)
    bright = profile(1, segments[1], 80.0)
    assert dull < 80.0 < bright
    # Reference is the 600 W mean: 0.2 * (300/600 - 1) = -10 %, and +10 %.
    assert dull == pytest.approx(80.0 * 0.9)
    assert bright == pytest.approx(80.0 * 1.1)


def test_weather_aware_profile_never_proposes_a_speed_above_the_cap():
    segments = [
        SegmentConditions(
            distance_m=100_000.0, heading_deg=0.0, wind_speed_mps=30.0, wind_from_deg=180.0
        )
    ]
    profile = weather_aware_profile(segments, max_speed_kmh=100.0, min_speed_kmh=50.0)
    assert profile(0, segments[0], 95.0) == pytest.approx(100.0)
    result = run(target_speed_kmh=95.0, segments=segments, speed_profile=profile)
    assert "speed_limit" not in blocking(result)


def test_weather_aware_profile_is_a_pure_function_of_its_inputs():
    segments = flat_leg()
    first = weather_aware_profile(segments, max_speed_kmh=130.0)
    second = weather_aware_profile(segments, max_speed_kmh=130.0)
    assert [first(i, s, 80.0) for i, s in enumerate(segments)] == [
        second(i, s, 80.0) for i, s in enumerate(segments)
    ]


# ---------------------------------------------------------------------------
# Uncertainty sweep
# ---------------------------------------------------------------------------


def test_uncertainty_sweep_brackets_the_base_case():
    result = run(target_speed_kmh=85.0, segments=flat_leg(), uncertainty_fraction=0.25)
    pessimistic = float(result.assumptions["pessimistic_end_soc_fraction"])
    optimistic = float(result.assumptions["optimistic_end_soc_fraction"])
    assert pessimistic <= result.end_soc_fraction <= optimistic
    assert result.assumptions["case"] == "base"
    assert any("sensitivity" in note.lower() for note in result.uncertainty_notes)
    assert any("NOT a probability distribution" in note for note in result.uncertainty_notes)


def test_without_a_sweep_the_result_says_robustness_is_unknown():
    result = run(target_speed_kmh=85.0, segments=flat_leg())
    assert "pessimistic_end_soc_fraction" not in result.assumptions
    assert any("No uncertainty sweep was run" in note for note in result.uncertainty_notes)


def test_scenario_sensitivity_returns_the_three_cases():
    sweep = scenario_sensitivity(
        uncertainty_fraction=0.2,
        segments=flat_leg(),
        name="s",
        description="d",
        target_speed_kmh=85.0,
        vehicle=VEHICLE,
        start_soc_fraction=1.0,
        now_utc=NOW,
    )
    assert sweep.base.assumptions["case"] == "base"
    assert sweep.pessimistic.assumptions["case"] == "pessimistic"
    assert sweep.optimistic.assumptions["case"] == "optimistic"
    low, high = sweep.end_soc_span
    assert low <= sweep.base.end_soc_fraction <= high
    assert sweep.robust is (sweep.base.feasible and sweep.pessimistic.feasible)
    assert "NOT a probability distribution" in sweep.method_note


def test_scenario_sensitivity_needs_explicit_segments():
    with pytest.raises(ValueError, match="explicit segments"):
        scenario_sensitivity(
            uncertainty_fraction=0.2,
            name="s",
            description="d",
            target_speed_kmh=85.0,
            vehicle=VEHICLE,
            start_soc_fraction=1.0,
            distance_m=100_000.0,
            now_utc=NOW,
        )


# ---------------------------------------------------------------------------
# recommend()
# ---------------------------------------------------------------------------


def _comparison(**kwargs):
    segments = flat_leg()
    specs = standard_candidates(vehicle=VEHICLE, segments=segments, baseline_speed_kmh=80.0)
    return compare_scenarios(
        specs,
        vehicle=VEHICLE,
        start_soc_fraction=1.0,
        segments=segments,
        start_time_utc=START,
        uncertainty_fraction=0.2,
        now_utc=NOW,
        **kwargs,
    )


def test_recommend_picks_the_fastest_feasible_and_names_its_numbers():
    results = _comparison()
    recommendation = recommend(
        results, baseline_name="balanced", now_utc=NOW, reevaluate_within_s=3600.0
    )
    chosen = next(r for r in results if r.name == recommendation.recommended_scenario)
    assert chosen.feasible
    assert chosen.travel_time_s == min(r.travel_time_s for r in results if r.feasible)

    baseline = next(r for r in results if r.name == "balanced")
    assert recommendation.time_impact_s == pytest.approx(
        chosen.travel_time_s - baseline.travel_time_s
    )
    assert recommendation.energy_impact_wh == pytest.approx(
        chosen.net_energy_wh - baseline.net_energy_wh
    )
    # The rationale must contain the numbers it is based on.
    assert f"{chosen.target_speed_kmh:.1f} km/h" in recommendation.rationale
    assert f"{chosen.energy_consumed_wh:.0f} Wh" in recommendation.rationale
    assert "state of charge" in recommendation.rationale
    assert recommendation.reevaluate_after_utc == NOW + timedelta(hours=1)
    assert recommendation.alternatives and recommendation.recommended_scenario not in (
        recommendation.alternatives
    )
    assert recommendation.evidence
    assert list(recommendation.safety_and_regulatory_notes) == list(SAFETY_NOTES)


def test_recommend_can_optimise_for_the_finishing_state_of_charge_instead():
    results = _comparison()
    recommendation = recommend(results, objective="max_end_soc", now_utc=NOW)
    chosen = next(r for r in results if r.name == recommendation.recommended_scenario)
    assert chosen.end_soc_fraction == max(r.end_soc_fraction for r in results if r.feasible)
    assert "highest finishing state of charge" in recommendation.rationale


def test_recommend_confidence_is_never_high():
    results = _comparison()
    recommendation = recommend(results, now_utc=NOW)
    assert recommendation.confidence in {"low", "medium"}
    assert any("capped at 'medium'" in note for note in recommendation.uncertainty_notes)


def test_recommend_confidence_is_low_without_a_sensitivity_sweep():
    segments = flat_leg()
    results = compare_scenarios(
        standard_candidates(vehicle=VEHICLE, segments=segments, baseline_speed_kmh=80.0),
        vehicle=VEHICLE,
        start_soc_fraction=1.0,
        segments=segments,
        now_utc=NOW,
    )
    assert recommend(results, now_utc=NOW).confidence == "low"


def test_recommend_says_so_when_nothing_is_feasible():
    illegal = run(name="too-fast", target_speed_kmh=150.0, segments=flat_leg())
    broke = run(
        name="out-of-energy",
        target_speed_kmh=90.0,
        segments=flat_leg(1_200_000.0, generation_w=None, segments=24),
    )
    assert not illegal.feasible and not broke.feasible

    recommendation = recommend([illegal, broke], now_utc=NOW)
    assert recommendation.recommended_scenario is None
    assert "No feasible scenario" in recommendation.headline
    assert "nothing to recommend" in recommendation.rationale
    assert "no least-bad option" in recommendation.rationale
    assert "legal_speed" in recommendation.rationale
    assert "battery_reserve" in recommendation.rationale
    assert recommendation.confidence == "low"
    assert recommendation.energy_impact_wh is None
    assert recommendation.time_impact_s is None
    assert set(recommendation.alternatives) == {"too-fast", "out-of-energy"}


def test_recommend_states_the_safety_and_regulatory_position():
    notes = " ".join(recommend(_comparison(), now_utc=NOW).safety_and_regulatory_notes)
    assert "NO vehicle commands" in notes
    assert "BLOCKING" in notes
    assert "human review" in notes
    # The regulations are cited, not asserted, and the open questions stay open.
    assert "rwi.regulations" in notes
    assert "regulation 2.5.2" in notes
    assert "regulation 3.31.6" in notes
    assert "19:00-06:30" in notes and "19:15/06:15" in notes
    assert "organisers must be asked" in notes
    assert "TODO(regulations)" not in notes


def test_recommend_requires_results():
    with pytest.raises(ValueError, match="must not be empty"):
        recommend([], now_utc=NOW)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_the_same_inputs_give_exactly_the_same_scenario_result():
    def once():
        return run(
            target_speed_kmh=84.0,
            segments=[
                SegmentConditions(
                    distance_m=75_000.0,
                    grade=0.004,
                    heading_deg=170.0,
                    wind_speed_mps=7.0,
                    wind_from_deg=20.0,
                    generation_w=640.0,
                    dwell_s=600.0,
                ),
                SegmentConditions(
                    distance_m=125_000.0,
                    grade=-0.003,
                    heading_deg=190.0,
                    wind_speed_mps=4.0,
                    wind_from_deg=260.0,
                    generation_w=520.0,
                ),
            ],
            start_time_utc=START,
            deadline_utc=START + timedelta(hours=2),
            uncertainty_fraction=0.15,
        )

    first, second = once(), once()
    assert first.model_dump() == second.model_dump()
    assert first.provenance.retrieved_at_utc == NOW


def test_determinism_holds_for_a_whole_comparison_and_recommendation():
    def once():
        results = _comparison()
        return [r.model_dump() for r in results], recommend(
            results, baseline_name="balanced", now_utc=NOW, reevaluate_within_s=1800.0
        ).model_dump()

    assert once() == once()
