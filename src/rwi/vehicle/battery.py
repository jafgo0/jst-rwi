"""Energy-store (battery) bookkeeping for the strategy engine.

What this is
------------
A deliberately simple energy accumulator in watt-hours, with a reserve policy
and constraint detection. It answers one question: *given this much generation
and this much consumption, where does the state of charge end up, and does it
break a limit on the way?*

What this is **not**
-------------------
It is not a cell model. There is no open-circuit-voltage curve, no internal
resistance, no current or C-rate limit, no coulomb counting, no ageing or
capacity fade, no cell balancing, and **no thermal model**. Temperature is
accepted as an input and compared against a configurable safe band so the
strategists can be warned; nothing in the energy calculation depends on it.
That band's effect on real pack behaviour is **unvalidated** and the limits
themselves are placeholders until the electrical lead sets them from the cell
datasheet and the pack design.

Reserve
-------
``reserve_fraction`` of the nameplate capacity is held back as the energy the
team refuses to plan into. The *usable* capacity is therefore

    ``usable_capacity_wh = capacity_wh * (1 - reserve_fraction)``

and discharging exactly that much from a full pack lands on the reserve
boundary. Landing exactly on the boundary is reported as "at reserve" and is
**not** a violation; going below it by more than a floating-point epsilon is a
``blocking`` violation. The reserve is a policy number, not a measurement.

Where the round-trip efficiency is applied
------------------------------------------
Exactly once, and on the **charge** side::

    stored_wh    = requested_charge_wh * battery_round_trip_efficiency
    delivered_wh = requested_discharge_wh          (1:1, no second derate)

Rationale: a single round-trip number describes the loss over a full
charge-then-discharge cycle, so applying it on one side only is the whole of
it. Splitting it as ``sqrt(eta)`` on each side would be equally defensible for
a symmetric pack, but then a quantity that is charged and never discharged
(the energy sitting in the pack at the finish line) would carry half a loss it
never incurred. Applying it on both sides in full would double-count. The
drivetrain efficiency is a different number and is applied in
:mod:`rwi.vehicle.dynamics`, never here, so the two cannot be confused.

Sign convention: in :func:`apply_net_energy`, positive energy charges the pack
and negative energy discharges it.

Validation status: implemented, not validated.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from rwi.contracts import ScenarioConstraintViolation, VehicleConfig
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import utc_now

__all__ = [
    "DEFAULT_BATTERY_LIMITS",
    "ENERGY_EPSILON_WH",
    "BatteryLimits",
    "BatteryState",
    "BatteryTransition",
    "apply_net_energy",
    "battery_provenance",
    "charge",
    "discharge",
    "state_from_vehicle",
    "temperature_violations",
]

ENERGY_EPSILON_WH: Final[float] = 1e-9
"""Tolerance for "below the reserve". Exactly at the reserve is not a violation."""


class BatteryLimits(BaseModel):
    """Configurable safe operating band. Placeholder values, not measurements."""

    model_config = ConfigDict(frozen=True)

    min_temperature_c: float = Field(
        default=0.0, description="below this, charging and power capability are suspect"
    )
    max_temperature_c: float = Field(
        default=45.0, description="above this, the pack is outside its assumed safe band"
    )
    source_notes: str = Field(
        default=(
            "ASSUMED placeholder band (0-45 degC). Replace with the cell datasheet's "
            "charge/discharge temperature limits and the pack designer's derating policy. "
            "This model has NO thermal physics: it cannot predict pack temperature, it only "
            "flags a temperature it is given. The link between this band and real pack "
            "behaviour is unvalidated."
        )
    )

    def contains(self, temperature_c: float) -> bool:
        return self.min_temperature_c <= temperature_c <= self.max_temperature_c


DEFAULT_BATTERY_LIMITS: Final[BatteryLimits] = BatteryLimits()


class BatteryState(BaseModel):
    """State of charge in Wh, with the capacity and reserve policy it belongs to."""

    model_config = ConfigDict(frozen=True)

    energy_wh: float = Field(ge=0.0, description="energy currently stored, Wh")
    capacity_wh: float = Field(gt=0.0, description="nameplate usable-at-100% capacity, Wh")
    reserve_fraction: float = Field(ge=0.0, lt=1.0)
    round_trip_efficiency: float = Field(default=0.95, gt=0.0, le=1.0)
    temperature_c: float | None = Field(
        default=None, description="pack or ambient temperature, degC; None = unknown, not 0"
    )

    @property
    def soc_fraction(self) -> float:
        """State of charge as a fraction of nameplate capacity, 0-1."""
        return self.energy_wh / self.capacity_wh

    @property
    def reserve_wh(self) -> float:
        """Energy held back by policy, Wh."""
        return self.capacity_wh * self.reserve_fraction

    @property
    def usable_capacity_wh(self) -> float:
        """Energy between a full pack and the reserve floor, Wh."""
        return self.capacity_wh * (1.0 - self.reserve_fraction)

    @property
    def energy_above_reserve_wh(self) -> float:
        """How much may still be planned for; negative means already below reserve."""
        return self.energy_wh - self.reserve_wh

    @property
    def is_at_or_below_reserve(self) -> bool:
        return self.energy_above_reserve_wh <= ENERGY_EPSILON_WH

    @property
    def is_below_reserve(self) -> bool:
        return self.energy_above_reserve_wh < -ENERGY_EPSILON_WH

    def with_energy(self, energy_wh: float, *, temperature_c: float | None = None) -> BatteryState:
        """Same pack and policy, different stored energy (clamped to [0, capacity])."""
        return self.model_copy(
            update={
                "energy_wh": min(max(energy_wh, 0.0), self.capacity_wh),
                "temperature_c": self.temperature_c if temperature_c is None else temperature_c,
            }
        )


class BatteryTransition(BaseModel):
    """One charge/discharge step: the new state and everything that went wrong."""

    model_config = ConfigDict(frozen=True)

    state: BatteryState
    requested_wh: float = Field(description="energy asked for; sign as per the calling function")
    delivered_wh: float = Field(
        default=0.0, ge=0.0, description="energy actually taken out of the pack, Wh"
    )
    stored_wh: float = Field(
        default=0.0, ge=0.0, description="energy actually added to the pack after efficiency, Wh"
    )
    shortfall_wh: float = Field(
        default=0.0,
        ge=0.0,
        description="discharge the pack could not supply because it hit empty, Wh",
    )
    spilled_wh: float = Field(
        default=0.0, ge=0.0, description="charge energy discarded because the pack was full, Wh"
    )
    unclamped_energy_wh: float = Field(
        description="where the energy would have landed with no 0/capacity clamping, Wh"
    )
    violations: list[ScenarioConstraintViolation] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def is_blocked(self) -> bool:
        return any(v.severity == "blocking" for v in self.violations)


def state_from_vehicle(
    vehicle: VehicleConfig,
    *,
    soc_fraction: float,
    temperature_c: float | None = None,
) -> BatteryState:
    """Battery state for a vehicle's pack at a given state of charge (0-1)."""
    if not 0.0 <= soc_fraction <= 1.0:
        raise ValueError("soc_fraction must be in [0, 1]")
    return BatteryState(
        energy_wh=vehicle.battery_capacity_wh * soc_fraction,
        capacity_wh=vehicle.battery_capacity_wh,
        reserve_fraction=vehicle.battery_reserve_fraction,
        round_trip_efficiency=vehicle.battery_round_trip_efficiency,
        temperature_c=temperature_c,
    )


def temperature_violations(
    state: BatteryState,
    *,
    limits: BatteryLimits = DEFAULT_BATTERY_LIMITS,
    context: str = "",
) -> list[ScenarioConstraintViolation]:
    """Warn when a supplied pack temperature sits outside the configured band.

    Returns an empty list when the temperature is unknown: an absent
    measurement is not evidence of a safe temperature, and inventing one would
    be worse than saying nothing. The warning is advisory only - this module
    has no thermal physics, so it cannot say what the pack will actually do.
    """
    if state.temperature_c is None:
        return []
    if limits.contains(state.temperature_c):
        return []
    where = f" ({context})" if context else ""
    return [
        ScenarioConstraintViolation(
            constraint="battery_temperature",
            detail=(
                f"pack/ambient temperature {state.temperature_c:.1f} degC is outside the "
                f"configured safe band [{limits.min_temperature_c:.1f}, "
                f"{limits.max_temperature_c:.1f}] degC{where}. The band is an ASSUMED "
                "placeholder and this model has no thermal physics: the energy numbers are "
                "unaffected and the real consequence is UNVALIDATED. Escalate to the "
                "electrical lead."
            ),
            severity="warning",
        )
    ]


def discharge(
    state: BatteryState,
    energy_wh: float,
    *,
    limits: BatteryLimits = DEFAULT_BATTERY_LIMITS,
    context: str = "",
) -> BatteryTransition:
    """Take ``energy_wh`` (>= 0) out of the pack, 1:1, with no efficiency derate.

    Round-trip efficiency is applied on the charge side only (see the module
    docstring), so a discharge removes exactly the energy that was delivered.
    Flags ``battery_reserve`` (blocking) when the result is below the reserve,
    and ``battery_empty`` (blocking) when the pack could not supply the request
    at all.
    """
    if energy_wh < 0.0:
        raise ValueError("discharge energy_wh must be non-negative; use charge() to add energy")
    return apply_net_energy(state, -energy_wh, limits=limits, context=context)


def charge(
    state: BatteryState,
    energy_wh: float,
    *,
    limits: BatteryLimits = DEFAULT_BATTERY_LIMITS,
    context: str = "",
) -> BatteryTransition:
    """Put ``energy_wh`` (>= 0) into the pack, derated once by the round-trip efficiency.

    Energy beyond the nameplate capacity is reported as ``spilled_wh`` with a
    ``battery_capacity`` warning: a real system curtails the MPPTs instead of
    overcharging, and curtailment is a lost opportunity, not a safety event.
    """
    if energy_wh < 0.0:
        raise ValueError("charge energy_wh must be non-negative; use discharge() to remove energy")
    return apply_net_energy(state, energy_wh, limits=limits, context=context)


def apply_net_energy(
    state: BatteryState,
    net_energy_wh: float,
    *,
    limits: BatteryLimits = DEFAULT_BATTERY_LIMITS,
    context: str = "",
) -> BatteryTransition:
    """Apply a signed energy change: positive charges, negative discharges.

    This is the single implementation; :func:`charge` and :func:`discharge` are
    sign-checked wrappers around it. The round-trip efficiency multiplies the
    charge case only.
    """
    where = f" ({context})" if context else ""
    violations: list[ScenarioConstraintViolation] = []
    notes: list[str] = []

    delivered = 0.0
    stored = 0.0
    shortfall = 0.0
    spilled = 0.0

    if net_energy_wh >= 0.0:
        stored_request = net_energy_wh * state.round_trip_efficiency
        unclamped = state.energy_wh + stored_request
        notes.append(
            f"charge {net_energy_wh:.3f} Wh * round_trip_efficiency="
            f"{state.round_trip_efficiency:g} -> {stored_request:.3f} Wh stored "
            "(efficiency applied once, on the charge side only)"
        )
        if unclamped > state.capacity_wh + ENERGY_EPSILON_WH:
            spilled = unclamped - state.capacity_wh
            stored = state.capacity_wh - state.energy_wh
            violations.append(
                ScenarioConstraintViolation(
                    constraint="battery_capacity",
                    detail=(
                        f"charging would exceed the nameplate capacity by {spilled:.1f} Wh"
                        f"{where}: SoC would reach "
                        f"{unclamped / state.capacity_wh * 100.0:.1f} % of "
                        f"{state.capacity_wh:.0f} Wh. {spilled:.1f} Wh is treated as curtailed "
                        "(MPPT clipping / array shaded off), i.e. lost opportunity, not damage."
                    ),
                    severity="warning",
                )
            )
        else:
            stored = stored_request
    else:
        request = -net_energy_wh
        unclamped = state.energy_wh - request
        notes.append(
            f"discharge {request:.3f} Wh taken 1:1 (no efficiency derate on discharge)"
        )
        if unclamped < -ENERGY_EPSILON_WH:
            shortfall = -unclamped
            delivered = state.energy_wh
            violations.append(
                ScenarioConstraintViolation(
                    constraint="battery_empty",
                    detail=(
                        f"the pack cannot supply the demand{where}: {request:.1f} Wh required, "
                        f"{state.energy_wh:.1f} Wh available, short by {shortfall:.1f} Wh. "
                        "The vehicle stops before this point; the state of charge is reported "
                        "clamped at 0 % and the shortfall is carried separately."
                    ),
                    severity="blocking",
                )
            )
        else:
            delivered = request

    new_state = state.with_energy(unclamped)

    if new_state.is_below_reserve or unclamped < state.reserve_wh - ENERGY_EPSILON_WH:
        deficit = state.reserve_wh - unclamped
        violations.append(
            ScenarioConstraintViolation(
                constraint="battery_reserve",
                detail=(
                    f"state of charge would fall below the {state.reserve_fraction * 100.0:.0f} % "
                    f"reserve{where}: {unclamped:.1f} Wh against a reserve floor of "
                    f"{state.reserve_wh:.1f} Wh ({deficit:.1f} Wh short). The reserve is a team "
                    "policy limit, so this scenario is not recommendable."
                ),
                severity="blocking",
            )
        )

    violations.extend(temperature_violations(new_state, limits=limits, context=context))

    return BatteryTransition(
        state=new_state,
        requested_wh=net_energy_wh,
        delivered_wh=delivered,
        stored_wh=stored,
        shortfall_wh=shortfall,
        spilled_wh=spilled,
        unclamped_energy_wh=unclamped,
        violations=violations,
        notes=notes,
    )


def battery_provenance(*, now_utc: datetime | None = None) -> Provenance:
    """Provenance stamp for battery bookkeeping results."""
    return Provenance(
        provider="rwi",
        dataset="vehicle.battery",
        kind=DataKind.DERIVED,
        retrieved_at_utc=now_utc if now_utc is not None else utc_now(),
        model_name="watt-hour energy accumulator with reserve policy",
        processing=(
            "round-trip efficiency on the charge side only; no cell model, no C-rate limit, "
            "no thermal physics (temperature is only compared against a configured band)"
        ),
    )
