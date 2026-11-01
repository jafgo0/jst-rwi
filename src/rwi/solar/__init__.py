"""Solar geometry and clear-sky irradiance (pvlib / NREL SPA). See docs/science/solar.md."""

from rwi.solar.clearsky import (
    clear_sky_irradiance,
    clear_sky_series,
    extraterrestrial_horizontal_w_m2,
)
from rwi.solar.decomposition import decompose_ghi
from rwi.solar.geometry import solar_day_events, solar_position, solar_positions

__all__ = [
    "clear_sky_irradiance",
    "clear_sky_series",
    "decompose_ghi",
    "extraterrestrial_horizontal_w_m2",
    "solar_day_events",
    "solar_position",
    "solar_positions",
]
