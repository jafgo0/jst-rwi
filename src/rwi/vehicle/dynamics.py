"""Longitudinal power model for a Challenger-class solar car.

Scope
-----
A quasi-static, point-mass, longitudinal force balance. At a given operating
point (ground speed, road gradient, air density, apparent wind) it returns the
*electrical* power the battery must supply. There is no lateral dynamics, no
tyre slip, no transient (inertial) term, and no thermal derating of the motor
or controller.

The force balance
-----------------
At steady speed the tractive force at the road must balance three resistances::

    F_aero  = 0.5 * rho * CdA * v_air^2                     [N]
    F_roll  = Crr * m * g * cos(theta)                      [N]
    F_grade = m * g * sin(theta)                            [N]

with ``theta = atan(grade)``, ``grade`` = rise/run (dimensionless). Mechanical
power is force times the speed at which the force does work *on the road*,
which is the **ground speed** ``v`` (m/s)::

    P_aero  = 0.5 * rho * CdA * v_air^2 * v                 [W]
    P_roll  = Crr * m * g * cos(theta) * v                  [W]
    P_grade = m * g * sin(theta) * v                        [W]

Apparent wind: why ground speed alone is wrong
----------------------------------------------
The aerodynamic *force* is set by the speed of the air over the body, not by
the speed over the ground. The relative (apparent) wind is the vector sum of
the true wind and the reversed ground-speed vector; that vector sum is computed
once, in :func:`rwi.core.wind.relative_wind`, and never re-derived here. Its
``headwind_mps`` component is the along-track apparent airspeed, positive for a
headwind:

    ``v_air = relative_wind.headwind_mps = v - w_along``

where ``w_along`` is the component of the true wind blowing in the direction of
travel. Consequences, all of them physical and all of them intended:

* **Headwind** (``w_along < 0``): ``v_air > v``, so ``P_aero`` is *larger* than
  in still air. A 5 m/s headwind at 25 m/s ground speed raises ``v_air`` to
  30 m/s and the drag power by a factor ``(30/25)^2 = 1.44``.
* **Tailwind** (``0 < w_along < v``): ``v_air < v``, so drag power is *smaller*
  than in still air. The reduction is real and is the single biggest free gain
  available on the Stuart Highway.
* **Tailwind stronger than the ground speed** (``w_along > v``): ``v_air`` goes
  negative. The air now pushes the body forward and the drag term becomes a
  *propulsive* power. To keep that sign, the square is taken as a **signed**
  square::

      P_aero = 0.5 * rho * CdA * sign(v_air) * v_air^2 * v

  so ``P_aero < 0`` exactly when ``v_air < 0``. Using ``v_air**2`` unsigned
  would silently turn a strong tailwind into extra drag; using the raw wind
  speed instead of the apparent wind would do the same for every wind
  direction. A test asserts the three cases above.

Only the along-track component is used for drag. The crosswind component is
*not* added into an apparent-speed magnitude, because the drag coefficient of a
solar car at yaw is not ``CdA`` and the team has no measured drag-vs-yaw curve.
The optional, default-off ``yaw_drag_sensitivity_per_deg`` exists so that a
measured curve can be approximated later as a linear increase

    ``CdA_eff = CdA * (1 + s * |yaw|)``   with ``s`` in 1/deg.

With the default ``s = 0.0`` there is no yaw effect at all. That is an
understatement of real drag, and it is recorded as such in the provenance
notes. **No yaw curve is invented here.**

Electrical demand, regeneration and negative power
--------------------------------------------------
Tractive mechanical power is converted to electrical demand by the drivetrain
efficiency, and the auxiliary load (telemetry, lights, pumps, driver fan) is
added on the electrical side::

    P_tractive = P_aero + P_roll + P_grade                  [W, mechanical]
    P_elec     = P_tractive / eta_drivetrain                [W, if P_tractive >= 0]
    P_total    = P_elec + P_aux                              [W, battery terminals]

When ``P_tractive < 0`` (steep descent, or a tailwind stronger than the ground
speed) the vehicle does not need the battery to move. Two policies exist and
the choice is explicit, because dividing a negative number by ``eta`` would
*overstate* recovered energy:

* ``regeneration="none"`` (**default, and the honest default**): the surplus is
  assumed to be thrown away in the brakes or by freewheeling, so the tractive
  part of the battery draw is clamped to ``0.0`` W. The auxiliary load is still
  drawn, so ``P_total = P_aux``. This under-promises: a car that does recover
  energy will do better than the model says.
* ``regeneration="regenerative"``: only when the team confirms that the motor
  controller recovers energy on the relevant descents. Then efficiency is
  applied as a *multiplication*, ``P_elec = P_tractive * eta_drivetrain``, so
  losses reduce the recovered power instead of inflating it, and ``P_total``
  may be negative (net charging). The battery round-trip efficiency is applied
  separately, on the storage side, in :mod:`rwi.vehicle.battery`; it is not
  applied here, so it cannot be double-counted.

Either way the policy actually used is written into
``ConsumptionEstimate.provenance.notes``.

Air density
-----------
``rho`` comes from :func:`rwi.core.units.air_density_kg_m3` (dry-air ideal gas
law) whenever a pressure and a temperature are supplied, or from an explicit
``air_density_kg_m3`` argument. Otherwise the ISA sea-level value
(101325 Pa, 15 degC => 1.2250 kg/m3) is used and the substitution is recorded
in the provenance notes. On the Stuart Highway (Alice Springs ~545 m, 35 degC)
the real density is nearer 1.10 kg/m3, so the ISA default *overestimates* drag
by roughly 10 %: conservative, but not accurate.

Validation status: implemented, not validated.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from typing import Final, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field
from scipy.optimize import lsq_linear

from rwi.contracts import ConsumptionEstimate, VehicleConfig
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import utc_now
from rwi.core.units import GRAVITY_M_S2, STANDARD_PRESSURE_PA
from rwi.core.units import air_density_kg_m3 as ideal_gas_air_density
from rwi.core.wind import RelativeWind
from rwi.regulations import (
    CLASS_LIMITS,
    MAX_POSTED_SPEED_KMH_TMG,
    REGULATIONS_VERSION,
    EventClass,
)

__all__ = [
    "CHALLENGER_MAX_STORAGE_WH",
    "DEFAULT_VEHICLE",
    "ISA_SEA_LEVEL_AIR_DENSITY_KG_M3",
    "ISA_SEA_LEVEL_TEMPERATURE_C",
    "CoastdownFit",
    "RegenerationPolicy",
    "consumption",
    "fit_coastdown",
    "resolve_air_density",
]

RegenerationPolicy = Literal["none", "regenerative"]
"""How a negative tractive power is treated. See the module docstring."""

ISA_SEA_LEVEL_TEMPERATURE_C: Final[float] = 15.0
"""ICAO International Standard Atmosphere sea-level temperature."""

ISA_SEA_LEVEL_AIR_DENSITY_KG_M3: Final[float] = ideal_gas_air_density(
    STANDARD_PRESSURE_PA, ISA_SEA_LEVEL_TEMPERATURE_C
)
"""Dry-air density at ISA sea level, ~1.2250 kg/m3. A documented fallback only."""

_ISA_NOTE: Final[str] = (
    "air_density=ISA sea-level default "
    f"({STANDARD_PRESSURE_PA:.0f} Pa, {ISA_SEA_LEVEL_TEMPERATURE_C:.0f} degC "
    f"=> {ISA_SEA_LEVEL_AIR_DENSITY_KG_M3:.4f} kg/m3); "
    "no pressure/temperature supplied, so drag is overestimated at race altitude"
)


# ---------------------------------------------------------------------------
# Default vehicle: an ASSUMED placeholder, not a JU Solar Team specification
# ---------------------------------------------------------------------------

_CHALLENGER_LIMITS: Final = CLASS_LIMITS[EventClass.CHALLENGER]

CHALLENGER_MAX_STORAGE_WH: Final[float] = _CHALLENGER_LIMITS.max_energy_storage_wh or 0.0
"""Challenger energy-storage ceiling, 11 MJ = 3055.56 Wh.

Taken from :mod:`rwi.regulations` (clause 2.5.2 of the 2027 BWSC Event
Regulations V1.0), which is the platform's single audited source for
regulatory limits. It is a **legal maximum**, not a pack specification: a real
pack's usable energy is lower and must be measured.
"""

_DEFAULT_VEHICLE_SOURCE_NOTES: Final[str] = f"""\
PLACEHOLDER - NOT A JU SOLAR TEAM VEHICLE SPECIFICATION.

Every number below is a generic, order-of-magnitude figure for a BWSC
Challenger-class single-seat solar car, assembled from publicly discussed
ranges for that vehicle class. None of it has been measured on, or supplied by,
JU Solar Team. It exists so that the model can be exercised and tested before
the car exists; it must be replaced parameter by parameter, and
parameter_source must become "measured" (or "datasheet") as that happens.
Results produced with this configuration are illustrative only and must not be
quoted as predictions for the team's car.

Measurement needed to replace each assumption:
  * mass_kg = 300: weigh the complete car in race trim on a calibrated scale,
    with the driver, ballast to the regulated minimum driver mass, battery,
    and all equipment carried. Record the weighing date and the scale used.
  * drag_area_cda_m2 = 0.10 (CdA, m2): wind-tunnel measurement over the yaw
    range the route actually produces, or a coast-down campaign on a closed,
    flat, calm-air road (see fit_coastdown). A single number is not enough: a
    drag-vs-yaw curve is needed before yaw_drag_sensitivity_per_deg can be set.
  * rolling_resistance_coefficient = 0.005 (Crr): tyre supplier test data at
    the race inflation pressure, tyre temperature and load, cross-checked by
    the same coast-down campaign; Crr also varies with road surface, so record
    the surface.
  * drivetrain_efficiency = 0.92: dynamometer measurement of motor plus
    controller plus wheel bearings over the real torque/speed envelope. It is
    a map, not a constant; 0.92 is a single-point stand-in.
  * auxiliary_power_w = 25: measure the actual house load (telemetry, radio,
    lights, instrument cluster, pumps, driver fan) with a shunt, in the day and
    night configurations separately.
  * battery_capacity_wh = {CHALLENGER_MAX_STORAGE_WH:.2f}: this is the
    REGULATORY CEILING for the Challenger class, not a measured pack capacity
    and not a pack this team owns. It is 11 MJ = {CHALLENGER_MAX_STORAGE_WH:.2f} Wh
    from clause 2.5.2 of the {REGULATIONS_VERSION}, via rwi.regulations, which
    is the platform's single audited source for regulatory limits. Using the
    ceiling as the placeholder is the most optimistic legal assumption, so any
    energy margin computed with it is an upper bound.
    The team MUST replace it with the built pack's measured usable energy from a
    full capacity test at race temperature - not the cell datasheet sum - and
    that figure will be lower than this ceiling.
  * battery_reserve_fraction = 0.10: a team policy decision, not a
    measurement. It must be agreed by the strategy group and the electrical
    lead, then recorded with the reasoning (limp-home distance, pack ageing,
    SoC-estimation error).
  * battery_round_trip_efficiency = 0.95: measure charge/discharge energy on
    the finished pack at race currents and temperatures.
  * max_legal_speed_kmh = {MAX_POSTED_SPEED_KMH_TMG:.0f}: a LAST-RESORT ceiling
    only. The posted limit of each individual segment governs, and exceeding it
    is penalised under clause 3.31.6; driving below 50 km/h where the posted
    limit is 100 km/h or more is a penalisable obstruction under the same
    clause. The figure itself ({MAX_POSTED_SPEED_KMH_TMG:.0f} km/h, the highest
    posted limit the Stuart Highway is known to contain) comes from the Team
    Manager's Guide and NOT from the Event Regulations; South Australia posts
    110 km/h, and road works or event instructions may impose less. It is not a
    permission to drive at this speed anywhere. The strategy layer must be given
    the posted limit for each segment, per segment.
  * yaw_drag_sensitivity_per_deg = 0.0: left at zero on purpose. Non-zero
    requires a measured drag-vs-yaw curve; inventing one would fabricate an
    aerodynamic property of a car nobody has tested.
"""

_TESSERACT_2025_SOURCE_NOTES = """\
PARAMETERS FROM 2025 JUST SOLAR CAR ("Tesseract"), NOT THE 2027 CAR.

Numbers below were read from the team's own repository drive-system at
3-software/host-software/python/cases/cars/tesseract-2025.yaml on 2026-11-02.
They describe the car that COMPETED in 2025, not the car being built for 2027,
and they are kept here only until the team supplies the 2027 car's own
measurements.

They are the LEAST wrong default available at this moment, which is why the
earlier generic placeholders were replaced with them. They are still wrong for
the actual car JST-RWI is being built to support.

Values and their location in the source file:
  * mass_kg = 269.0  (tesseract-2025.yaml:27, race mass incl. driver)
  * drag_area_cda_m2 = 0.137  (yaml:30, wind-tunnel figure)
  * rolling_resistance_coefficient = 0.0017  (yaml:33, analytical only;
    there is a surface-dependent polynomial in _defaults.yaml:29-32 the team
    uses in simulation that this single number does not represent)
  * drivetrain_efficiency = 0.92  (team models it as a speed-dependent map
    around a Mitsuba direct-drive hub motor, not a constant; the single
    number stays "assumed" until a measurement replaces it)
  * auxiliary_power_w = 12.6  (yaml:61 steady_loads_w sums to
    [2.6, 2.0, 1.0, 2.0, 3.0, 2.0] = 12.6 W)
  * battery_capacity_wh = 3341.0  (yaml:38-40, 38 cells x 3.2 V x 27.52 Ah =
    3341 Wh. NOTE: this EXCEEDS the Challenger 2.5.2 regulatory ceiling of
    3055.56 Wh, so it is a theoretical pack energy from cell datasheet
    numbers, not usable energy in race configuration. The regulatory ceiling
    still applies to any 2027 build.)

What this configuration does NOT capture, which the team's own simulator does:
  * no regenerative-braking model (team's p_output is also brake-free, so
    this gap is shared);
  * no speed-dependent drivetrain efficiency map (team has one in
    consumption/drivetrain_efficiency.py);
  * no altitude-corrected air density (team's fast kernel has an ISA term;
    rwi.vehicle.resolve_air_density takes pressure and temperature instead);
  * no surface-dependent rolling resistance polynomial.

Replace with the 2027 car's own measurements as soon as they exist, change
parameter_source to "measured" at that point, and remove this notes block.
"""

DEFAULT_VEHICLE: Final[VehicleConfig] = VehicleConfig(
    name="TESSERACT-2025 (JUST team's 2025 car, NOT the 2027 build)",
    mass_kg=269.0,
    drag_area_cda_m2=0.137,
    rolling_resistance_coefficient=0.0017,
    drivetrain_efficiency=0.92,
    auxiliary_power_w=12.6,
    battery_capacity_wh=CHALLENGER_MAX_STORAGE_WH,
    battery_reserve_fraction=0.10,
    battery_round_trip_efficiency=0.95,
    max_legal_speed_kmh=MAX_POSTED_SPEED_KMH_TMG,
    yaw_drag_sensitivity_per_deg=0.0,
    parameter_source="assumed",
    source_notes=_TESSERACT_2025_SOURCE_NOTES,
)
"""The 2025 car's measured parameters, used as the LEAST WRONG default.

The battery capacity stays clamped to the regulatory ceiling, not the 2025
pack's theoretical maximum: using 3341 Wh in the model would silently simulate
an illegal battery. See the source notes.
"""


# ---------------------------------------------------------------------------
# Air density
# ---------------------------------------------------------------------------


def resolve_air_density(
    *,
    air_density_kg_m3: float | None = None,
    pressure_pa: float | None = None,
    temperature_c: float | None = None,
) -> tuple[float, str]:
    """Air density to use for drag, plus a provenance note describing its origin.

    Precedence: an explicit ``air_density_kg_m3`` wins; otherwise a pressure and
    a temperature are put through the ideal-gas law
    (:func:`rwi.core.units.air_density_kg_m3`); otherwise the ISA sea-level
    default is substituted and the note says so. Units: Pa, degC, kg/m3.
    """
    if air_density_kg_m3 is not None:
        if air_density_kg_m3 <= 0.0:
            raise ValueError("air_density_kg_m3 must be positive")
        return air_density_kg_m3, "air_density=supplied directly by caller"
    if pressure_pa is not None and temperature_c is not None:
        rho = ideal_gas_air_density(pressure_pa, temperature_c)
        return rho, (
            "air_density=ideal gas law from supplied "
            f"pressure={pressure_pa:.0f} Pa, temperature={temperature_c:.1f} degC "
            "(dry air; humidity ignored, which overestimates density slightly)"
        )
    return ISA_SEA_LEVEL_AIR_DENSITY_KG_M3, _ISA_NOTE


# ---------------------------------------------------------------------------
# The power model
# ---------------------------------------------------------------------------


def consumption(
    vehicle: VehicleConfig,
    *,
    speed_mps: float,
    grade: float = 0.0,
    air_density_kg_m3: float | None = None,
    relative_wind: RelativeWind | None = None,
    pressure_pa: float | None = None,
    temperature_c: float | None = None,
    regeneration: RegenerationPolicy = "none",
    now_utc: datetime | None = None,
) -> ConsumptionEstimate:
    """Electrical power demand at one steady operating point.

    Parameters
    ----------
    vehicle:
        Mass, CdA, Crr, drivetrain efficiency and auxiliary load. If
        ``vehicle.parameter_source == "assumed"`` the output is an illustration,
        not a prediction.
    speed_mps:
        Ground speed, m/s, non-negative. This is the speed that appears
        linearly in every power term, because it is the speed at which the
        tractive force does work on the road.
    grade:
        Road gradient as rise/run (dimensionless). ``theta = atan(grade)``.
        Positive is uphill. ``P_grade`` is negative downhill.
    air_density_kg_m3:
        kg/m3. If None, derived from ``pressure_pa``/``temperature_c``, else the
        ISA sea-level default (recorded in the provenance notes).
    relative_wind:
        Apparent wind from :func:`rwi.core.wind.relative_wind`. Its
        ``headwind_mps`` is the along-track apparent airspeed used for drag
        (positive = headwind). If None, still air is assumed and
        ``v_air = speed_mps``.
    pressure_pa, temperature_c:
        Ambient state for the ideal-gas air density, Pa and degC.
    regeneration:
        ``"none"`` (default) clamps the tractive battery draw at 0 W when the
        tractive power is negative; ``"regenerative"`` multiplies it by the
        drivetrain efficiency and allows a negative total (net charging). See
        the module docstring.
    now_utc:
        Timestamp for the provenance record. Supply it when the result must be
        reproducible (the scenario engine always does); otherwise the current
        wall clock is used.

    Returns
    -------
    ConsumptionEstimate
        ``aerodynamic_power_w``, ``rolling_power_w`` and ``gradient_power_w``
        are *mechanical* powers at the road, in W, before the drivetrain
        division. ``auxiliary_power_w`` is the electrical house load.
        ``total_power_w`` is the **electrical** demand at the battery
        terminals and is therefore *not* the plain sum of the other four
        fields: it is
        ``f(P_aero + P_roll + P_grade) + P_aux`` with ``f`` the drivetrain /
        regeneration policy above. The breakdown is kept mechanical so that the
        physics can be checked term by term.

    Worked check (asserted in the tests): ``rho = 1.2``, ``CdA = 0.1``,
    ``v = 25`` m/s, still air gives
    ``P_aero = 0.5 * 1.2 * 0.1 * 25^3 = 937.5`` W, and ``Crr = 0.005``,
    ``m = 300`` kg, ``g = 9.80665`` m/s2, flat road gives
    ``P_roll = 0.005 * 300 * 9.80665 * 25 = 367.749375`` W.
    """
    if speed_mps < 0.0:
        raise ValueError("speed_mps must be non-negative (this is a longitudinal model)")
    if not math.isfinite(grade):
        raise ValueError("grade must be finite")

    rho, density_note = resolve_air_density(
        air_density_kg_m3=air_density_kg_m3,
        pressure_pa=pressure_pa,
        temperature_c=temperature_c,
    )

    v = float(speed_mps)
    v_air = relative_wind.headwind_mps if relative_wind is not None else v

    cda = vehicle.drag_area_cda_m2
    sensitivity = vehicle.yaw_drag_sensitivity_per_deg
    if relative_wind is not None and sensitivity > 0.0:
        yaw_factor = 1.0 + sensitivity * abs(relative_wind.yaw_angle_deg)
        cda *= yaw_factor
        yaw_note = (
            f"CdA scaled by {yaw_factor:.4f} = 1 + "
            f"{sensitivity:g}/deg * |yaw|={abs(relative_wind.yaw_angle_deg):.2f} deg "
            "(LINEAR PLACEHOLDER for a drag-vs-yaw curve; not a measured curve)"
        )
    else:
        yaw_note = (
            "no yaw drag correction (yaw_drag_sensitivity_per_deg=0): drag at yaw is "
            "UNDERESTIMATED until a drag-vs-yaw curve is measured"
        )

    # Signed square keeps a stronger-than-ground-speed tailwind propulsive.
    p_aero = 0.5 * rho * cda * math.copysign(v_air * v_air, v_air) * v

    theta = math.atan(grade)
    weight = vehicle.mass_kg * GRAVITY_M_S2
    p_roll = vehicle.rolling_resistance_coefficient * weight * math.cos(theta) * v
    p_grade = weight * math.sin(theta) * v

    p_tractive = p_aero + p_roll + p_grade
    if p_tractive >= 0.0:
        p_electric_traction = p_tractive / vehicle.drivetrain_efficiency
        policy_note = (
            f"tractive power positive; divided by drivetrain_efficiency="
            f"{vehicle.drivetrain_efficiency:g}"
        )
    elif regeneration == "regenerative":
        p_electric_traction = p_tractive * vehicle.drivetrain_efficiency
        policy_note = (
            "tractive power negative and regeneration=regenerative: MULTIPLIED by "
            f"drivetrain_efficiency={vehicle.drivetrain_efficiency:g} so losses reduce the "
            "recovered power; total_power_w may be negative (net charging). Battery "
            "round-trip efficiency is applied separately in rwi.vehicle.battery."
        )
    else:
        p_electric_traction = 0.0
        policy_note = (
            "tractive power negative and regeneration=none (default policy): battery draw "
            "from traction CLAMPED to 0 W; the surplus is assumed dissipated in the brakes or "
            "by freewheeling. Change only when the team confirms regenerative braking."
        )

    total = p_electric_traction + vehicle.auxiliary_power_w

    provenance = Provenance(
        provider="rwi",
        dataset="vehicle.dynamics.consumption",
        kind=DataKind.PHYSICAL_MODEL,
        retrieved_at_utc=now_utc if now_utc is not None else utc_now(),
        model_name="quasi-static longitudinal point-mass power balance",
        model_version="1",
        processing="; ".join(
            [
                "P_aero=0.5*rho*CdA*sign(v_air)*v_air^2*v_ground",
                "P_roll=Crr*m*g*cos(atan(grade))*v_ground",
                "P_grade=m*g*sin(atan(grade))*v_ground",
                "P_total=drivetrain(P_aero+P_roll+P_grade)+P_aux",
            ]
        ),
        notes=" | ".join(
            [
                density_note,
                yaw_note,
                policy_note,
                f"vehicle.parameter_source={vehicle.parameter_source}",
                "component fields are mechanical road power; total_power_w is electrical",
                "no inertial term (steady-state), no motor/controller thermal derating",
            ]
        ),
    )

    return ConsumptionEstimate(
        speed_mps=v,
        aerodynamic_power_w=p_aero,
        rolling_power_w=p_roll,
        gradient_power_w=p_grade,
        auxiliary_power_w=vehicle.auxiliary_power_w,
        total_power_w=total,
        relative_wind=relative_wind,
        air_density_kg_m3=rho,
        vehicle=vehicle,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# Coast-down style parameter fit
# ---------------------------------------------------------------------------


class CoastdownFit(BaseModel):
    """Result of fitting ``CdA`` and ``Crr`` to measured power-versus-speed data."""

    model_config = ConfigDict(frozen=True)

    drag_area_cda_m2: float = Field(description="fitted CdA, m2")
    rolling_resistance_coefficient: float = Field(description="fitted Crr, dimensionless")
    sample_count: int = Field(ge=0)
    residual_rms_w: float = Field(ge=0.0, description="RMS of (measured - fitted) power, W")
    r_squared: float = Field(description="coefficient of determination against measured power")
    air_density_kg_m3: float
    mass_kg: float
    method: str
    notes: list[str] = Field(default_factory=list)
    provenance: Provenance

    def apply_to(
        self, vehicle: VehicleConfig, *, parameter_source: str = "measured"
    ) -> VehicleConfig:
        """Copy of ``vehicle`` with the fitted CdA and Crr substituted.

        ``parameter_source`` is passed through so a caller fitting *synthetic*
        data can keep it at ``"assumed"`` and avoid labelling a simulation as a
        measurement.
        """
        return vehicle.model_copy(
            update={
                "drag_area_cda_m2": self.drag_area_cda_m2,
                "rolling_resistance_coefficient": self.rolling_resistance_coefficient,
                "parameter_source": parameter_source,
                "source_notes": (
                    f"CdA and Crr fitted from {self.sample_count} power-vs-speed samples "
                    f"({self.method}, residual RMS {self.residual_rms_w:.1f} W, "
                    f"R^2={self.r_squared:.4f}); all other parameters unchanged from "
                    f"{vehicle.name!r}."
                ),
            }
        )


def fit_coastdown(
    speed_mps: Sequence[float],
    power_w: Sequence[float],
    *,
    mass_kg: float,
    air_density_kg_m3: float | None = None,
    pressure_pa: float | None = None,
    temperature_c: float | None = None,
    power_is_mechanical: bool = True,
    drivetrain_efficiency: float | None = None,
    now_utc: datetime | None = None,
) -> CoastdownFit:
    """Fit ``CdA`` and ``Crr`` by least squares to (speed, power) measurements.

    The model being fitted is the flat-road, still-air, steady-state power
    balance of :func:`consumption`::

        P(v) = 0.5 * rho * CdA * v^3  +  Crr * m * g * v

    which is **linear in the two unknowns** ``CdA`` and ``Crr``. The design
    matrix columns are therefore ``0.5 * rho * v^3`` and ``m * g * v``, and the
    fit is a bounded linear least-squares problem solved with
    :func:`scipy.optimize.lsq_linear` with both parameters constrained
    non-negative (a negative CdA or Crr is unphysical and would otherwise be
    returned happily by an unconstrained solve of noisy data).

    Call it with data from a constant-speed power sweep, or with the
    speed/power pairs reconstructed from a classical coast-down
    (``P = -m * v * dv/dt``, differentiated by whoever owns the measurement).
    This function does not differentiate a ``v(t)`` trace itself, and it cannot
    separate CdA from Crr unless the samples span a decent speed range: two
    terms that both grow with ``v`` are only distinguishable because one grows
    as ``v^3``.

    Preconditions the caller is responsible for, and which the fit cannot
    detect: flat road (or gradient already removed), still air (or the apparent
    wind already removed), steady speed, warm tyres, known mass.

    Parameters
    ----------
    speed_mps, power_w:
        Equal-length sequences, at least 2 samples, at least 2 distinct speeds.
    mass_kg:
        Mass in the condition the data was taken in, kg.
    air_density_kg_m3, pressure_pa, temperature_c:
        Air density during the test; same precedence as in :func:`consumption`.
        Getting this wrong scales CdA by exactly the density error.
    power_is_mechanical:
        True when ``power_w`` is mechanical power at the road. False when it is
        measured electrical input power, in which case
        ``drivetrain_efficiency`` is required and the data is multiplied by it
        (and the auxiliary load must already have been subtracted by the
        caller).
    now_utc:
        Timestamp for the provenance record.

    Notes
    -----
    **No measurement data exists yet.** This helper is here so the assumed
    parameters of :data:`DEFAULT_VEHICLE` can be replaced by measurements the
    moment the team has them; it works purely on data supplied by the caller
    and has itself only been exercised against synthetic data in the tests.
    """
    v = np.asarray(speed_mps, dtype=float)
    p = np.asarray(power_w, dtype=float)
    if v.ndim != 1 or p.ndim != 1:
        raise ValueError("speed_mps and power_w must be one-dimensional")
    if v.size != p.size:
        raise ValueError("speed_mps and power_w must have equal length")
    if v.size < 2:
        raise ValueError("need at least 2 samples to fit 2 parameters")
    if np.unique(np.round(v, 6)).size < 2:
        raise ValueError("need at least 2 distinct speeds to separate CdA from Crr")
    if np.any(v <= 0.0):
        raise ValueError("all speeds must be positive")
    if mass_kg <= 0.0:
        raise ValueError("mass_kg must be positive")

    rho, density_note = resolve_air_density(
        air_density_kg_m3=air_density_kg_m3,
        pressure_pa=pressure_pa,
        temperature_c=temperature_c,
    )

    notes = [density_note]
    if power_is_mechanical:
        target = p
        notes.append("input power treated as mechanical power at the road")
    else:
        if drivetrain_efficiency is None:
            raise ValueError(
                "drivetrain_efficiency is required when power_is_mechanical is False"
            )
        if not 0.0 < drivetrain_efficiency <= 1.0:
            raise ValueError("drivetrain_efficiency must be in (0, 1]")
        target = p * drivetrain_efficiency
        notes.append(
            "input power treated as ELECTRICAL and multiplied by drivetrain_efficiency="
            f"{drivetrain_efficiency:g}; the caller must already have removed the auxiliary load"
        )

    design = np.column_stack([0.5 * rho * v**3, mass_kg * GRAVITY_M_S2 * v])
    solution = lsq_linear(design, target, bounds=(0.0, np.inf))
    cda, crr = (float(solution.x[0]), float(solution.x[1]))

    fitted = design @ solution.x
    residual = target - fitted
    residual_rms = float(np.sqrt(np.mean(residual**2)))
    variance = float(np.sum((target - target.mean()) ** 2))
    r_squared = 1.0 - float(np.sum(residual**2)) / variance if variance > 0.0 else float("nan")

    speed_span = float(v.max() - v.min())
    if speed_span < 5.0:
        notes.append(
            f"speed span is only {speed_span:.1f} m/s: CdA and Crr are poorly separated, "
            "treat the split between them as unreliable"
        )
    if crr <= 0.0 or cda <= 0.0:
        notes.append(
            "a fitted parameter hit the non-negativity bound: the data does not support "
            "this two-term model (check for gradient, wind or a speed-dependent aux load)"
        )

    provenance = Provenance(
        provider="rwi",
        dataset="vehicle.dynamics.fit_coastdown",
        kind=DataKind.DERIVED,
        retrieved_at_utc=now_utc if now_utc is not None else utc_now(),
        model_name="bounded linear least squares on P(v)=0.5*rho*CdA*v^3 + Crr*m*g*v",
        processing="scipy.optimize.lsq_linear, both parameters bounded >= 0",
        notes=" | ".join(notes),
    )

    return CoastdownFit(
        drag_area_cda_m2=cda,
        rolling_resistance_coefficient=crr,
        sample_count=int(v.size),
        residual_rms_w=residual_rms,
        r_squared=r_squared,
        air_density_kg_m3=rho,
        mass_kg=float(mass_kg),
        method="scipy.optimize.lsq_linear (non-negative bounded linear least squares)",
        notes=notes,
        provenance=provenance,
    )
