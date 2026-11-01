"""Vehicle longitudinal dynamics and energy store.

:mod:`rwi.vehicle.dynamics` turns an operating point (speed, gradient, air
density, apparent wind) into an electrical power demand;
:mod:`rwi.vehicle.battery` keeps the watt-hour books and enforces the reserve.

Neither module imports :mod:`rwi.route`, :mod:`rwi.solar` or
:mod:`rwi.energy`: everything they need arrives as contract types or plain
numbers.

Validation status: implemented, not validated.
"""

from __future__ import annotations

from rwi.vehicle.battery import (
    DEFAULT_BATTERY_LIMITS,
    BatteryLimits,
    BatteryState,
    BatteryTransition,
    apply_net_energy,
    charge,
    discharge,
    state_from_vehicle,
    temperature_violations,
)
from rwi.vehicle.dynamics import (
    DEFAULT_VEHICLE,
    ISA_SEA_LEVEL_AIR_DENSITY_KG_M3,
    CoastdownFit,
    consumption,
    fit_coastdown,
    resolve_air_density,
)

__all__ = [
    "DEFAULT_BATTERY_LIMITS",
    "DEFAULT_VEHICLE",
    "ISA_SEA_LEVEL_AIR_DENSITY_KG_M3",
    "BatteryLimits",
    "BatteryState",
    "BatteryTransition",
    "CoastdownFit",
    "apply_net_energy",
    "charge",
    "consumption",
    "discharge",
    "fit_coastdown",
    "resolve_air_density",
    "state_from_vehicle",
    "temperature_violations",
]
