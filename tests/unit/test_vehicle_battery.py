"""rwi.vehicle.battery: reserve boundary, round-trip efficiency, temperature band."""

from __future__ import annotations

import pytest

from rwi.contracts import VehicleConfig
from rwi.vehicle.battery import (
    BatteryLimits,
    BatteryState,
    apply_net_energy,
    charge,
    discharge,
    state_from_vehicle,
    temperature_violations,
)

PACK = VehicleConfig(
    name="battery-test-pack",
    mass_kg=300.0,
    drag_area_cda_m2=0.1,
    rolling_resistance_coefficient=0.005,
    drivetrain_efficiency=0.92,
    auxiliary_power_w=25.0,
    battery_capacity_wh=3000.0,  # legal: below the 3055.56 Wh clause-2.5.2 ceiling
    battery_reserve_fraction=0.10,
    battery_round_trip_efficiency=0.95,
    parameter_source="assumed",
)


def full() -> BatteryState:
    return state_from_vehicle(PACK, soc_fraction=1.0)


def constraints(transition) -> set[str]:
    return {v.constraint for v in transition.violations}


def blocking(transition) -> set[str]:
    return {v.constraint for v in transition.violations if v.severity == "blocking"}


# ---------------------------------------------------------------------------
# State arithmetic
# ---------------------------------------------------------------------------


def test_capacity_reserve_and_usable_capacity():
    state = full()
    assert state.energy_wh == 3000.0
    assert state.soc_fraction == 1.0
    assert state.reserve_wh == 300.0
    assert state.usable_capacity_wh == 2700.0
    assert state.energy_above_reserve_wh == 2700.0
    assert state.is_below_reserve is False


def test_state_from_vehicle_rejects_an_impossible_soc():
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        state_from_vehicle(PACK, soc_fraction=1.5)


# ---------------------------------------------------------------------------
# The reserve boundary: exactly at it is fine, one step past it is not
# ---------------------------------------------------------------------------


def test_discharging_exactly_the_usable_capacity_lands_on_the_reserve():
    transition = discharge(full(), 2700.0)
    assert transition.state.energy_wh == pytest.approx(300.0, abs=1e-9)
    assert transition.state.soc_fraction == pytest.approx(0.10, abs=1e-12)
    assert transition.delivered_wh == 2700.0
    assert transition.shortfall_wh == 0.0
    assert transition.violations == []
    assert transition.state.is_at_or_below_reserve is True
    assert transition.state.is_below_reserve is False


def test_one_step_past_the_reserve_is_a_blocking_violation():
    at_reserve = discharge(full(), 2700.0).state
    next_step = discharge(at_reserve, 1.0)
    assert "battery_reserve" in blocking(next_step)
    assert next_step.state.energy_wh == pytest.approx(299.0)
    detail = next_step.violations[0].detail
    assert "299.0 Wh" in detail
    assert "300.0 Wh" in detail
    assert "policy" in detail


def test_discharging_past_empty_reports_a_shortfall_and_clamps_at_zero():
    transition = discharge(full(), 4000.0)
    assert transition.state.energy_wh == 0.0
    assert transition.state.soc_fraction == 0.0
    assert transition.delivered_wh == 3000.0
    assert transition.shortfall_wh == pytest.approx(1000.0)
    assert transition.unclamped_energy_wh == pytest.approx(-1000.0)
    assert {"battery_empty", "battery_reserve"} <= blocking(transition)


# ---------------------------------------------------------------------------
# Round-trip efficiency, applied once
# ---------------------------------------------------------------------------


def test_round_trip_efficiency_is_applied_once_on_the_charge_side():
    half = state_from_vehicle(PACK, soc_fraction=0.5)
    assert half.energy_wh == 1500.0

    charged = charge(half, 1000.0)
    # 1000 Wh offered * 0.95 = 950.0 Wh stored. Exactly once: not 0.95^2 = 902.5.
    assert charged.stored_wh == pytest.approx(950.0, abs=1e-9)
    assert charged.state.energy_wh == pytest.approx(2450.0, abs=1e-9)
    assert charged.state.energy_wh != pytest.approx(1500.0 + 902.5)

    discharged = discharge(charged.state, 1000.0)
    # Discharge is 1:1 - the loss was already taken on the way in.
    assert discharged.delivered_wh == pytest.approx(1000.0, abs=1e-9)
    assert discharged.state.energy_wh == pytest.approx(1450.0, abs=1e-9)

    # A full charge-then-discharge of 1000 Wh costs exactly (1 - 0.95) * 1000 Wh.
    assert half.energy_wh - discharged.state.energy_wh == pytest.approx(50.0, abs=1e-9)
    assert "applied once" in " ".join(charged.notes)


def test_lossless_pack_round_trips_exactly():
    lossless = full().model_copy(update={"round_trip_efficiency": 1.0, "energy_wh": 2000.0})
    out = discharge(charge(lossless, 500.0).state, 500.0)
    assert out.state.energy_wh == pytest.approx(2000.0, abs=1e-9)


def test_charging_past_capacity_spills_with_a_warning_not_a_blocking_violation():
    nearly = state_from_vehicle(PACK, soc_fraction=0.99)  # 2970 Wh
    transition = charge(nearly, 1000.0)  # 950 Wh offered to a 30 Wh hole
    assert transition.state.energy_wh == pytest.approx(3000.0)
    assert transition.stored_wh == pytest.approx(30.0)
    assert transition.spilled_wh == pytest.approx(920.0, abs=1e-9)
    assert constraints(transition) == {"battery_capacity"}
    assert blocking(transition) == set()
    assert "curtailed" in transition.violations[0].detail


def test_sign_convention_of_apply_net_energy():
    state = state_from_vehicle(PACK, soc_fraction=0.5)
    assert apply_net_energy(state, 100.0).state.energy_wh > state.energy_wh
    assert apply_net_energy(state, -100.0).state.energy_wh == pytest.approx(1400.0)
    assert apply_net_energy(state, 0.0).state.energy_wh == pytest.approx(1500.0)


def test_wrong_signs_are_rejected_rather_than_silently_inverted():
    with pytest.raises(ValueError, match="non-negative"):
        discharge(full(), -10.0)
    with pytest.raises(ValueError, match="non-negative"):
        charge(full(), -10.0)


# ---------------------------------------------------------------------------
# Temperature: flagged, never modelled
# ---------------------------------------------------------------------------


def test_temperature_inside_the_band_raises_nothing():
    state = state_from_vehicle(PACK, soc_fraction=0.8, temperature_c=30.0)
    assert temperature_violations(state) == []


def test_temperature_outside_the_band_warns_and_admits_it_is_unvalidated():
    hot = state_from_vehicle(PACK, soc_fraction=0.8, temperature_c=55.0)
    violations = temperature_violations(hot)
    assert len(violations) == 1
    assert violations[0].constraint == "battery_temperature"
    assert violations[0].severity == "warning"
    assert "UNVALIDATED" in violations[0].detail
    assert "55.0 degC" in violations[0].detail

    cold = state_from_vehicle(PACK, soc_fraction=0.8, temperature_c=-5.0)
    assert len(temperature_violations(cold)) == 1


def test_unknown_temperature_is_not_treated_as_a_safe_temperature():
    unknown = state_from_vehicle(PACK, soc_fraction=0.8, temperature_c=None)
    assert unknown.temperature_c is None
    assert temperature_violations(unknown) == []


def test_the_safe_band_is_configurable():
    state = state_from_vehicle(PACK, soc_fraction=0.8, temperature_c=50.0)
    assert temperature_violations(state, limits=BatteryLimits(max_temperature_c=60.0)) == []
    assert temperature_violations(state, limits=BatteryLimits(max_temperature_c=40.0))


def test_temperature_never_changes_the_energy_numbers():
    cool = discharge(state_from_vehicle(PACK, soc_fraction=1.0, temperature_c=25.0), 1000.0)
    hot = discharge(state_from_vehicle(PACK, soc_fraction=1.0, temperature_c=70.0), 1000.0)
    assert cool.state.energy_wh == hot.state.energy_wh
    assert "battery_temperature" in constraints(hot)
    assert "battery_temperature" not in constraints(cool)
