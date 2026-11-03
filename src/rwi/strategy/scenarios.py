"""Scenario engine: candidate driving strategies, compared and ranked.

This module is **decision support**. It evaluates "what if we drove at X" and
hands the strategists numbers and caveats. It issues no speed command, no
control signal and no instruction of any kind to the vehicle or the driver, and
nothing here is safe to connect to a control loop. Every output requires human
review before it influences the car.

How a scenario is evaluated
---------------------------
For each segment (see :mod:`rwi.strategy.segments`):

1. pick the speed - the scenario's constant target, or whatever its
   ``speed_profile`` returns for that segment;
2. apply the posted speed limit and the vehicle's legal ceiling as
   *constraints*, never as a silent correction: exceeding either is a
   ``blocking`` violation and the scenario is reported infeasible, because a
   strategy that breaks a speed limit must never be recommended;
3. compute the apparent wind with :func:`rwi.core.wind.relative_wind` and the
   electrical demand with :func:`rwi.vehicle.dynamics.consumption`;
4. integrate: ``dt = distance / speed``, consumption ``= P_total * dt``,
   generation ``= P_array * (dt + dwell)`` (the array keeps working at a
   control stop), plus the auxiliary load during the dwell;
5. push the net energy through :mod:`rwi.vehicle.battery`, which enforces the
   reserve and the capacity.

Assumptions that materially shape the answer
--------------------------------------------
* **Generation is attached to position, not to the clock.** A segment's array
  power is the number the caller supplied for that piece of road. Driving
  slower therefore collects *more* energy from the same sunshine, which is
  right for a short re-plan horizon and wrong for a whole day: in reality the
  sun moves, and a slower plan reaches each segment later and under a different
  sky. A time-consistent treatment means re-running the trajectory and the
  irradiance forecast per candidate speed (owned by :mod:`rwi.route` and
  :mod:`rwi.energy`) and feeding the result back in here. Until then, prefer
  short horizons, and read the generation numbers as "the sky as forecast for
  this stretch".
* **Quasi-static.** No acceleration energy, no braking losses beyond the
  regeneration policy, no traffic, no road trains, no overtaking, no
  convoy-imposed stops.
* Discretisation: constant conditions within a segment.

Validation status: implemented, not validated.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field
from scipy.optimize import brentq

from rwi.contracts import (
    Recommendation,
    ScenarioConstraintViolation,
    ScenarioResult,
    Trajectory,
    VehicleConfig,
)
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import utc_now
from rwi.core.units import kmh_to_ms, ms_to_kmh
from rwi.core.wind import relative_wind
from rwi.regulations import (
    REGULATIONS_VERSION,
    EventClass,
    RegulatoryFinding,
    check_energy_storage,
    check_speed,
)
from rwi.strategy.segments import (
    SegmentConditions,
    attach_generation,
    generation_index,
    scale_generation,
    scale_wind,
    segments_from_distance,
    segments_from_trajectory,
    total_distance_m,
)
from rwi.vehicle.battery import (
    DEFAULT_BATTERY_LIMITS,
    ENERGY_EPSILON_WH,
    BatteryLimits,
    BatteryState,
    apply_net_energy,
    state_from_vehicle,
    temperature_violations,
)
from rwi.vehicle.dynamics import RegenerationPolicy, consumption

__all__ = [
    "ScenarioEnsembleResult",
    "ScenarioSensitivity",
    "ScenarioSpec",
    "SpeedOptimisation",
    "SpeedProfile",
    "compare_scenarios",
    "evaluate_scenario",
    "optimal_constant_speed",
    "recommend",
    "scenario_cvar",
    "scenario_sensitivity",
    "standard_candidates",
    "weather_aware_profile",
]

SpeedProfile = Callable[[int, SegmentConditions, float], float]
"""``(segment_index, segment, target_speed_kmh) -> speed_kmh`` for that segment."""

AssumptionValue = str | float | bool

SAFETY_NOTES: Final[tuple[str, ...]] = (
    "This platform is decision support only. It issues NO vehicle commands: no speed, no "
    "throttle, no braking, no array or battery control. Every number here is advisory.",
    "Posted speed limits and the vehicle's legal speed ceiling are encoded as BLOCKING "
    "constraints. A scenario that exceeds either is reported infeasible and can never be the "
    "recommended scenario.",
    "The battery reserve is a team policy floor, also blocking. It exists because the state of "
    "charge is estimated, not measured, and because the model is unvalidated.",
    "Every recommendation requires human review by the strategy group before it affects the car. "
    "The driver and the observer remain responsible for the road, the traffic and the event "
    "regulations.",
    f"Regulatory limits are not asserted by this module: they come from rwi.regulations, which "
    f"cites the {REGULATIONS_VERSION} clause by clause. Violations name the clause (for example "
    "'regulation 2.5.2' for energy storage, 'regulation 3.31.6' for speed) so a strategist can "
    "look them up.",
    "Open regulatory questions the platform cannot resolve on its own: clause 3.17.2 gives the "
    "Challenger charging lock-out as 19:00-06:30 while clauses 3.28.8-9 give 19:15/06:15 for "
    "pack sealing - the regulations disagree and the organisers must be asked; and the official "
    "route notes, control-stop list and per-segment posted speed limits are not published yet, "
    "so posted limits must be supplied per segment by the team.",
)

_UNCERTAINTY_METHOD_NOTE: Final[str] = (
    "Uncertainty is a documented two-point sensitivity sweep, NOT a probability distribution: "
    "the pessimistic case scales every generation figure by (1 - f) and every wind speed by "
    "(1 + f); the optimistic case uses (1 + f) and (1 - f). Because a stronger tailwind helps, "
    "the wind part is a perturbation of the forecast magnitude, not a bound on the outcome. "
    "No confidence level can be attached to it."
)


# ---------------------------------------------------------------------------
# Scenario definition
# ---------------------------------------------------------------------------


class ScenarioSpec(BaseModel):
    """One candidate strategy: a name, a target speed, and optionally a profile."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: str
    description: str
    target_speed_kmh: float = Field(gt=0.0)
    speed_profile: SpeedProfile | None = Field(
        default=None,
        description="per-segment speed rule; None means hold target_speed_kmh everywhere",
    )
    assumptions: dict[str, AssumptionValue] = Field(default_factory=dict)


class ScenarioSensitivity(BaseModel):
    """A scenario under the base, pessimistic and optimistic cases."""

    model_config = ConfigDict(frozen=True)

    base: ScenarioResult
    pessimistic: ScenarioResult
    optimistic: ScenarioResult
    uncertainty_fraction: float = Field(ge=0.0)
    method_note: str = _UNCERTAINTY_METHOD_NOTE

    @property
    def robust(self) -> bool:
        """True only when the scenario survives the pessimistic case as well."""
        return self.base.feasible and self.pessimistic.feasible

    @property
    def end_soc_span(self) -> tuple[float, float]:
        return (self.pessimistic.end_soc_fraction, self.optimistic.end_soc_fraction)


class ScenarioEnsembleResult(BaseModel):
    """Scenario evaluated against a stack of ensemble member realisations.

    The team's own simulator (drive-system/.../python/simulation/robust_objective.py
    at :149) runs the scenario against several weather realisations and uses the
    upper CVaR as the objective to minimise. That is the right principle for a
    solar car - a strategy that wins on the median forecast and loses on the
    bad quartile is not a strategy you can race on - but their realisations are
    perturbations of a single point forecast. We have real ECMWF ensemble
    members in rwi.ensemble; running the scenario against them is honest in a
    way a perturbation is not.
    """

    model_config = ConfigDict(frozen=True)

    member_results: list[ScenarioResult]
    """One ScenarioResult per ensemble member fed through the model."""
    objective: Literal["net_energy_wh", "travel_time_s"] = Field(
        description="which quantity is treated as the loss (lower is worse)"
    )
    alpha: float = Field(
        gt=0.0, lt=1.0,
        description="worst-quartile fraction, usually 0.25; CVaR_alpha is the mean of "
                    "the alpha worst outcomes",
    )
    cvar_value: float = Field(
        description="mean of the loss over the worst alpha fraction of members "
                    "(lower is worse than cvar_median)"
    )
    median_value: float
    best_value: float
    worst_value: float
    member_count: int = Field(ge=1)
    feasible_count: int = Field(ge=0)
    method_note: str = _UNCERTAINTY_METHOD_NOTE


class SpeedOptimisation(BaseModel):
    """Result of :func:`optimal_constant_speed`."""

    model_config = ConfigDict(frozen=True)

    feasible: bool
    speed_kmh: float | None
    speed_cap_kmh: float
    min_speed_kmh: float
    travel_time_s: float | None = None
    end_soc_fraction: float | None = None
    method: str
    function_evaluations: int = Field(ge=0)
    scenario: ScenarioResult | None = None
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


SPEED_TOLERANCE_KMH: Final[float] = 1e-9
"""Allowance on speed-limit comparisons, so that planning to drive at exactly
the limit is not reported as exceeding it by a floating-point rounding error."""

FALLBACK_SCAN_POINTS: Final[int] = 128
"""Grid resolution of the optimiser's fallback scan (see `optimal_constant_speed`)."""

MEDIUM_CONFIDENCE_MARGIN_PP: Final[float] = 5.0
"""State-of-charge margin above the reserve, in percentage points, below which a
recommendation stays at ``"low"`` confidence even if the pessimistic case survives."""


def _as_float(value: AssumptionValue | None) -> float | None:
    """Read a numeric assumption, ignoring the string and bool entries."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _provenance(dataset: str, notes: str, now_utc: datetime | None) -> Provenance:
    return Provenance(
        provider="rwi",
        dataset=dataset,
        kind=DataKind.PHYSICAL_MODEL,
        retrieved_at_utc=now_utc if now_utc is not None else utc_now(),
        model_name="quasi-static longitudinal consumption + Wh energy accumulator",
        model_version="1",
        processing=(
            "per-segment integration; generation attached to position rather than clock time"
        ),
        notes=notes,
    )


def _resolve_segments(
    *,
    segments: Sequence[SegmentConditions] | None,
    trajectory: Trajectory | None,
    distance_m: float | None,
    segment_count: int,
) -> list[SegmentConditions]:
    supplied = [x is not None for x in (segments, trajectory, distance_m)]
    if sum(supplied) != 1:
        raise ValueError(
            "supply exactly one of segments=, trajectory= or distance_m= "
            "(they are three ways to describe the same leg)"
        )
    if segments is not None:
        if not segments:
            raise ValueError("segments must not be empty")
        return list(segments)
    if trajectory is not None:
        return segments_from_trajectory(trajectory)
    if distance_m is None:  # unreachable: narrowed by the check above
        raise ValueError("distance_m is required when no segments or trajectory are given")
    return segments_from_distance(distance_m, segment_count=segment_count)


def _compress(
    violations: Sequence[ScenarioConstraintViolation],
) -> list[ScenarioConstraintViolation]:
    """One entry per constraint, the first occurrence, with a count of repeats.

    A 120-segment leg that runs below the reserve reports one reserve violation
    saying "and 73 further segments", not 74 near-identical rows the
    strategists have to read.
    """
    first: dict[str, ScenarioConstraintViolation] = {}
    counts: dict[str, int] = {}
    for violation in violations:
        counts[violation.constraint] = counts.get(violation.constraint, 0) + 1
        first.setdefault(violation.constraint, violation)
    out: list[ScenarioConstraintViolation] = []
    for constraint, violation in first.items():
        extra = counts[constraint] - 1
        if extra > 0:
            out.append(
                violation.model_copy(
                    update={
                        "detail": (
                            f"{violation.detail} (first of {counts[constraint]} occurrences; "
                            f"{extra} further segment(s) affected)"
                        )
                    }
                )
            )
        else:
            out.append(violation)
    order = {"blocking": 0, "warning": 1}
    return sorted(out, key=lambda v: order[v.severity])


def _as_violation(
    finding: RegulatoryFinding, *, context: str = ""
) -> ScenarioConstraintViolation:
    """Turn a :class:`~rwi.regulations.RegulatoryFinding` into a scenario violation.

    The ``constraint`` field names the clause, so a strategist reading the
    output can see *which* regulation a candidate plan would break and look it
    up. Severity follows the regulations module: ``finding.blocking`` means the
    plan is not recommendable at all.
    """
    where = f" ({context})" if context else ""
    return ScenarioConstraintViolation(
        constraint=f"regulation {finding.clause}",
        detail=(
            f"{finding.detail}{where}. Source: clause {finding.clause} of the "
            f"{REGULATIONS_VERSION}, via rwi.regulations."
        ),
        severity="blocking" if finding.blocking else "warning",
    )


def _regulatory_speed_violations(
    speed_mps: float, *, posted_limit_kmh: float | None, context: str
) -> list[ScenarioConstraintViolation]:
    """Clause-citing violations for one cruising speed (3.31.6, 3.29.1).

    The posted limit is passed with a 1e-9 km/h allowance, matching the
    tolerance used by the physics-level checks. `rwi.regulations.check_speed`
    compares exactly, and a km/h -> m/s -> km/h round trip is not exact:
    `ms_to_kmh(kmh_to_ms(60.0))` is 60.00000000000001. Without the allowance,
    planning to drive at exactly the posted limit would be reported as a
    regulatory breach of 1e-14 km/h. The allowance is far below any real
    speed-holding accuracy and does not change the printed limit.
    """
    allowance = SPEED_TOLERANCE_KMH if posted_limit_kmh is not None else 0.0
    limit = posted_limit_kmh + allowance if posted_limit_kmh is not None else None
    return [
        _as_violation(finding, context=context)
        for finding in check_speed(speed_mps, posted_limit_kmh=limit)
    ]


def _perturb(
    segments: Sequence[SegmentConditions], fraction: float, *, pessimistic: bool
) -> list[SegmentConditions]:
    """Segments with generation and wind scaled for one sensitivity case.

    Pessimistic: generation x (1 - f), wind speed x (1 + f).
    Optimistic:  generation x (1 + f), wind speed x (1 - f).
    See :data:`_UNCERTAINTY_METHOD_NOTE` for why this is a sensitivity sweep
    and not a bound.
    """
    gen_factor = 1.0 - fraction if pessimistic else 1.0 + fraction
    wind_factor = 1.0 + fraction if pessimistic else 1.0 - fraction
    return scale_wind(
        scale_generation(segments, max(gen_factor, 0.0)), max(wind_factor, 0.0)
    )


def _speed_cap_kmh(
    vehicle: VehicleConfig, speed_limit_kmh: float | None, segment: SegmentConditions | None = None
) -> float:
    limits = [vehicle.max_legal_speed_kmh]
    if speed_limit_kmh is not None:
        limits.append(speed_limit_kmh)
    if segment is not None and segment.speed_limit_kmh is not None:
        limits.append(segment.speed_limit_kmh)
    return min(limits)


# ---------------------------------------------------------------------------
# evaluate_scenario
# ---------------------------------------------------------------------------


def evaluate_scenario(  # one explicit, readable integration loop; do not split for metrics
    *,
    name: str,
    description: str,
    target_speed_kmh: float,
    vehicle: VehicleConfig,
    start_soc_fraction: float,
    segments: Sequence[SegmentConditions] | None = None,
    trajectory: Trajectory | None = None,
    distance_m: float | None = None,
    segment_count: int = 1,
    generation: Sequence[Any] | float | None = None,
    generation_as_energy_wh: bool = False,
    speed_profile: SpeedProfile | None = None,
    start_time_utc: datetime | None = None,
    deadline_utc: datetime | None = None,
    speed_limit_kmh: float | None = None,
    power_limit_w: float | None = None,
    power_limit_severity: Literal["blocking", "warning"] = "blocking",
    battery_temperature_c: float | None = None,
    battery_limits: BatteryLimits = DEFAULT_BATTERY_LIMITS,
    regeneration: RegenerationPolicy = "none",
    event_class: EventClass = EventClass.CHALLENGER,
    uncertainty_fraction: float = 0.0,
    extra_assumptions: Mapping[str, AssumptionValue] | None = None,
    case: str = "base",
    now_utc: datetime | None = None,
) -> ScenarioResult:
    """Integrate one candidate strategy over a leg and check every constraint.

    The leg is described in exactly one of three ways: ready-made ``segments``,
    a ``trajectory`` (geometry and gradient only - its speeds and ETAs are not
    used), or a plain ``distance_m`` split into ``segment_count`` identical
    pieces.

    Parameters
    ----------
    target_speed_kmh:
        The scenario's nominal speed. Held on every segment unless
        ``speed_profile`` overrides it.
    start_soc_fraction:
        Battery state of charge at the start, 0-1, as a fraction of nameplate
        capacity.
    generation:
        Convenience: a generation series (``PowerEstimate`` objects, watts, or a
        single scalar) attached to the resolved segments. With
        ``generation_as_energy_wh`` the numbers are Wh per segment. Generation
        already present on the segments is used when this is None.
    speed_limit_kmh, power_limit_w:
        Leg-wide limits; per-segment limits on the segments take precedence
        where they are lower (speed) or also apply (power).
    power_limit_severity:
        ``"blocking"`` by default: a demand above a stated power limit means the
        scenario is not deliverable by the hardware, so it must not be
        recommended. Set it to ``"warning"`` when the limit is advisory (for
        instance a self-imposed current cap).
    deadline_utc:
        Arriving later than this is a ``warning``, not blocking: being late is
        a competitive loss, and the platform must not pretend that a late plan
        is forbidden. Requires ``start_time_utc`` to be meaningful.
    uncertainty_fraction:
        When > 0, the pessimistic and optimistic cases of
        :func:`scenario_sensitivity` are run and their outcomes recorded in
        ``assumptions`` and ``uncertainty_notes``.
    case:
        Label for a sensitivity run; ``"base"`` is the real evaluation. Used
        internally to stop the sweep recursing.
    now_utc:
        Timestamp written into the provenance. **Supply it for reproducible
        output** - it is the only wall-clock dependence in this function.

    Returns
    -------
    ScenarioResult
        ``feasible`` is False if and only if at least one ``blocking``
        violation was raised. Energy figures are Wh at the battery terminals;
        ``end_soc_fraction`` is clamped to the physical 0-100 % range, with any
        unmet demand reported as ``energy_shortfall_wh`` in ``assumptions`` and
        in the ``battery_empty`` violation.
    """
    if target_speed_kmh <= 0.0:
        raise ValueError("target_speed_kmh must be positive")
    if not 0.0 <= start_soc_fraction <= 1.0:
        raise ValueError("start_soc_fraction must be in [0, 1]")
    if uncertainty_fraction < 0.0:
        raise ValueError("uncertainty_fraction must be non-negative")

    resolved = _resolve_segments(
        segments=segments,
        trajectory=trajectory,
        distance_m=distance_m,
        segment_count=segment_count,
    )
    if generation is not None:
        resolved = attach_generation(resolved, generation, as_energy_wh=generation_as_energy_wh)

    violations: list[ScenarioConstraintViolation] = []

    # --- legal speed, checked before anything is integrated ---------------
    if target_speed_kmh > vehicle.max_legal_speed_kmh + 1e-9:
        violations.append(
            ScenarioConstraintViolation(
                constraint="legal_speed",
                detail=(
                    f"target speed {target_speed_kmh:.1f} km/h exceeds the vehicle's legal "
                    f"ceiling of {vehicle.max_legal_speed_kmh:.1f} km/h. The platform never "
                    "recommends breaking a speed limit."
                ),
                severity="blocking",
            )
        )
    if speed_limit_kmh is not None and target_speed_kmh > speed_limit_kmh + 1e-9:
        violations.append(
            ScenarioConstraintViolation(
                constraint="speed_limit",
                detail=(
                    f"target speed {target_speed_kmh:.1f} km/h exceeds the supplied speed limit "
                    f"of {speed_limit_kmh:.1f} km/h. The platform never recommends breaking a "
                    "speed limit."
                ),
                severity="blocking",
            )
        )

    # --- regulatory configuration check: is this even a legal car? ---------
    storage_finding = check_energy_storage(vehicle.battery_capacity_wh, event_class)
    if storage_finding is not None:
        violations.append(
            _as_violation(
                storage_finding,
                context=f"vehicle {vehicle.name!r}, parameter_source={vehicle.parameter_source}",
            )
        )

    battery: BatteryState = state_from_vehicle(
        vehicle, soc_fraction=start_soc_fraction, temperature_c=battery_temperature_c
    )
    start_soc = battery.soc_fraction
    reserve_wh = battery.reserve_wh
    violations.extend(temperature_violations(battery, limits=battery_limits, context="at start"))

    unclamped_wh = battery.energy_wh
    consumed_wh = 0.0
    generated_wh = 0.0
    travel_time_s = 0.0
    dwell_total_s = 0.0
    shortfall_wh = 0.0
    spilled_wh = 0.0
    min_soc = battery.soc_fraction
    demand_time_product = 0.0
    speeds_kmh: list[float] = []

    for index, segment in enumerate(resolved):
        cap = _speed_cap_kmh(vehicle, speed_limit_kmh, segment)
        raw_speed_kmh = (
            speed_profile(index, segment, target_speed_kmh)
            if speed_profile is not None
            else target_speed_kmh
        )
        if raw_speed_kmh <= 0.0:
            raise ValueError(
                f"segment {index}: speed {raw_speed_kmh} km/h is not positive; a stopped "
                "vehicle never finishes the leg (use dwell_s for planned stops)"
            )
        if raw_speed_kmh > cap + 1e-9:
            violations.append(
                ScenarioConstraintViolation(
                    constraint="speed_limit",
                    detail=(
                        f"segment {index} ({segment.label or 'unnamed'}): planned speed "
                        f"{raw_speed_kmh:.1f} km/h exceeds the applicable limit of "
                        f"{cap:.1f} km/h. The platform never recommends breaking a speed limit."
                    ),
                    severity="blocking",
                )
            )
        speed_kmh = raw_speed_kmh
        speeds_kmh.append(speed_kmh)
        speed_mps = kmh_to_ms(speed_kmh)

        # Clause-citing regulatory findings for this speed, alongside the
        # physics checks above. `posted` is the POSTED limit only: the
        # vehicle's own ceiling is not a road sign, and an unknown posted limit
        # produces no upper-bound finding rather than an assumed one.
        posted = segment.speed_limit_kmh if segment.speed_limit_kmh is not None else speed_limit_kmh
        violations.extend(
            _regulatory_speed_violations(
                speed_mps,
                posted_limit_kmh=posted,
                context=f"segment {index} ({segment.label or 'unnamed'})",
            )
        )

        wind = relative_wind(
            vehicle_speed_mps=speed_mps,
            vehicle_heading_deg=segment.heading_deg,
            wind_speed_mps=segment.wind_speed_mps,
            wind_from_deg=segment.wind_from_deg,
        )
        estimate = consumption(
            vehicle,
            speed_mps=speed_mps,
            grade=segment.grade,
            air_density_kg_m3=segment.air_density_kg_m3,
            relative_wind=wind,
            pressure_pa=segment.pressure_pa,
            temperature_c=segment.temperature_c,
            regeneration=regeneration,
            now_utc=now_utc,
        )

        dt_s = segment.distance_m / speed_mps
        travel_time_s += dt_s + segment.dwell_s
        dwell_total_s += segment.dwell_s
        demand_time_product += estimate.total_power_w * dt_s

        segment_consumed_wh = estimate.total_power_w * dt_s / 3600.0
        segment_consumed_wh += vehicle.auxiliary_power_w * segment.dwell_s / 3600.0
        if segment.generation_wh is not None:
            segment_generated_wh = segment.generation_wh
        elif segment.generation_w is not None:
            segment_generated_wh = segment.generation_w * (dt_s + segment.dwell_s) / 3600.0
        else:
            segment_generated_wh = 0.0

        consumed_wh += segment_consumed_wh
        generated_wh += segment_generated_wh

        limit = segment.power_limit_w if segment.power_limit_w is not None else power_limit_w
        if limit is not None and estimate.total_power_w > limit + 1e-9:
            violations.append(
                ScenarioConstraintViolation(
                    constraint="power_limit",
                    detail=(
                        f"segment {index} ({segment.label or 'unnamed'}): demand "
                        f"{estimate.total_power_w:.0f} W exceeds the stated limit of "
                        f"{limit:.0f} W at {speed_kmh:.1f} km/h "
                        f"(apparent headwind {wind.headwind_mps:.1f} m/s, grade "
                        f"{segment.grade * 100:.2f} %)."
                    ),
                    severity=power_limit_severity,
                )
            )

        net_wh = segment_generated_wh - segment_consumed_wh
        transition = apply_net_energy(
            battery,
            net_wh,
            limits=battery_limits,
            context=f"segment {index} at {segment.distance_m / 1000.0:.1f} km of leg",
        )
        battery = transition.state
        violations.extend(transition.violations)
        shortfall_wh += transition.shortfall_wh
        spilled_wh += transition.spilled_wh
        # Unclamped trail: the same arithmetic without the 0/capacity clamps, so
        # that the optimiser has a smooth, monotone constraint function.
        unclamped_wh += net_wh * battery.round_trip_efficiency if net_wh >= 0.0 else net_wh
        min_soc = min(min_soc, battery.soc_fraction)

    distance = total_distance_m(resolved)
    arrival_utc = (
        start_time_utc + timedelta(seconds=travel_time_s) if start_time_utc is not None else None
    )
    if deadline_utc is not None and arrival_utc is not None and arrival_utc > deadline_utc:
        late_s = (arrival_utc - deadline_utc).total_seconds()
        violations.append(
            ScenarioConstraintViolation(
                constraint="arrival_deadline",
                detail=(
                    f"arrival {arrival_utc.isoformat()} is {late_s / 60.0:.0f} min after the "
                    f"deadline {deadline_utc.isoformat()}. Reported as a warning, not a "
                    "blocking constraint: being late is a competitive cost, not a rule the "
                    "platform may assume."
                ),
                severity="warning",
            )
        )
    if deadline_utc is not None and start_time_utc is None:
        violations.append(
            ScenarioConstraintViolation(
                constraint="arrival_deadline",
                detail=(
                    "a deadline was supplied but no start_time_utc, so no arrival time could be "
                    "computed and the deadline was NOT checked."
                ),
                severity="warning",
            )
        )

    compressed = _compress(violations)
    feasible = not any(v.severity == "blocking" for v in compressed)

    mean_demand_w = demand_time_product / (travel_time_s - dwell_total_s) if travel_time_s else 0.0
    assumptions: dict[str, AssumptionValue] = {
        "case": case,
        "segment_count": float(len(resolved)),
        "speed_rule": "per-segment profile" if speed_profile is not None else "constant target",
        "mean_speed_kmh": float(sum(speeds_kmh) / len(speeds_kmh)) if speeds_kmh else 0.0,
        "min_speed_kmh": float(min(speeds_kmh)) if speeds_kmh else 0.0,
        "max_speed_kmh": float(max(speeds_kmh)) if speeds_kmh else 0.0,
        "regeneration_policy": regeneration,
        "generation_attribution": (
            "per-segment mean array power held constant over the segment and tied to POSITION, "
            "not to clock time; a slower plan therefore collects more energy from the same "
            "forecast, which is only valid over a short horizon"
        ),
        "mean_demand_w": mean_demand_w,
        "reserve_floor_wh": reserve_wh,
        "reserve_soc_fraction": float(vehicle.battery_reserve_fraction),
        "battery_capacity_wh": float(vehicle.battery_capacity_wh),
        "min_soc_fraction": min_soc,
        "end_energy_unclamped_wh": unclamped_wh,
        "energy_shortfall_wh": shortfall_wh,
        "energy_curtailed_wh": spilled_wh,
        "dwell_s_total": dwell_total_s,
        "vehicle_name": vehicle.name,
        "vehicle_parameter_source": vehicle.parameter_source,
        "event_class": event_class.value,
        "regulations_version": REGULATIONS_VERSION,
        "battery_round_trip_efficiency_applied": "charge side only, once",
        "uncertainty_fraction": float(uncertainty_fraction),
        "quasi_static": True,
        "inertial_term_included": False,
        "traffic_modelled": False,
    }
    if extra_assumptions:
        assumptions.update(extra_assumptions)

    uncertainty_notes: list[str] = [
        "Vehicle parameters are "
        f"{vehicle.parameter_source}: "
        + (
            "every consumption figure below is an illustration, not a prediction."
            if vehicle.parameter_source == "assumed"
            else "consumption figures inherit the accuracy of those parameters."
        ),
        "The consumption and battery models are implemented but NOT validated against any "
        "measurement of this or any other vehicle.",
        "Generation is tied to position rather than clock time (see assumptions): comparisons "
        "between widely different speeds over a long horizon are optimistic for the slow ones.",
        "No acceleration, braking, traffic, road-train or convoy effects are modelled.",
    ]
    if spilled_wh > ENERGY_EPSILON_WH:
        uncertainty_notes.append(
            f"{spilled_wh:.0f} Wh of generation was curtailed because the pack hit capacity; a "
            "faster plan, or charging later, would capture some of it."
        )

    if case == "base" and uncertainty_fraction > 0.0:
        shared: dict[str, Any] = {
            "name": name,
            "description": description,
            "target_speed_kmh": target_speed_kmh,
            "vehicle": vehicle,
            "start_soc_fraction": start_soc_fraction,
            "speed_profile": speed_profile,
            "start_time_utc": start_time_utc,
            "deadline_utc": deadline_utc,
            "speed_limit_kmh": speed_limit_kmh,
            "power_limit_w": power_limit_w,
            "power_limit_severity": power_limit_severity,
            "battery_temperature_c": battery_temperature_c,
            "battery_limits": battery_limits,
            "regeneration": regeneration,
            "event_class": event_class,
            "extra_assumptions": extra_assumptions,
            "now_utc": now_utc,
        }
        pessimistic = evaluate_scenario(
            segments=_perturb(resolved, uncertainty_fraction, pessimistic=True),
            uncertainty_fraction=0.0,
            case="pessimistic",
            **shared,
        )
        optimistic = evaluate_scenario(
            segments=_perturb(resolved, uncertainty_fraction, pessimistic=False),
            uncertainty_fraction=0.0,
            case="optimistic",
            **shared,
        )
        assumptions.update(
            {
                "pessimistic_end_soc_fraction": pessimistic.end_soc_fraction,
                "optimistic_end_soc_fraction": optimistic.end_soc_fraction,
                "pessimistic_feasible": pessimistic.feasible,
                "optimistic_feasible": optimistic.feasible,
                "pessimistic_net_energy_wh": pessimistic.net_energy_wh,
                "optimistic_net_energy_wh": optimistic.net_energy_wh,
            }
        )
        uncertainty_notes.append(_UNCERTAINTY_METHOD_NOTE)
        uncertainty_notes.append(
            f"Sensitivity at f={uncertainty_fraction:.0%}: end SoC "
            f"{pessimistic.end_soc_fraction:.1%} (pessimistic) to "
            f"{optimistic.end_soc_fraction:.1%} (optimistic); pessimistic case is "
            f"{'still feasible' if pessimistic.feasible else 'NOT feasible'}."
        )
    elif case == "base":
        uncertainty_notes.append(
            "No uncertainty sweep was run (uncertainty_fraction=0), so the robustness of this "
            "result to forecast error is unknown."
        )

    return ScenarioResult(
        name=name,
        description=description,
        target_speed_kmh=target_speed_kmh,
        distance_m=distance,
        energy_generated_wh=generated_wh,
        energy_consumed_wh=consumed_wh,
        net_energy_wh=generated_wh - consumed_wh,
        start_soc_fraction=start_soc,
        end_soc_fraction=battery.soc_fraction,
        arrival_utc=arrival_utc,
        travel_time_s=travel_time_s,
        feasible=feasible,
        violations=compressed,
        assumptions=assumptions,
        uncertainty_notes=uncertainty_notes,
        provenance=_provenance(
            "strategy.scenarios.evaluate_scenario",
            notes=(
                f"case={case}; decision support only, no vehicle commands; "
                f"vehicle.parameter_source={vehicle.parameter_source}"
            ),
            now_utc=now_utc,
        ),
    )


def scenario_sensitivity(
    *,
    uncertainty_fraction: float,
    segments: Sequence[SegmentConditions] | None = None,
    **kwargs: Any,
) -> ScenarioSensitivity:
    """Run a scenario under the base, pessimistic and optimistic cases.

    The sweep is deliberately crude and completely explicit: generation is
    scaled by ``1 -/+ f`` and wind speed by ``1 +/- f``. It is a sensitivity
    study, not a probabilistic forecast, and no confidence level may be derived
    from it. See :data:`_UNCERTAINTY_METHOD_NOTE`.

    ``segments`` is required here (a sweep must perturb concrete segments), and
    the remaining keyword arguments are those of :func:`evaluate_scenario`.
    """
    if uncertainty_fraction < 0.0:
        raise ValueError("uncertainty_fraction must be non-negative")
    if segments is None:
        raise ValueError("scenario_sensitivity needs explicit segments= to perturb")
    base_segments = list(segments)
    base = evaluate_scenario(
        segments=base_segments, uncertainty_fraction=0.0, case="base", **kwargs
    )
    pessimistic = evaluate_scenario(
        segments=_perturb(base_segments, uncertainty_fraction, pessimistic=True),
        uncertainty_fraction=0.0,
        case="pessimistic",
        **kwargs,
    )
    optimistic = evaluate_scenario(
        segments=_perturb(base_segments, uncertainty_fraction, pessimistic=False),
        uncertainty_fraction=0.0,
        case="optimistic",
        **kwargs,
    )
    return ScenarioSensitivity(
        base=base,
        pessimistic=pessimistic,
        optimistic=optimistic,
        uncertainty_fraction=uncertainty_fraction,
    )


def scenario_cvar(
    *,
    segments: Sequence[SegmentConditions],
    ghi_members_w_m2: Sequence[Sequence[float]] | None = None,
    wind_members_mps: Sequence[Sequence[float]] | None = None,
    alpha: float = 0.25,
    objective: Literal["net_energy_wh", "travel_time_s"] = "net_energy_wh",
    **kwargs: Any,
) -> ScenarioEnsembleResult:
    """Evaluate a scenario against ensemble members and report its CVaR.

    This is the principle the team's own simulator uses: a strategy that wins on
    the median forecast and fails on the bad quartile has not actually won.
    CVaR (conditional value at risk) at level ``alpha`` is the mean of the
    worst ``alpha`` fraction of outcomes. A plan whose median net energy is
    +600 Wh but whose CVaR_0.25 is -800 Wh is a plan with a tail that must be
    priced in.

    The members are supplied as per-segment arrays, one row per ensemble
    member. If both ``ghi_members_w_m2`` and ``wind_members_mps`` are None, the
    function raises - a single realisation gives no CVaR information and the
    caller probably meant :func:`scenario_sensitivity`.

    This function returns the member results so a caller can show the fan, not
    just the single number.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    if ghi_members_w_m2 is None and wind_members_mps is None:
        raise ValueError(
            "scenario_cvar needs at least one of ghi_members_w_m2 or wind_members_mps; "
            "with a single realisation use scenario_sensitivity instead"
        )

    # Normalise to a common member count.
    sources = [m for m in (ghi_members_w_m2, wind_members_mps) if m is not None]
    counts = {len(m) for m in sources}
    if len(counts) != 1:
        raise ValueError(
            f"ensemble member counts disagree: {sorted(counts)} - supply the same "
            "number of members for every variable"
        )
    member_count = counts.pop()
    if member_count < 2:
        raise ValueError("need at least 2 ensemble members to compute a CVaR")

    seg_count = len(segments)
    for name, arrays in (
        ("ghi_members_w_m2", ghi_members_w_m2),
        ("wind_members_mps", wind_members_mps),
    ):
        if arrays is None:
            continue
        for i, row in enumerate(arrays):
            if len(row) != seg_count:
                raise ValueError(
                    f"{name}[{i}] has {len(row)} segments but there are {seg_count}; "
                    "each member must cover every segment"
                )

    member_results: list[ScenarioResult] = []
    for member in range(member_count):
        per_segment = []
        for seg_idx, seg in enumerate(segments):
            overrides: dict[str, Any] = {}
            if ghi_members_w_m2 is not None:
                # Rescale generation by this member's GHI relative to the segment's own.
                ratio = _generation_scale(seg, ghi_members_w_m2[member][seg_idx])
                if ratio is not None and seg.generation_w is not None:
                    overrides["generation_w"] = seg.generation_w * ratio
                elif ratio is not None and seg.generation_wh is not None:
                    overrides["generation_wh"] = seg.generation_wh * ratio
            if wind_members_mps is not None:
                overrides["wind_speed_mps"] = float(wind_members_mps[member][seg_idx])
            per_segment.append(seg.model_copy(update=overrides) if overrides else seg)
        result = evaluate_scenario(
            segments=per_segment,
            uncertainty_fraction=0.0,
            case=f"ensemble_member_{member}",
            name=f"ensemble member {member}",
            description="ensemble CVaR evaluation",
            **kwargs,
        )
        member_results.append(result)

    def _loss(r: ScenarioResult) -> float:
        if objective == "net_energy_wh":
            return -r.net_energy_wh  # lower is worse -> loss is -net_energy
        return r.travel_time_s

    losses = sorted(_loss(r) for r in member_results)
    k = max(1, round(len(losses) * alpha))
    worst_losses = losses[-k:]
    cvar_loss = sum(worst_losses) / len(worst_losses)
    median_loss = losses[len(losses) // 2]

    def _back(x: float) -> float:
        return -x if objective == "net_energy_wh" else x

    return ScenarioEnsembleResult(
        member_results=member_results,
        objective=objective,
        alpha=alpha,
        cvar_value=_back(cvar_loss),
        median_value=_back(median_loss),
        best_value=_back(losses[0]),
        worst_value=_back(losses[-1]),
        member_count=member_count,
        feasible_count=sum(1 for r in member_results if r.feasible),
    )


def _generation_scale(seg: SegmentConditions, member_ghi_w_m2: float) -> float | None:
    """Multiplicative factor to rescale a segment's generation by a member GHI.

    Uses :func:`generation_index` as the reference GHI if generation_w was
    directly supplied; otherwise returns None and leaves generation untouched.
    This is a first-order approximation that treats array power as linear in
    GHI (which it approximately is away from the clear-sky limit).
    """
    reference = generation_index(seg)
    if reference is None or reference == 0.0:
        return None
    return float(member_ghi_w_m2) / float(reference)


# ---------------------------------------------------------------------------
# Candidate sets
# ---------------------------------------------------------------------------


def weather_aware_profile(
    segments: Sequence[SegmentConditions],
    *,
    headwind_sensitivity: float = 0.5,
    generation_sensitivity: float = 0.2,
    min_speed_kmh: float = 50.0,
    max_speed_kmh: float,
) -> SpeedProfile:
    """A documented *heuristic* speed rule. Not an optimum.

    The rule, in full::

        v_seg = clamp( (v_target - k_w * w_head_kmh) * (1 + k_g * (g_seg/g_ref - 1)),
                       min_speed, max_speed )

    where ``w_head_kmh`` is the along-track component of the **true** wind in
    km/h (positive into the nose, obtained from
    :func:`rwi.core.wind.relative_wind` evaluated at zero vehicle speed, so it
    is the wind's own contribution and not the vehicle's), ``g_seg`` is the
    segment's generation index and ``g_ref`` the mean over all segments.

    Why this shape:

    * **Slower into a headwind, faster with a tailwind.** Drag power scales
      with the apparent airspeed squared times the ground speed, so a headwind
      is the most expensive place to buy speed and a tailwind the cheapest.
      ``k_w = 0.5`` gives up half the headwind in speed and keeps the rest.
    * **Faster when the sun is strong, slower when it is weak**, by up to
      ``k_g`` of the target.

    Why it is a heuristic and not an optimum: the true optimum couples all
    segments through the state of charge and depends on the whole remaining
    forecast, which is a dynamic-programming or model-predictive-control
    problem (see :func:`optimal_constant_speed` and
    ``docs/science/vehicle.md``). The gains ``k_w`` and ``k_g`` are chosen for
    plausibility, have not been tuned against anything, and must not be read as
    optimal. The rule is deterministic and depends only on its inputs.
    """
    if headwind_sensitivity < 0.0 or generation_sensitivity < 0.0:
        raise ValueError("sensitivities must be non-negative")
    if min_speed_kmh <= 0.0 or max_speed_kmh <= 0.0:
        raise ValueError("speed bounds must be positive")
    if min_speed_kmh > max_speed_kmh:
        raise ValueError("min_speed_kmh must not exceed max_speed_kmh")

    indices = [generation_index(s) for s in segments]
    known = [i for i in indices if i is not None]
    reference = sum(known) / len(known) if known else None

    def profile(index: int, segment: SegmentConditions, target_speed_kmh: float) -> float:
        head_mps = relative_wind(
            vehicle_speed_mps=0.0,
            vehicle_heading_deg=segment.heading_deg,
            wind_speed_mps=segment.wind_speed_mps,
            wind_from_deg=segment.wind_from_deg,
        ).headwind_mps
        speed = target_speed_kmh - headwind_sensitivity * ms_to_kmh(head_mps)
        own = generation_index(segment)
        if reference is not None and reference > 0.0 and own is not None:
            speed *= 1.0 + generation_sensitivity * (own / reference - 1.0)
        return min(max(speed, min_speed_kmh), max_speed_kmh)

    return profile


def standard_candidates(
    *,
    vehicle: VehicleConfig,
    segments: Sequence[SegmentConditions],
    baseline_speed_kmh: float,
    speed_limit_kmh: float | None = None,
    conservative_margin_kmh: float = 10.0,
    faster_margin_kmh: float = 10.0,
    min_speed_kmh: float = 50.0,
    headwind_sensitivity: float = 0.5,
    generation_sensitivity: float = 0.2,
) -> list[ScenarioSpec]:
    """The four candidates named in the project brief.

    ``conservative`` - ``baseline - conservative_margin_kmh``: buy energy
    margin with time.
    ``balanced`` - the baseline speed, the reference everything else is
    compared against.
    ``faster-within-limits`` - ``baseline + faster_margin_kmh``, **capped** at
    the smallest applicable legal limit (the vehicle ceiling, the leg limit and
    every segment limit). The cap is applied when the candidate is *built*, so
    this candidate never proposes an illegal speed; a scenario that does is
    still reported infeasible by :func:`evaluate_scenario`.
    ``weather-aware adaptive`` - the baseline speed modulated per segment by
    :func:`weather_aware_profile`. Its ``target_speed_kmh`` is the baseline the
    profile modulates, not the speed actually driven anywhere.

    All four are candidates for a human to choose between. The speeds and
    margins are defaults chosen for plausibility, not optimised values.
    """
    if baseline_speed_kmh <= 0.0:
        raise ValueError("baseline_speed_kmh must be positive")
    if not segments:
        raise ValueError("segments must not be empty")

    cap = min(_speed_cap_kmh(vehicle, speed_limit_kmh, s) for s in segments)
    balanced = min(baseline_speed_kmh, cap)
    conservative = max(min(baseline_speed_kmh - conservative_margin_kmh, cap), 1.0)
    faster = min(baseline_speed_kmh + faster_margin_kmh, cap)

    adaptive_profile = weather_aware_profile(
        segments,
        headwind_sensitivity=headwind_sensitivity,
        generation_sensitivity=generation_sensitivity,
        min_speed_kmh=min(min_speed_kmh, cap),
        max_speed_kmh=cap,
    )

    return [
        ScenarioSpec(
            name="conservative",
            description=(
                f"Hold {conservative:.0f} km/h, {conservative_margin_kmh:.0f} km/h below the "
                f"{baseline_speed_kmh:.0f} km/h baseline, to protect state of charge."
            ),
            target_speed_kmh=conservative,
            assumptions={"candidate_role": "energy margin first"},
        ),
        ScenarioSpec(
            name="balanced",
            description=(
                f"Hold the {balanced:.0f} km/h baseline; the reference the others are "
                "compared against."
            ),
            target_speed_kmh=balanced,
            assumptions={"candidate_role": "baseline"},
        ),
        ScenarioSpec(
            name="faster-within-limits",
            description=(
                f"Hold {faster:.0f} km/h: baseline + {faster_margin_kmh:.0f} km/h, capped at the "
                f"smallest applicable legal limit ({cap:.0f} km/h)."
            ),
            target_speed_kmh=faster,
            assumptions={"candidate_role": "time first", "legal_speed_cap_kmh": cap},
        ),
        ScenarioSpec(
            name="weather-aware adaptive",
            description=(
                f"Modulate the {balanced:.0f} km/h baseline per segment: slower into a headwind "
                f"(give up {headwind_sensitivity:.0%} of the along-track wind), faster with a "
                f"tailwind, and +/-{generation_sensitivity:.0%} with the segment's generation "
                "relative to the leg mean. A documented HEURISTIC, not an optimum."
            ),
            target_speed_kmh=balanced,
            speed_profile=adaptive_profile,
            assumptions={
                "candidate_role": "weather-aware heuristic",
                "headwind_sensitivity": headwind_sensitivity,
                "generation_sensitivity": generation_sensitivity,
                "heuristic_not_optimum": True,
            },
        ),
    ]


def compare_scenarios(
    specs: Sequence[ScenarioSpec],
    *,
    vehicle: VehicleConfig,
    start_soc_fraction: float,
    segments: Sequence[SegmentConditions] | None = None,
    trajectory: Trajectory | None = None,
    distance_m: float | None = None,
    **kwargs: Any,
) -> list[ScenarioResult]:
    """Evaluate every candidate against identical conditions, in the given order.

    The shared keyword arguments are those of :func:`evaluate_scenario`. Each
    spec contributes its name, description, target speed, speed profile and its
    own extra assumptions. Order is preserved so that a caller can treat the
    first entry as the baseline.
    """
    if not specs:
        raise ValueError("specs must not be empty")
    results: list[ScenarioResult] = []
    for spec in specs:
        results.append(
            evaluate_scenario(
                name=spec.name,
                description=spec.description,
                target_speed_kmh=spec.target_speed_kmh,
                speed_profile=spec.speed_profile,
                vehicle=vehicle,
                start_soc_fraction=start_soc_fraction,
                segments=segments,
                trajectory=trajectory,
                distance_m=distance_m,
                extra_assumptions=spec.assumptions,
                **kwargs,
            )
        )
    return results


# ---------------------------------------------------------------------------
# The optimiser
# ---------------------------------------------------------------------------


def optimal_constant_speed(
    *,
    vehicle: VehicleConfig,
    start_soc_fraction: float,
    segments: Sequence[SegmentConditions] | None = None,
    trajectory: Trajectory | None = None,
    distance_m: float | None = None,
    segment_count: int = 1,
    min_speed_kmh: float = 30.0,
    max_speed_kmh: float | None = None,
    speed_limit_kmh: float | None = None,
    tolerance_kmh: float = 0.01,
    name: str = "time-optimal constant speed",
    description: str = "",
    now_utc: datetime | None = None,
    **kwargs: Any,
) -> SpeedOptimisation:
    """Fastest **constant** speed that still finishes above the battery reserve.

    Formulation
    -----------
    Minimise travel time ``T(v) = D / v`` subject to

    * ``SoC_finish(v) >= reserve``,
    * ``v <= min(vehicle.max_legal_speed_kmh, speed_limit_kmh, every segment limit)``,
    * ``v >= min_speed_kmh``.

    ``T(v)`` is strictly decreasing in ``v``, so the optimum is the **largest
    feasible speed**: either the legal cap, or the speed at which the energy
    constraint becomes exactly active. The second case is a root of

        ``h(v) = E_finish(v) - E_reserve``

    where ``E_finish`` is the unclamped finish energy reported by
    :func:`evaluate_scenario` (unclamped so that ``h`` stays smooth instead of
    flattening out at an empty pack).

    Why ``brentq`` and not ``minimize_scalar`` with a penalty: the problem has
    one scalar variable, a monotone objective and a monotone constraint, so the
    optimum is a *boundary*, and locating a boundary is root-finding. ``brentq``
    is bracketed, derivative-free, guaranteed to converge inside a sign-change
    bracket, and gives a speed accurate to ``tolerance_kmh`` in a handful of
    evaluations. A penalised ``minimize_scalar`` would need a penalty weight
    chosen by hand, could return a slightly infeasible point, and would confuse
    "the constraint is active" with "the optimiser stopped early". The two
    saturated cases (the cap is feasible, or even ``min_speed_kmh`` is not) are
    handled explicitly before any root-finding, and the returned speed is
    re-evaluated and verified feasible before it is reported.

    ``h`` is assumed monotone decreasing in ``v``. That is what the physics
    gives for a fixed generation field (drag grows faster than linearly, while
    the time spent collecting energy shrinks), but it is **not proven** here;
    only the endpoints are checked. A test benchmarks the result against a
    brute-force scan over speeds.

    Limits
    ------
    This is a **constant-speed optimum**, not an optimal speed profile. It
    cannot trade speed between segments, cannot time a cloud, cannot place a
    stop, and cannot exploit a hill. Doing that properly is a dynamic
    programming or model-predictive-control problem over state of charge,
    position and time, with the forecast and its uncertainty as inputs. That is
    future work and is listed as such in ``docs/science/vehicle.md``.
    """
    resolved = _resolve_segments(
        segments=segments,
        trajectory=trajectory,
        distance_m=distance_m,
        segment_count=segment_count,
    )
    cap = min(_speed_cap_kmh(vehicle, speed_limit_kmh, s) for s in resolved)
    if min_speed_kmh <= 0.0:
        raise ValueError("min_speed_kmh must be positive")
    notes: list[str] = [
        "Constant-speed optimum only: no dynamic programming, no speed profile, no cloud "
        "timing, no traffic.",
        "Monotonicity of the energy constraint in speed is assumed and only checked at the "
        "endpoints.",
    ]
    if min_speed_kmh > cap:
        return SpeedOptimisation(
            feasible=False,
            speed_kmh=None,
            speed_cap_kmh=cap,
            min_speed_kmh=min_speed_kmh,
            method="no search: the speed window is empty",
            function_evaluations=0,
            notes=[
                *notes,
                f"min_speed_kmh={min_speed_kmh:.1f} exceeds the legal cap {cap:.1f} km/h, so "
                "there is no legal speed to search.",
            ],
        )

    calls = 0

    def run(speed_kmh: float) -> ScenarioResult:
        nonlocal calls
        calls += 1
        return evaluate_scenario(
            name=name,
            description=description or f"constant {speed_kmh:.2f} km/h",
            target_speed_kmh=speed_kmh,
            vehicle=vehicle,
            start_soc_fraction=start_soc_fraction,
            segments=resolved,
            speed_limit_kmh=speed_limit_kmh,
            uncertainty_fraction=0.0,
            now_utc=now_utc,
            **kwargs,
        )

    def margin(speed_kmh: float) -> float:
        result = run(speed_kmh)
        end = float(result.assumptions["end_energy_unclamped_wh"])
        reserve = float(result.assumptions["reserve_floor_wh"])
        return end - reserve

    at_cap = run(cap)
    cap_margin_wh = float(at_cap.assumptions["end_energy_unclamped_wh"]) - float(
        at_cap.assumptions["reserve_floor_wh"]
    )
    if cap_margin_wh >= 0.0 and at_cap.feasible:
        return SpeedOptimisation(
            feasible=True,
            speed_kmh=cap,
            speed_cap_kmh=cap,
            min_speed_kmh=min_speed_kmh,
            travel_time_s=at_cap.travel_time_s,
            end_soc_fraction=at_cap.end_soc_fraction,
            method="saturated at the legal speed cap (energy constraint inactive)",
            function_evaluations=calls,
            scenario=at_cap,
            notes=[
                *notes,
                "The energy constraint is not active: the legal speed limit is what binds. "
                "Any further time gain would require breaking it, which this platform never "
                "recommends.",
            ],
        )

    at_min = run(min_speed_kmh)
    min_margin_wh = float(at_min.assumptions["end_energy_unclamped_wh"]) - float(
        at_min.assumptions["reserve_floor_wh"]
    )
    min_deficit_wh = -min_margin_wh
    if min_margin_wh < 0.0:
        return SpeedOptimisation(
            feasible=False,
            speed_kmh=None,
            speed_cap_kmh=cap,
            min_speed_kmh=min_speed_kmh,
            method="no search: infeasible at every speed in the window",
            function_evaluations=calls,
            scenario=at_min,
            notes=[
                *notes,
                f"Even {min_speed_kmh:.1f} km/h finishes {min_deficit_wh:.0f} Wh "
                "below the reserve, so no constant speed in the window completes this leg. "
                "The leg needs more generation, a longer window, a shorter distance, or an "
                "explicit decision to spend the reserve.",
            ],
        )

    # The energy constraint brackets a root only when it is what binds inside
    # the window. When something else binds at the cap - a power limit, the
    # clause-3.31.6 obstruction rule, a temperature flag - the margin can be
    # positive at BOTH ends, and calling brentq would raise rather than answer.
    # Root-find when the bracket exists; otherwise scan (see below).
    if cap_margin_wh < 0.0 <= min_margin_wh:
        root = float(brentq(margin, min_speed_kmh, cap, xtol=tolerance_kmh, rtol=1e-12))
        candidate = min(root, cap)
        result = run(candidate)
        stepped = 0
        while not result.feasible and candidate - tolerance_kmh >= min_speed_kmh and stepped < 10:
            candidate -= tolerance_kmh
            result = run(candidate)
            stepped += 1
        if result.feasible:
            if stepped:
                notes.append(
                    f"stepped {stepped} x {tolerance_kmh} km/h below the root to land strictly "
                    "inside the feasible region"
                )
            return SpeedOptimisation(
                feasible=True,
                speed_kmh=candidate,
                speed_cap_kmh=cap,
                min_speed_kmh=min_speed_kmh,
                travel_time_s=result.travel_time_s,
                end_soc_fraction=result.end_soc_fraction,
                method=(
                    "scipy.optimize.brentq on the finish-energy margin "
                    "(monotone constraint => the optimum is the boundary)"
                ),
                function_evaluations=calls,
                scenario=result,
                notes=[
                    *notes,
                    f"The energy constraint is active: at {candidate:.2f} km/h the pack finishes "
                    f"on the {vehicle.battery_reserve_fraction:.0%} reserve, so this speed has NO "
                    "energy margin beyond the reserve itself.",
                ],
            )
        notes.append(
            "The root of the energy constraint was not feasible when re-evaluated, so a "
            "constraint other than energy is binding; fell back to a bounded scan."
        )

    # Fallback: a bounded scan on FEASIBILITY, not on the energy margin.
    # Feasibility need not be monotone in speed (the obstruction rule makes very
    # low speeds infeasible while the middle of the window is fine), so a grid
    # scan is used rather than a bisection, then the boundary is refined by
    # bisection between the highest feasible grid point and the next one up.
    # It costs FALLBACK_SCAN_POINTS evaluations plus a handful, and it is
    # reported honestly in `method` so nobody mistakes it for the root find.
    grid = [
        min_speed_kmh + (cap - min_speed_kmh) * i / (FALLBACK_SCAN_POINTS - 1)
        for i in range(FALLBACK_SCAN_POINTS)
    ]
    best_index: int | None = None
    best_result: ScenarioResult | None = None
    for index, speed in enumerate(grid):
        probe = run(speed)
        if probe.feasible:
            best_index, best_result = index, probe
    if best_index is None or best_result is None:
        return SpeedOptimisation(
            feasible=False,
            speed_kmh=None,
            speed_cap_kmh=cap,
            min_speed_kmh=min_speed_kmh,
            method=f"bounded {FALLBACK_SCAN_POINTS}-point feasibility scan: no feasible speed",
            function_evaluations=calls,
            scenario=at_cap,
            notes=[
                *notes,
                "The energy constraint alone did not decide the leg and no speed on the scan "
                "grid was feasible; inspect the violations of the returned scenario, because "
                "something other than energy is blocking every speed in the window.",
            ],
        )
    candidate = grid[best_index]
    result = best_result
    if best_index + 1 < len(grid):
        low, high = candidate, grid[best_index + 1]
        while high - low > tolerance_kmh:
            middle = 0.5 * (low + high)
            probe = run(middle)
            if probe.feasible:
                low, result = middle, probe
            else:
                high = middle
        candidate = low
    return SpeedOptimisation(
        feasible=True,
        speed_kmh=candidate,
        speed_cap_kmh=cap,
        min_speed_kmh=min_speed_kmh,
        travel_time_s=result.travel_time_s,
        end_soc_fraction=result.end_soc_fraction,
        method=(
            f"bounded {FALLBACK_SCAN_POINTS}-point feasibility scan + bisection "
            "(the energy constraint did not bracket a root, so another constraint binds)"
        ),
        function_evaluations=calls,
        scenario=result,
        notes=[
            *notes,
            "A constraint other than the energy reserve is binding, so the fast root-find was "
            "not applicable and a scan was used. The binding constraint is whatever appears as "
            "a blocking violation one step above this speed; inspect the scenario's violations.",
        ],
    )


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------

Objective = Literal["earliest_arrival", "max_end_soc"]


def _objective_key(objective: Objective) -> Callable[[ScenarioResult], float]:
    if objective == "earliest_arrival":
        return lambda r: r.travel_time_s
    return lambda r: -r.end_soc_fraction


def recommend(
    results: Sequence[ScenarioResult],
    *,
    objective: Objective = "earliest_arrival",
    baseline_name: str | None = None,
    reevaluate_after_utc: datetime | None = None,
    reevaluate_within_s: float | None = None,
    now_utc: datetime | None = None,
) -> Recommendation:
    """Pick the best **feasible** scenario and show the numbers behind the choice.

    Objective
    ---------
    ``"earliest_arrival"`` (default) minimises travel time among the feasible
    scenarios - the race objective, once energy feasibility is satisfied.
    ``"max_end_soc"`` maximises the finishing state of charge, which is what to
    ask for when the next day's weather is the dominant risk.

    Infeasible scenarios are never selected, whatever their numbers. If no
    scenario is feasible, the result says exactly that:
    ``recommended_scenario is None``, and the rationale lists why each
    candidate failed. There is no "least bad" fallback, because driving an
    infeasible plan is not a recommendation a weather platform is entitled to
    make.

    Confidence is capped at ``"medium"`` on purpose: the models behind these
    numbers are implemented but unvalidated, so ``"high"`` would be a false
    claim. ``"medium"`` requires that a pessimistic sensitivity case was run,
    that it is also feasible, and that the chosen scenario finishes at least
    5 percentage points of SoC above its reserve; anything less is ``"low"``.

    ``reevaluate_after_utc`` is passed through if given, otherwise derived from
    ``now_utc + reevaluate_within_s``. A recommendation with no re-evaluation
    time is a standing instruction, which this platform must not issue.
    """
    if not results:
        raise ValueError("results must not be empty")

    provenance = _provenance(
        "strategy.scenarios.recommend",
        notes="decision support only; no vehicle commands; requires human review",
        now_utc=now_utc,
    )
    deadline = reevaluate_after_utc
    if deadline is None and reevaluate_within_s is not None:
        base_time = now_utc if now_utc is not None else utc_now()
        deadline = base_time + timedelta(seconds=reevaluate_within_s)

    by_name = {r.name: r for r in results}
    baseline = by_name.get(baseline_name) if baseline_name else results[0]
    if baseline_name and baseline is None:
        raise ValueError(f"baseline_name {baseline_name!r} is not among the results")

    feasible = [r for r in results if r.feasible]
    all_notes: list[str] = []
    for result in results:
        for note in result.uncertainty_notes:
            if note not in all_notes:
                all_notes.append(note)

    if not feasible:
        reasons = []
        for result in results:
            blocking = [v for v in result.violations if v.severity == "blocking"]
            reasons.append(
                f"{result.name} ({result.target_speed_kmh:.0f} km/h): "
                + (
                    "; ".join(f"{v.constraint}: {v.detail}" for v in blocking)
                    if blocking
                    else "no blocking violation recorded but flagged infeasible"
                )
            )
        return Recommendation(
            headline=(
                f"No feasible scenario among the {len(results)} evaluated: "
                "do not plan from this set."
            ),
            rationale=(
                "Every candidate breaks at least one blocking constraint, so there is nothing to "
                "recommend and no least-bad option is offered. "
                + " || ".join(reasons)
                + " Change the inputs that are actually changeable - start state of charge, the "
                "distance to be covered in this leg, the time window, or the speed envelope - "
                "and re-evaluate. If the blocking constraint is a speed limit, the answer is a "
                "lower speed, never an exemption."
            ),
            recommended_scenario=None,
            alternatives=[r.name for r in results],
            energy_impact_wh=None,
            time_impact_s=None,
            confidence="low",
            uncertainty_notes=[
                *all_notes,
                "No feasible scenario: the comparison itself carries no confidence.",
            ],
            safety_and_regulatory_notes=list(SAFETY_NOTES),
            reevaluate_after_utc=deadline,
            evidence=[
                f"{r.name}: {r.target_speed_kmh:.0f} km/h, end SoC {r.end_soc_fraction:.1%}, "
                f"net {r.net_energy_wh:+.0f} Wh, feasible={r.feasible}"
                for r in results
            ],
            provenance=provenance,
        )

    chosen = min(feasible, key=_objective_key(objective))
    others = [r for r in results if r.name != chosen.name]

    pessimistic_feasible = chosen.assumptions.get("pessimistic_feasible")
    pessimistic_soc = _as_float(chosen.assumptions.get("pessimistic_end_soc_fraction"))
    min_soc = _as_float(chosen.assumptions.get("min_soc_fraction"))
    reserve_soc = _as_float(chosen.assumptions.get("reserve_soc_fraction"))

    # Margin above the policy reserve, in percentage points of state of charge,
    # measured at the worst moment of the leg rather than only at the finish.
    margin_pp: float | None = None
    if min_soc is not None and reserve_soc is not None:
        margin_pp = (min_soc - reserve_soc) * 100.0

    robust = pessimistic_feasible is True and pessimistic_soc is not None
    if robust and margin_pp is not None and margin_pp >= MEDIUM_CONFIDENCE_MARGIN_PP:
        confidence: Literal["low", "medium", "high"] = "medium"
    else:
        confidence = "low"

    time_impact = chosen.travel_time_s - baseline.travel_time_s if baseline else None
    energy_impact = chosen.net_energy_wh - baseline.net_energy_wh if baseline else None

    objective_phrase = (
        "earliest arrival among the feasible candidates"
        if objective == "earliest_arrival"
        else "highest finishing state of charge among the feasible candidates"
    )
    rationale_parts = [
        f"Objective: {objective_phrase}.",
        f"{chosen.name} at {chosen.target_speed_kmh:.1f} km/h covers "
        f"{chosen.distance_m / 1000.0:.1f} km in {chosen.travel_time_s / 3600.0:.2f} h, "
        f"consuming {chosen.energy_consumed_wh:.0f} Wh against {chosen.energy_generated_wh:.0f} Wh "
        f"generated (net {chosen.net_energy_wh:+.0f} Wh), taking the pack from "
        f"{chosen.start_soc_fraction:.1%} to {chosen.end_soc_fraction:.1%} state of charge"
        + (
            f", with a worst-moment margin of {margin_pp:+.1f} percentage points above the "
            f"{reserve_soc:.0%} reserve"
            if margin_pp is not None and reserve_soc is not None
            else ""
        )
        + ".",
    ]
    if chosen.arrival_utc is not None:
        rationale_parts.append(f"Predicted arrival {chosen.arrival_utc.isoformat()}.")
    for other in others:
        if other.feasible:
            rationale_parts.append(
                f"Against {other.name} at {other.target_speed_kmh:.1f} km/h: "
                f"{(chosen.travel_time_s - other.travel_time_s) / 60.0:+.0f} min, "
                f"{chosen.net_energy_wh - other.net_energy_wh:+.0f} Wh net, end SoC "
                f"{chosen.end_soc_fraction - other.end_soc_fraction:+.1%}."
            )
        else:
            blocking_names = "; ".join(
                v.constraint for v in other.violations if v.severity == "blocking"
            )
            rationale_parts.append(
                f"{other.name} at {other.target_speed_kmh:.1f} km/h was excluded as infeasible "
                f"({blocking_names or 'blocking violation'})."
            )
    if pessimistic_soc is not None:
        rationale_parts.append(
            f"Under the pessimistic sensitivity case the chosen scenario finishes at "
            f"{pessimistic_soc:.1%} state of charge and is "
            f"{'still feasible' if pessimistic_feasible else 'NOT feasible'}."
        )
    else:
        rationale_parts.append(
            "No sensitivity sweep accompanied this comparison, so the robustness of the choice "
            "to forecast error is unquantified."
        )
    warnings = [v for v in chosen.violations if v.severity == "warning"]
    if warnings:
        rationale_parts.append(
            "Outstanding warnings on the chosen scenario: "
            + "; ".join(f"{v.constraint} - {v.detail}" for v in warnings)
        )

    return Recommendation(
        headline=(
            f"{chosen.name}: hold {chosen.target_speed_kmh:.0f} km/h "
            f"({chosen.travel_time_s / 3600.0:.1f} h, end SoC {chosen.end_soc_fraction:.0%}) "
            "- advisory, human review required"
        ),
        rationale=" ".join(rationale_parts),
        recommended_scenario=chosen.name,
        alternatives=[r.name for r in others],
        energy_impact_wh=energy_impact,
        time_impact_s=time_impact,
        confidence=confidence,
        uncertainty_notes=[
            *all_notes,
            "Confidence is capped at 'medium' by policy: the consumption, battery and scenario "
            "models are implemented but not validated against measured data, so no result from "
            "them can be called high-confidence.",
        ],
        safety_and_regulatory_notes=list(SAFETY_NOTES),
        reevaluate_after_utc=deadline,
        evidence=[
            f"{r.name}: {r.target_speed_kmh:.1f} km/h, {r.travel_time_s / 3600.0:.2f} h, "
            f"consumed {r.energy_consumed_wh:.0f} Wh, generated {r.energy_generated_wh:.0f} Wh, "
            f"end SoC {r.end_soc_fraction:.1%}, feasible={r.feasible}"
            + (
                " | violations: "
                + "; ".join(f"{v.severity}:{v.constraint}" for v in r.violations)
                if r.violations
                else ""
            )
            for r in results
        ],
        provenance=provenance,
    )
