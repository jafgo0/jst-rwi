"""rwi.strategy.scenarios.optimal_constant_speed, benchmarked against brute force.

The brute-force scan in this file is written independently of the optimiser: it
only calls :func:`evaluate_scenario` and reads ``feasible``. If the two
disagree by more than the scan step, one of them is wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from rwi.contracts import VehicleConfig
from rwi.strategy.scenarios import evaluate_scenario, optimal_constant_speed
from rwi.strategy.segments import SegmentConditions, segments_from_distance

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

VEHICLE = VehicleConfig(
    name="optimiser-test-vehicle (assumed)",
    mass_kg=300.0,
    drag_area_cda_m2=0.10,
    rolling_resistance_coefficient=0.005,
    drivetrain_efficiency=0.92,
    auxiliary_power_w=25.0,
    battery_capacity_wh=5000.0,
    battery_reserve_fraction=0.10,
    battery_round_trip_efficiency=0.95,
    max_legal_speed_kmh=130.0,
    parameter_source="assumed",
)

SCAN_STEP_KMH = 0.25


def leg(distance_m: float = 400_000.0, *, generation_w: float = 700.0, segments: int = 16):
    return segments_from_distance(
        distance_m, segment_count=segments, generation_w=generation_w
    )


def feasible_at(speed_kmh: float, segments, *, vehicle=VEHICLE, start_soc_fraction=1.0) -> bool:
    """Independent feasibility probe: one scenario, one boolean."""
    return evaluate_scenario(
        name="probe",
        description=f"{speed_kmh} km/h",
        target_speed_kmh=speed_kmh,
        vehicle=vehicle,
        start_soc_fraction=start_soc_fraction,
        segments=segments,
        now_utc=NOW,
    ).feasible


def brute_force_best(
    segments,
    *,
    vehicle=VEHICLE,
    start_soc_fraction=1.0,
    low_kmh: float = 30.0,
    high_kmh: float | None = None,
    step_kmh: float = SCAN_STEP_KMH,
) -> float | None:
    """Highest speed on a fixed grid that still completes the leg, or None."""
    cap = high_kmh if high_kmh is not None else vehicle.max_legal_speed_kmh
    best: float | None = None
    speed = low_kmh
    while speed <= cap + 1e-9:
        if feasible_at(
            speed, segments, vehicle=vehicle, start_soc_fraction=start_soc_fraction
        ):
            best = speed
        speed += step_kmh
    return best


# ---------------------------------------------------------------------------
# Agreement with brute force
# ---------------------------------------------------------------------------


@pytest.mark.science
def test_optimiser_agrees_with_a_brute_force_scan_to_within_the_step_size():
    segments = leg()
    result = optimal_constant_speed(
        vehicle=VEHICLE, start_soc_fraction=1.0, segments=segments, now_utc=NOW
    )
    scanned = brute_force_best(segments)

    assert result.feasible is True
    assert scanned is not None
    assert result.speed_kmh is not None
    # The optimum is the exact boundary; the scan can only land on a grid point
    # at or below it, never above it.
    assert result.speed_kmh >= scanned - 1e-9
    assert result.speed_kmh - scanned <= SCAN_STEP_KMH + 1e-9
    # The energy constraint is what binds here, not the legal cap.
    assert result.speed_kmh < result.speed_cap_kmh
    assert "brentq" in result.method


def test_the_optimum_is_feasible_and_one_step_above_it_is_not():
    segments = leg()
    result = optimal_constant_speed(
        vehicle=VEHICLE, start_soc_fraction=1.0, segments=segments, now_utc=NOW
    )
    assert result.speed_kmh is not None
    assert feasible_at(result.speed_kmh, segments) is True
    assert feasible_at(result.speed_kmh + 1.0, segments) is False
    assert result.scenario is not None
    assert result.scenario.feasible is True
    assert result.travel_time_s == pytest.approx(result.scenario.travel_time_s)


def test_the_optimum_finishes_on_the_reserve_and_says_it_has_no_margin():
    segments = leg()
    result = optimal_constant_speed(
        vehicle=VEHICLE, start_soc_fraction=1.0, segments=segments, now_utc=NOW
    )
    assert result.end_soc_fraction == pytest.approx(VEHICLE.battery_reserve_fraction, abs=1e-3)
    assert any("NO energy margin" in note for note in result.notes)
    assert any("no dynamic programming" in note.lower() for note in result.notes)


def test_a_shorter_leg_allows_a_higher_optimal_speed():
    short = optimal_constant_speed(
        vehicle=VEHICLE, start_soc_fraction=1.0, segments=leg(300_000.0), now_utc=NOW
    )
    long = optimal_constant_speed(
        vehicle=VEHICLE, start_soc_fraction=1.0, segments=leg(500_000.0), now_utc=NOW
    )
    assert short.speed_kmh is not None and long.speed_kmh is not None
    assert short.speed_kmh > long.speed_kmh
    assert short.travel_time_s is not None and long.travel_time_s is not None


# ---------------------------------------------------------------------------
# The legal cap is respected
# ---------------------------------------------------------------------------


def test_the_optimiser_never_exceeds_the_vehicle_legal_ceiling():
    # Plenty of sun and a short leg: the energy constraint cannot bind.
    segments = leg(80_000.0, generation_w=3000.0, segments=4)
    result = optimal_constant_speed(
        vehicle=VEHICLE, start_soc_fraction=1.0, segments=segments, now_utc=NOW
    )
    assert result.feasible is True
    assert result.speed_kmh == pytest.approx(VEHICLE.max_legal_speed_kmh)
    assert result.speed_cap_kmh == pytest.approx(VEHICLE.max_legal_speed_kmh)
    assert "saturated at the legal speed cap" in result.method
    assert any("never recommends" in note for note in result.notes)


def test_a_supplied_speed_limit_caps_the_optimum():
    segments = leg(80_000.0, generation_w=3000.0, segments=4)
    result = optimal_constant_speed(
        vehicle=VEHICLE,
        start_soc_fraction=1.0,
        segments=segments,
        speed_limit_kmh=90.0,
        now_utc=NOW,
    )
    assert result.speed_cap_kmh == pytest.approx(90.0)
    assert result.speed_kmh == pytest.approx(90.0)


def test_a_per_segment_speed_limit_caps_the_optimum():
    segments = [
        SegmentConditions(distance_m=40_000.0, generation_w=3000.0),
        SegmentConditions(distance_m=40_000.0, generation_w=3000.0, speed_limit_kmh=60.0),
    ]
    result = optimal_constant_speed(
        vehicle=VEHICLE, start_soc_fraction=1.0, segments=segments, now_utc=NOW
    )
    assert result.speed_cap_kmh == pytest.approx(60.0)
    assert result.speed_kmh == pytest.approx(60.0)


def test_the_capped_optimum_matches_the_brute_force_scan_under_the_same_cap():
    segments = leg(80_000.0, generation_w=3000.0, segments=4)
    result = optimal_constant_speed(
        vehicle=VEHICLE,
        start_soc_fraction=1.0,
        segments=segments,
        speed_limit_kmh=95.0,
        now_utc=NOW,
    )
    scanned = brute_force_best(segments, high_kmh=95.0)
    assert scanned is not None
    assert result.speed_kmh is not None
    assert result.speed_kmh - scanned <= SCAN_STEP_KMH + 1e-9


# ---------------------------------------------------------------------------
# Infeasibility is reported, not papered over
# ---------------------------------------------------------------------------


def test_infeasible_at_every_speed_is_reported_as_such():
    segments = segments_from_distance(2_000_000.0, segment_count=20)  # no generation at all
    result = optimal_constant_speed(
        vehicle=VEHICLE,
        start_soc_fraction=1.0,
        segments=segments,
        min_speed_kmh=30.0,
        now_utc=NOW,
    )
    assert result.feasible is False
    assert result.speed_kmh is None
    assert brute_force_best(segments) is None
    assert "infeasible at every speed" in result.method
    assert any("below the reserve" in note for note in result.notes)
    assert result.scenario is not None and result.scenario.feasible is False


def test_an_empty_speed_window_is_reported_rather_than_searched():
    result = optimal_constant_speed(
        vehicle=VEHICLE,
        start_soc_fraction=1.0,
        segments=leg(),
        min_speed_kmh=200.0,
        now_utc=NOW,
    )
    assert result.feasible is False
    assert result.speed_kmh is None
    assert result.function_evaluations == 0
    assert "speed window is empty" in result.method


def test_starting_on_the_reserve_with_no_generation_is_infeasible():
    """A pack already at the reserve cannot move at all without generation.

    With generation it can: at a low enough speed the array out-earns the
    demand, so the same start state is feasible. Both cases are asserted, since
    the difference is exactly the point of the energy constraint.
    """
    dark = segments_from_distance(400_000.0, segment_count=16)
    at_reserve = optimal_constant_speed(
        vehicle=VEHICLE, start_soc_fraction=0.10, segments=dark, now_utc=NOW
    )
    assert at_reserve.feasible is False
    assert brute_force_best(dark, start_soc_fraction=0.10) is None

    full = optimal_constant_speed(
        vehicle=VEHICLE, start_soc_fraction=1.0, segments=dark, now_utc=NOW
    )
    assert full.feasible is True

    sunny = optimal_constant_speed(
        vehicle=VEHICLE, start_soc_fraction=0.10, segments=leg(), now_utc=NOW
    )
    assert sunny.feasible is True
    assert sunny.speed_kmh is not None and full.speed_kmh is not None


# ---------------------------------------------------------------------------
# Determinism and bookkeeping
# ---------------------------------------------------------------------------


def test_the_optimiser_is_deterministic():
    segments = leg()
    first = optimal_constant_speed(
        vehicle=VEHICLE, start_soc_fraction=1.0, segments=segments, now_utc=NOW
    )
    second = optimal_constant_speed(
        vehicle=VEHICLE, start_soc_fraction=1.0, segments=segments, now_utc=NOW
    )
    assert first.model_dump() == second.model_dump()


def test_the_optimiser_is_cheap_compared_with_a_scan():
    segments = leg()
    result = optimal_constant_speed(
        vehicle=VEHICLE, start_soc_fraction=1.0, segments=segments, now_utc=NOW
    )
    scan_evaluations = int((VEHICLE.max_legal_speed_kmh - 30.0) / SCAN_STEP_KMH)
    assert 0 < result.function_evaluations < scan_evaluations


def test_a_tighter_tolerance_does_not_move_the_answer_much():
    segments = leg()
    coarse = optimal_constant_speed(
        vehicle=VEHICLE,
        start_soc_fraction=1.0,
        segments=segments,
        tolerance_kmh=0.5,
        now_utc=NOW,
    )
    fine = optimal_constant_speed(
        vehicle=VEHICLE,
        start_soc_fraction=1.0,
        segments=segments,
        tolerance_kmh=0.001,
        now_utc=NOW,
    )
    assert coarse.speed_kmh is not None and fine.speed_kmh is not None
    assert coarse.speed_kmh == pytest.approx(fine.speed_kmh, abs=0.5)


def test_the_optimiser_accepts_a_plain_distance_and_a_vehicle_without_a_reserve():
    no_reserve = VehicleConfig(
        **{**VEHICLE.model_dump(), "battery_reserve_fraction": 0.0, "name": "no-reserve"}
    )
    with_reserve = optimal_constant_speed(
        vehicle=VEHICLE,
        start_soc_fraction=1.0,
        distance_m=400_000.0,
        segment_count=16,
        now_utc=NOW,
    )
    without = optimal_constant_speed(
        vehicle=no_reserve,
        start_soc_fraction=1.0,
        distance_m=400_000.0,
        segment_count=16,
        now_utc=NOW,
    )
    assert with_reserve.speed_kmh is not None and without.speed_kmh is not None
    # Releasing the reserve buys speed; it is a policy choice, not free energy.
    assert without.speed_kmh > with_reserve.speed_kmh
