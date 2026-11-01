"""Unit constants and conversions.

Rule for the whole platform: every stored and transported numeric value uses the
canonical SI-ish unit declared in :mod:`rwi.core.variables`. Conversion happens
at the system boundary (provider adapter in, UI formatting out) and nowhere else.

Dimensional discipline: irradiance is W/m2, power is W, energy is Wh. These are
never interchangeable; see :func:`integrate_power_wh`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

# Physical constants
SOLAR_CONSTANT_W_M2 = 1361.0
"""Mean total solar irradiance at 1 AU (W/m2), IAU/CODATA 2015 value."""

STANDARD_PRESSURE_PA = 101325.0
ABSOLUTE_ZERO_C = -273.15
STEFAN_BOLTZMANN = 5.670374419e-8
AIR_GAS_CONSTANT_J_KG_K = 287.058
GRAVITY_M_S2 = 9.80665
STC_IRRADIANCE_W_M2 = 1000.0
"""Standard Test Conditions reference irradiance for PV modules."""
STC_CELL_TEMPERATURE_C = 25.0

# Speed
def kmh_to_ms(v_kmh: float) -> float:
    return v_kmh / 3.6


def ms_to_kmh(v_ms: float) -> float:
    return v_ms * 3.6


def knots_to_ms(v_kn: float) -> float:
    return v_kn * 0.5144444444444445


# Temperature
def celsius_to_kelvin(t_c: float) -> float:
    return t_c - ABSOLUTE_ZERO_C


def kelvin_to_celsius(t_k: float) -> float:
    return t_k + ABSOLUTE_ZERO_C


# Pressure
def hpa_to_pa(p_hpa: float) -> float:
    return p_hpa * 100.0


def pa_to_hpa(p_pa: float) -> float:
    return p_pa / 100.0


# Energy
def wh_to_joule(e_wh: float) -> float:
    return e_wh * 3600.0


def joule_to_wh(e_j: float) -> float:
    return e_j / 3600.0


def wh_to_kwh(e_wh: float) -> float:
    return e_wh / 1000.0


def integrate_power_wh(power_w: Sequence[float], times_s: Sequence[float]) -> float:
    """Trapezoidal integration of instantaneous power (W) over time (s) -> energy (Wh).

    This is the only sanctioned way to turn a power series into energy. Both
    sequences must be the same length and ``times_s`` must be non-decreasing.
    """
    if len(power_w) != len(times_s):
        raise ValueError("power_w and times_s must have equal length")
    if len(power_w) < 2:
        return 0.0
    joules = 0.0
    for i in range(1, len(power_w)):
        dt = times_s[i] - times_s[i - 1]
        if dt < 0:
            raise ValueError("times_s must be non-decreasing")
        joules += 0.5 * (power_w[i] + power_w[i - 1]) * dt
    return joule_to_wh(joules)


# Angles
def wrap_deg_360(angle_deg: float) -> float:
    """Wrap an angle into [0, 360)."""
    return angle_deg % 360.0


def wrap_deg_180(angle_deg: float) -> float:
    """Wrap an angle into (-180, 180]."""
    a = (angle_deg + 180.0) % 360.0 - 180.0
    return 180.0 if a == -180.0 else a


def angular_difference_deg(a_deg: float, b_deg: float) -> float:
    """Smallest signed difference a-b, in (-180, 180]."""
    return wrap_deg_180(a_deg - b_deg)


def deg_to_rad(a_deg: float) -> float:
    return math.radians(a_deg)


def rad_to_deg(a_rad: float) -> float:
    return math.degrees(a_rad)


# Air density, needed for aerodynamic drag at race altitudes/temperatures.
def air_density_kg_m3(pressure_pa: float, temperature_c: float) -> float:
    """Dry-air density from the ideal gas law.

    Ignores humidity; moist air is slightly less dense, so this is a mildly
    conservative (higher-drag) estimate. Documented in docs/SCIENCE.md.
    """
    return pressure_pa / (AIR_GAS_CONSTANT_J_KG_K * celsius_to_kelvin(temperature_c))
