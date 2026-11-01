"""PV energy model: irradiance (W/m2) -> POA -> cell temperature -> power (W) -> energy (Wh)."""

from rwi.energy.array import DEFAULT_ARRAY, MONO_SI_ARRAY, array_power, dc_power_w
from rwi.energy.poa import poa_irradiance, surface_azimuth_for_heading
from rwi.energy.series import (
    ErrorMetrics,
    PowerSample,
    compare_with_measured,
    energy_along_trajectory,
    integrate_energy,
    power_series,
)
from rwi.energy.thermal import cell_temperature_c

__all__ = [
    "DEFAULT_ARRAY",
    "MONO_SI_ARRAY",
    "ErrorMetrics",
    "PowerSample",
    "array_power",
    "cell_temperature_c",
    "compare_with_measured",
    "dc_power_w",
    "energy_along_trajectory",
    "integrate_energy",
    "poa_irradiance",
    "power_series",
    "surface_azimuth_for_heading",
]
