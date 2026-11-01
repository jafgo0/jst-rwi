"""Physics of rwi.vehicle.dynamics: signs, exact numbers, scaling, policies.

Every input is constructed here. Nothing imports rwi.route / rwi.solar /
rwi.energy, and nothing touches the network.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from rwi.contracts import VehicleConfig
from rwi.core.units import GRAVITY_M_S2, STANDARD_PRESSURE_PA, air_density_kg_m3
from rwi.core.wind import relative_wind
from rwi.regulations import REGULATIONS_VERSION, EventClass, check_energy_storage
from rwi.vehicle.dynamics import (
    CHALLENGER_MAX_STORAGE_WH,
    DEFAULT_VEHICLE,
    ISA_SEA_LEVEL_AIR_DENSITY_KG_M3,
    consumption,
    fit_coastdown,
    resolve_air_density,
)

FIXED_TIME = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

# A vehicle with the exact parameters used by the hand-computed reference values.
REFERENCE_VEHICLE = VehicleConfig(
    name="reference-for-tests",
    mass_kg=300.0,
    drag_area_cda_m2=0.1,
    rolling_resistance_coefficient=0.005,
    drivetrain_efficiency=1.0,  # 1.0 so mechanical and electrical numbers coincide
    auxiliary_power_w=0.0,
    battery_capacity_wh=CHALLENGER_MAX_STORAGE_WH,
    battery_reserve_fraction=0.1,
    parameter_source="assumed",
)

RHO = 1.2
SPEED = 25.0


def estimate(vehicle: VehicleConfig = REFERENCE_VEHICLE, **kwargs):
    kwargs.setdefault("air_density_kg_m3", RHO)
    kwargs.setdefault("now_utc", FIXED_TIME)
    return consumption(vehicle, **kwargs)


# ---------------------------------------------------------------------------
# Exact numbers. A units regression must fail here, loudly.
# ---------------------------------------------------------------------------


@pytest.mark.science
def test_aerodynamic_power_exact_still_air():
    """P_aero = 0.5 * rho * CdA * v^3 = 0.5 * 1.2 * 0.1 * 25^3 = 937.5 W exactly."""
    result = estimate(speed_mps=SPEED)
    expected = 0.5 * RHO * 0.1 * SPEED**3
    assert expected == 937.5
    assert result.aerodynamic_power_w == pytest.approx(937.5, abs=1e-12)


@pytest.mark.science
def test_rolling_power_exact_flat_road():
    """P_roll = Crr * m * g * v = 0.005 * 300 * 9.80665 * 25 = 367.749375 W exactly.

    ``g`` is the standard gravity of rwi.core.units (9.80665 m/s2, CGPM 1901).
    """
    result = estimate(speed_mps=SPEED)
    assert GRAVITY_M_S2 == 9.80665
    expected = 0.005 * 300.0 * GRAVITY_M_S2 * SPEED
    assert expected == pytest.approx(367.749375, abs=1e-12)
    assert result.rolling_power_w == pytest.approx(367.749375, abs=1e-9)
    assert result.gradient_power_w == 0.0


@pytest.mark.science
def test_isa_sea_level_air_density_matches_the_standard_value():
    """ICAO ISA sea level: 101325 Pa, 15 degC -> 1.225 kg/m3 (standard reference)."""
    assert pytest.approx(1.225, abs=5e-4) == ISA_SEA_LEVEL_AIR_DENSITY_KG_M3
    rho, note = resolve_air_density()
    assert rho == ISA_SEA_LEVEL_AIR_DENSITY_KG_M3
    assert "ISA sea-level default" in note


def test_total_power_is_electrical_demand_not_the_sum_of_the_parts():
    vehicle = DEFAULT_VEHICLE
    result = consumption(
        vehicle, speed_mps=SPEED, air_density_kg_m3=RHO, grade=0.01, now_utc=FIXED_TIME
    )
    tractive = result.aerodynamic_power_w + result.rolling_power_w + result.gradient_power_w
    assert result.total_power_w == pytest.approx(
        tractive / vehicle.drivetrain_efficiency + vehicle.auxiliary_power_w, abs=1e-9
    )
    assert result.total_power_w != pytest.approx(tractive + vehicle.auxiliary_power_w)


# ---------------------------------------------------------------------------
# Signs: apparent wind
# ---------------------------------------------------------------------------


def _wind(speed_mps: float, from_deg: float, *, vehicle_speed_mps: float = SPEED):
    """Apparent wind for a vehicle heading due north (heading 0 deg)."""
    return relative_wind(
        vehicle_speed_mps=vehicle_speed_mps,
        vehicle_heading_deg=0.0,
        wind_speed_mps=speed_mps,
        wind_from_deg=from_deg,
    )


def test_headwind_raises_and_tailwind_lowers_aerodynamic_power():
    """5 m/s head/tail wind, strictly either side of the still-air case."""
    still = estimate(speed_mps=SPEED).aerodynamic_power_w
    head = _wind(5.0, 0.0)  # from the north, vehicle heading north
    tail = _wind(5.0, 180.0)  # from the south
    assert head.headwind_mps == pytest.approx(SPEED + 5.0)
    assert tail.headwind_mps == pytest.approx(SPEED - 5.0)

    head_power = estimate(speed_mps=SPEED, relative_wind=head).aerodynamic_power_w
    tail_power = estimate(speed_mps=SPEED, relative_wind=tail).aerodynamic_power_w

    assert head_power > still > tail_power
    # The apparent airspeed is squared; the ground speed stays linear.
    assert head_power == pytest.approx(still * ((SPEED + 5.0) / SPEED) ** 2, rel=1e-12)
    assert tail_power == pytest.approx(still * ((SPEED - 5.0) / SPEED) ** 2, rel=1e-12)


def test_tailwind_stronger_than_ground_speed_makes_drag_power_negative():
    """A 30 m/s tailwind at 25 m/s ground speed pushes the car: -37.5 W."""
    wind = _wind(30.0, 180.0)
    assert wind.headwind_mps == pytest.approx(-5.0)
    power = estimate(speed_mps=SPEED, relative_wind=wind).aerodynamic_power_w
    assert power < 0.0
    assert power == pytest.approx(0.5 * RHO * 0.1 * -(5.0**2) * SPEED, abs=1e-12)
    assert power == pytest.approx(-37.5, abs=1e-12)


def test_raw_wind_speed_is_not_used_instead_of_the_apparent_wind():
    """Head and tail winds of equal speed must not give the same drag power."""
    head = estimate(speed_mps=SPEED, relative_wind=_wind(8.0, 0.0)).aerodynamic_power_w
    tail = estimate(speed_mps=SPEED, relative_wind=_wind(8.0, 180.0)).aerodynamic_power_w
    assert head != pytest.approx(tail)


def test_pure_crosswind_leaves_the_along_track_airspeed_at_ground_speed():
    """With yaw sensitivity 0 a crosswind changes nothing in this model."""
    cross = _wind(5.0, 90.0)
    assert cross.headwind_mps == pytest.approx(SPEED)
    assert cross.crosswind_mps != pytest.approx(0.0)
    assert estimate(speed_mps=SPEED, relative_wind=cross).aerodynamic_power_w == pytest.approx(
        estimate(speed_mps=SPEED).aerodynamic_power_w, abs=1e-12
    )


# ---------------------------------------------------------------------------
# Signs: gradient
# ---------------------------------------------------------------------------


def test_gradient_symmetry_about_the_flat_case():
    """Uphill costs, downhill pays back, and the gradient term is antisymmetric.

    The symmetry asserted here, precisely:

    * ``gradient_power_w(+g) == -gradient_power_w(-g)`` exactly, because
      ``sin(atan(g))`` is odd in ``g``;
    * ``rolling_power_w(+g) == rolling_power_w(-g)`` exactly, because
      ``cos(atan(g))`` is even in ``g``;
    * therefore the mean of the uphill and downhill *tractive* powers equals
      the tractive power on a road with the same cos-corrected rolling term,
      which is marginally **below** the flat-road value (``cos < 1`` off the
      flat) - a 1 % grade reduces the rolling term by only 0.005 %.
    """
    grade = 0.01
    flat = estimate(speed_mps=SPEED)
    up = estimate(speed_mps=SPEED, grade=grade)
    down = estimate(speed_mps=SPEED, grade=-grade)

    assert up.gradient_power_w == pytest.approx(-down.gradient_power_w, abs=1e-12)
    assert up.gradient_power_w > 0.0 > down.gradient_power_w
    assert up.rolling_power_w == pytest.approx(down.rolling_power_w, abs=1e-12)
    assert up.aerodynamic_power_w == pytest.approx(down.aerodynamic_power_w, abs=1e-12)

    def tractive(e):
        return e.aerodynamic_power_w + e.rolling_power_w + e.gradient_power_w

    assert tractive(up) > tractive(flat) > tractive(down)
    mean = 0.5 * (tractive(up) + tractive(down))
    assert mean <= tractive(flat)
    assert mean == pytest.approx(tractive(flat), rel=1e-3)

    # And the magnitude is the textbook m*g*sin(atan(grade))*v.
    assert up.gradient_power_w == pytest.approx(
        300.0 * GRAVITY_M_S2 * math.sin(math.atan(grade)) * SPEED, abs=1e-9
    )


def test_total_power_increases_uphill_and_decreases_downhill():
    flat = estimate(vehicle=DEFAULT_VEHICLE, speed_mps=SPEED).total_power_w
    up = estimate(vehicle=DEFAULT_VEHICLE, speed_mps=SPEED, grade=0.01).total_power_w
    down = estimate(vehicle=DEFAULT_VEHICLE, speed_mps=SPEED, grade=-0.01).total_power_w
    assert up > flat > down


# ---------------------------------------------------------------------------
# Scaling
# ---------------------------------------------------------------------------


def test_aerodynamic_power_is_cubic_in_speed_in_still_air():
    slow = estimate(speed_mps=12.5).aerodynamic_power_w
    fast = estimate(speed_mps=25.0).aerodynamic_power_w
    assert fast == pytest.approx(8.0 * slow, rel=1e-12)


def test_rolling_power_is_linear_in_speed():
    slow = estimate(speed_mps=12.5).rolling_power_w
    fast = estimate(speed_mps=25.0).rolling_power_w
    assert fast == pytest.approx(2.0 * slow, rel=1e-12)


# ---------------------------------------------------------------------------
# Regeneration / negative power policy
# ---------------------------------------------------------------------------


def test_default_policy_clamps_the_battery_draw_at_zero_and_says_so():
    result = consumption(
        DEFAULT_VEHICLE, speed_mps=SPEED, grade=-0.10, air_density_kg_m3=RHO, now_utc=FIXED_TIME
    )
    tractive = result.aerodynamic_power_w + result.rolling_power_w + result.gradient_power_w
    assert tractive < 0.0
    assert result.total_power_w == pytest.approx(DEFAULT_VEHICLE.auxiliary_power_w, abs=1e-12)
    assert "CLAMPED to 0 W" in (result.provenance.notes or "")
    assert "regeneration=none" in (result.provenance.notes or "")


def test_regenerative_policy_multiplies_by_efficiency_and_may_go_negative():
    result = consumption(
        DEFAULT_VEHICLE,
        speed_mps=SPEED,
        grade=-0.10,
        air_density_kg_m3=RHO,
        regeneration="regenerative",
        now_utc=FIXED_TIME,
    )
    tractive = result.aerodynamic_power_w + result.rolling_power_w + result.gradient_power_w
    assert result.total_power_w == pytest.approx(
        tractive * DEFAULT_VEHICLE.drivetrain_efficiency + DEFAULT_VEHICLE.auxiliary_power_w,
        abs=1e-9,
    )
    assert result.total_power_w < 0.0
    # Multiplying (not dividing) means less recovered power, never more.
    assert result.total_power_w > tractive / DEFAULT_VEHICLE.drivetrain_efficiency


def test_standing_still_costs_only_the_auxiliary_load():
    result = consumption(DEFAULT_VEHICLE, speed_mps=0.0, air_density_kg_m3=RHO, now_utc=FIXED_TIME)
    assert result.aerodynamic_power_w == 0.0
    assert result.rolling_power_w == 0.0
    assert result.total_power_w == pytest.approx(DEFAULT_VEHICLE.auxiliary_power_w)


def test_negative_speed_is_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        consumption(DEFAULT_VEHICLE, speed_mps=-1.0, now_utc=FIXED_TIME)


# ---------------------------------------------------------------------------
# Air density
# ---------------------------------------------------------------------------


def test_air_density_from_pressure_and_temperature_is_used_and_recorded():
    result = consumption(
        REFERENCE_VEHICLE,
        speed_mps=SPEED,
        pressure_pa=95_000.0,
        temperature_c=35.0,
        now_utc=FIXED_TIME,
    )
    expected = air_density_kg_m3(95_000.0, 35.0)
    assert result.air_density_kg_m3 == pytest.approx(expected)
    assert expected < ISA_SEA_LEVEL_AIR_DENSITY_KG_M3  # hot and high: thinner air
    assert "ideal gas law" in (result.provenance.notes or "")
    assert result.aerodynamic_power_w == pytest.approx(0.5 * expected * 0.1 * SPEED**3)


def test_missing_atmosphere_falls_back_to_isa_and_records_the_substitution():
    result = consumption(REFERENCE_VEHICLE, speed_mps=SPEED, now_utc=FIXED_TIME)
    assert result.air_density_kg_m3 == ISA_SEA_LEVEL_AIR_DENSITY_KG_M3
    assert "ISA sea-level default" in (result.provenance.notes or "")
    assert resolve_air_density(pressure_pa=STANDARD_PRESSURE_PA, temperature_c=15.0)[0] == (
        pytest.approx(ISA_SEA_LEVEL_AIR_DENSITY_KG_M3)
    )


def test_explicit_density_wins_over_pressure_and_temperature():
    result = consumption(
        REFERENCE_VEHICLE,
        speed_mps=SPEED,
        air_density_kg_m3=1.0,
        pressure_pa=95_000.0,
        temperature_c=35.0,
        now_utc=FIXED_TIME,
    )
    assert result.air_density_kg_m3 == 1.0
    assert "supplied directly" in (result.provenance.notes or "")


# ---------------------------------------------------------------------------
# Yaw
# ---------------------------------------------------------------------------


def test_yaw_sensitivity_defaults_to_no_effect_and_says_it_underestimates():
    assert DEFAULT_VEHICLE.yaw_drag_sensitivity_per_deg == 0.0
    result = estimate(vehicle=DEFAULT_VEHICLE, speed_mps=SPEED, relative_wind=_wind(10.0, 90.0))
    assert "UNDERESTIMATED" in (result.provenance.notes or "")
    assert result.aerodynamic_power_w == pytest.approx(
        estimate(vehicle=DEFAULT_VEHICLE, speed_mps=SPEED).aerodynamic_power_w, abs=1e-9
    )


def test_yaw_sensitivity_scales_cda_linearly_when_a_curve_is_supplied():
    sensitive = REFERENCE_VEHICLE.model_copy(update={"yaw_drag_sensitivity_per_deg": 0.01})
    wind = _wind(5.0, 90.0)
    yaw = abs(wind.yaw_angle_deg)
    assert yaw > 0.0
    plain = estimate(speed_mps=SPEED, relative_wind=wind).aerodynamic_power_w
    scaled = estimate(
        vehicle=sensitive, speed_mps=SPEED, relative_wind=wind
    ).aerodynamic_power_w
    assert scaled == pytest.approx(plain * (1.0 + 0.01 * yaw), rel=1e-12)
    assert "PLACEHOLDER" in (
        estimate(vehicle=sensitive, speed_mps=SPEED, relative_wind=wind).provenance.notes or ""
    )


# ---------------------------------------------------------------------------
# DEFAULT_VEHICLE honesty
# ---------------------------------------------------------------------------


def test_default_vehicle_is_labelled_assumed_and_lists_every_measurement_needed():
    assert DEFAULT_VEHICLE.parameter_source == "assumed"
    assert "PLACEHOLDER" in DEFAULT_VEHICLE.name
    notes = DEFAULT_VEHICLE.source_notes or ""
    assert "NOT A JU SOLAR TEAM VEHICLE SPECIFICATION" in notes
    for parameter in (
        "mass_kg",
        "drag_area_cda_m2",
        "rolling_resistance_coefficient",
        "drivetrain_efficiency",
        "auxiliary_power_w",
        "battery_capacity_wh",
        "battery_reserve_fraction",
        "battery_round_trip_efficiency",
        "max_legal_speed_kmh",
        "yaw_drag_sensitivity_per_deg",
    ):
        assert parameter in notes, f"{parameter} has no documented replacement measurement"
    assert "2027" in notes


@pytest.mark.science
def test_default_vehicle_storage_is_the_clause_2_5_2_regulatory_ceiling():
    """Clause 2.5.2 (2027 BWSC Event Regulations V1.0): Challenger storage <= 11 MJ.

    11 MJ = 11e6 / 3600 Wh = 3055.555... Wh. The reference value comes from
    rwi.regulations, the platform's single audited source for the regulations;
    this test asserts that the shipped default is legal, is exactly the ceiling,
    and says in its own notes that a ceiling is not a measured pack.
    """
    assert CHALLENGER_MAX_STORAGE_WH == pytest.approx(11.0e6 / 3600.0, abs=1e-9)
    assert CHALLENGER_MAX_STORAGE_WH == pytest.approx(3055.5556, abs=1e-3)
    assert DEFAULT_VEHICLE.battery_capacity_wh == pytest.approx(CHALLENGER_MAX_STORAGE_WH)
    assert check_energy_storage(DEFAULT_VEHICLE.battery_capacity_wh, EventClass.CHALLENGER) is None

    notes = DEFAULT_VEHICLE.source_notes or ""
    assert "REGULATORY CEILING" in notes
    assert "2.5.2" in notes
    assert REGULATIONS_VERSION in notes
    # The resolved question must no longer be carried as an open TODO.
    assert "TODO(regulations)" not in notes


@pytest.mark.science
def test_a_pack_above_the_clause_2_5_2_ceiling_is_reported_against_that_clause():
    """Clause 2.5.2: 5000 Wh describes an illegal Challenger car."""
    finding = check_energy_storage(5000.0, EventClass.CHALLENGER)
    assert finding is not None
    assert finding.clause == "2.5.2"
    assert finding.blocking is True
    assert "5000 Wh" in finding.detail


def test_default_vehicle_speed_ceiling_defers_to_the_posted_limit():
    # The notes are hard-wrapped, so compare on whitespace-collapsed text.
    notes = " ".join((DEFAULT_VEHICLE.source_notes or "").split())
    assert "LAST-RESORT ceiling" in notes
    assert "3.31.6" in notes
    assert "Team Manager's Guide and NOT from the Event Regulations" in notes
    assert "posted limit of each individual segment governs" in notes


def test_consumption_provenance_is_a_physical_model_not_an_observation():
    result = estimate(speed_mps=SPEED)
    assert result.provenance.kind.value == "physical_model"
    assert result.provenance.is_measurement is False
    assert result.provenance.retrieved_at_utc == FIXED_TIME


# ---------------------------------------------------------------------------
# Coast-down style fit
# ---------------------------------------------------------------------------

FIT_CDA = 0.12
FIT_CRR = 0.0062
FIT_MASS = 320.0
FIT_RHO = 1.18


def _synthetic_power(speeds):
    return [
        0.5 * FIT_RHO * FIT_CDA * v**3 + FIT_CRR * FIT_MASS * GRAVITY_M_S2 * v for v in speeds
    ]


def test_fit_coastdown_recovers_the_parameters_it_was_generated_from():
    speeds = [8.0, 12.0, 16.0, 20.0, 25.0, 30.0]
    fit = fit_coastdown(
        speeds,
        _synthetic_power(speeds),
        mass_kg=FIT_MASS,
        air_density_kg_m3=FIT_RHO,
        now_utc=FIXED_TIME,
    )
    assert fit.drag_area_cda_m2 == pytest.approx(FIT_CDA, rel=1e-9)
    assert fit.rolling_resistance_coefficient == pytest.approx(FIT_CRR, rel=1e-9)
    assert fit.residual_rms_w == pytest.approx(0.0, abs=1e-6)
    assert fit.r_squared == pytest.approx(1.0, abs=1e-12)
    assert fit.sample_count == len(speeds)
    assert "lsq_linear" in fit.method


def test_fit_coastdown_result_can_replace_the_assumed_parameters():
    speeds = [8.0, 14.0, 20.0, 26.0, 32.0]
    fit = fit_coastdown(
        speeds,
        _synthetic_power(speeds),
        mass_kg=FIT_MASS,
        air_density_kg_m3=FIT_RHO,
        now_utc=FIXED_TIME,
    )
    updated = fit.apply_to(DEFAULT_VEHICLE)
    assert updated.drag_area_cda_m2 == pytest.approx(FIT_CDA, rel=1e-9)
    assert updated.rolling_resistance_coefficient == pytest.approx(FIT_CRR, rel=1e-9)
    assert updated.parameter_source == "measured"
    assert updated.mass_kg == DEFAULT_VEHICLE.mass_kg  # untouched
    # A fit of synthetic data must be allowed to stay honest about itself.
    synthetic = fit.apply_to(DEFAULT_VEHICLE, parameter_source="assumed")
    assert synthetic.parameter_source == "assumed"


def test_fit_coastdown_accepts_electrical_power_with_an_efficiency():
    speeds = [10.0, 18.0, 26.0]
    mechanical = _synthetic_power(speeds)
    electrical = [p / 0.9 for p in mechanical]
    fit = fit_coastdown(
        speeds,
        electrical,
        mass_kg=FIT_MASS,
        air_density_kg_m3=FIT_RHO,
        power_is_mechanical=False,
        drivetrain_efficiency=0.9,
        now_utc=FIXED_TIME,
    )
    assert fit.drag_area_cda_m2 == pytest.approx(FIT_CDA, rel=1e-9)
    assert fit.rolling_resistance_coefficient == pytest.approx(FIT_CRR, rel=1e-9)
    assert any("ELECTRICAL" in note for note in fit.notes)


def test_fit_coastdown_is_robust_to_moderate_noise():
    speeds = [8.0, 10.0, 12.5, 15.0, 17.5, 20.0, 22.5, 25.0, 27.5, 30.0]
    clean = _synthetic_power(speeds)
    # Deterministic, alternating +/-15 W measurement error. No RNG in tests.
    noisy = [p + (15.0 if i % 2 == 0 else -15.0) for i, p in enumerate(clean)]
    fit = fit_coastdown(
        speeds, noisy, mass_kg=FIT_MASS, air_density_kg_m3=FIT_RHO, now_utc=FIXED_TIME
    )
    assert fit.drag_area_cda_m2 == pytest.approx(FIT_CDA, rel=0.05)
    assert fit.rolling_resistance_coefficient == pytest.approx(FIT_CRR, rel=0.15)
    assert fit.residual_rms_w > 0.0


def test_fit_coastdown_warns_when_the_speed_span_cannot_separate_the_terms():
    speeds = [20.0, 21.0, 22.0]
    fit = fit_coastdown(
        speeds,
        _synthetic_power(speeds),
        mass_kg=FIT_MASS,
        air_density_kg_m3=FIT_RHO,
        now_utc=FIXED_TIME,
    )
    assert any("poorly separated" in note for note in fit.notes)


def test_fit_coastdown_rejects_unusable_data():
    with pytest.raises(ValueError, match="equal length"):
        fit_coastdown([10.0, 20.0], [100.0], mass_kg=300.0)
    with pytest.raises(ValueError, match="at least 2 samples"):
        fit_coastdown([10.0], [100.0], mass_kg=300.0)
    with pytest.raises(ValueError, match="distinct speeds"):
        fit_coastdown([20.0, 20.0, 20.0], [100.0, 101.0, 99.0], mass_kg=300.0)
    with pytest.raises(ValueError, match="drivetrain_efficiency is required"):
        fit_coastdown([10.0, 20.0], [100.0, 400.0], mass_kg=300.0, power_is_mechanical=False)
