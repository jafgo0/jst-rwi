"""Internal helpers shared by the solar modules (not public API).

One vectorised pvlib/NREL-SPA call produces a ``pandas.DataFrame`` of solar
geometry for an arbitrary list of instants; the public functions are thin
adapters from that frame to the shared contract types.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd
import pvlib

from rwi.core.geo import GeoPoint
from rwi.core.timebase import ensure_utc

#: Air temperature [degC] used for the SPA refraction correction when the caller
#: supplies none. This is pvlib's own default for ``get_solarposition``.
DEFAULT_REFRACTION_TEMPERATURE_C = 12.0

#: The pvlib method used for every solar-position calculation (NREL SPA, pure numpy).
PVLIB_SPA_METHOD = "nrel_numpy"


@dataclass(frozen=True)
class SolarFrame:
    """Vectorised SPA output plus the inputs that produced it."""

    index: pd.DatetimeIndex
    data: pd.DataFrame
    altitude_m: float
    pressure_pa: float
    temperature_c: float
    notes: str


def to_utc_index(times: Sequence[datetime]) -> pd.DatetimeIndex:
    """Aware-UTC ``DatetimeIndex`` from datetimes; naive datetimes raise."""
    utc = [ensure_utc(t) for t in times]
    return pd.DatetimeIndex(utc, tz="UTC")


def compute_frame(
    point: GeoPoint,
    index: pd.DatetimeIndex,
    *,
    pressure_pa: float | None = None,
    temperature_c: float | None = None,
) -> SolarFrame:
    """Run ``pvlib.solarposition.get_solarposition`` once over ``index``.

    Passed to pvlib (NREL SPA, method ``nrel_numpy``):
      * ``altitude``    = ``point.elevation_or_zero_m`` (0 m if unknown);
      * ``pressure``    = caller value, else the ICAO standard-atmosphere pressure
        for that altitude (``pvlib.atmosphere.alt2pres``);
      * ``temperature`` = caller value, else 12 degC (pvlib default).
    Altitude enters SPA through the topocentric parallax correction; pressure and
    temperature enter only through the atmospheric-refraction correction.
    """
    altitude = point.elevation_or_zero_m
    alt_note = f"altitude_m={altitude:.1f}" + (
        " (elevation unknown, 0 m assumed)" if point.elevation_m is None else ""
    )
    if pressure_pa is None:
        pressure = float(pvlib.atmosphere.alt2pres(altitude))
        p_note = f"pressure_pa={pressure:.0f} (standard atmosphere for altitude)"
    else:
        if not (pressure_pa > 0.0 and np.isfinite(pressure_pa)):
            raise ValueError(f"pressure_pa must be positive and finite, got {pressure_pa!r}")
        pressure = float(pressure_pa)
        p_note = f"pressure_pa={pressure:.0f} (supplied)"
    if temperature_c is None:
        temperature = DEFAULT_REFRACTION_TEMPERATURE_C
        t_note = f"temperature_c={temperature:.1f} (pvlib default)"
    else:
        if not np.isfinite(temperature_c):
            raise ValueError(f"temperature_c must be finite, got {temperature_c!r}")
        temperature = float(temperature_c)
        t_note = f"temperature_c={temperature:.1f} (supplied)"

    data = pvlib.solarposition.get_solarposition(
        index,
        point.latitude_deg,
        point.longitude_deg,
        altitude=altitude,
        pressure=pressure,
        method=PVLIB_SPA_METHOD,
        temperature=temperature,
    )
    notes = f"{alt_note}; {p_note}; {t_note}"
    return SolarFrame(
        index=index,
        data=data,
        altitude_m=altitude,
        pressure_pa=pressure,
        temperature_c=temperature,
        notes=notes,
    )


def declination_deg(point: GeoPoint, frame: SolarFrame) -> np.ndarray:
    """Solar declination [deg] recovered from the SPA elevation/azimuth.

    Exact spherical trigonometry on SPA outputs (topocentric, geometric
    elevation): ``sin(dec) = sin(lat) sin(el) + cos(lat) cos(el) cos(az)``.
    """
    lat = np.radians(point.latitude_deg)
    el = np.radians(frame.data["elevation"].to_numpy(dtype=float))
    az = np.radians(frame.data["azimuth"].to_numpy(dtype=float))
    sin_dec = np.sin(lat) * np.sin(el) + np.cos(lat) * np.cos(el) * np.cos(az)
    return np.degrees(np.arcsin(np.clip(sin_dec, -1.0, 1.0)))
