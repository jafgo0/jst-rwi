"""Clear-sky irradiance (cloudless upper bound) and extraterrestrial irradiance.

Model: Ineichen & Perez (2002), "A new airmass independent formulation for the
Linke turbidity coefficient", Solar Energy 73(3), 151-157, with the Perez et al.
(2002) DNI enhancement, via ``pvlib.clearsky.ineichen(perez_enhancement=True)``.
Linke turbidity comes from the SoDa climatology bundled with pvlib
(``pvlib.clearsky.lookup_linke_turbidity``, file ``LinkeTurbidities.h5``,
Remund et al.). The bundled file was verified to work **offline**; no network
access is needed. If the lookup ever fails, a documented constant turbidity of
``FALLBACK_LINKE_TURBIDITY`` (3.0) is used and recorded in the provenance notes.

Everything returned here has ``kind=DataKind.CLEAR_SKY_MODEL``: it is a physical
upper bound under a cloudless sky, never a measurement and never a forecast.

Units: W/m2. Angles in degrees. Times aware UTC.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import numpy as np
import pandas as pd
import pvlib

from rwi.contracts import IrradianceComponents
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import ensure_utc, utc_now
from rwi.solar._frame import compute_frame, to_utc_index

__all__ = [
    "FALLBACK_LINKE_TURBIDITY",
    "clear_sky_irradiance",
    "clear_sky_series",
    "extraterrestrial_horizontal_w_m2",
]

#: Used only if the bundled pvlib Linke-turbidity climatology cannot be read.
FALLBACK_LINKE_TURBIDITY = 3.0

#: Below this extraterrestrial horizontal irradiance [W/m2] the clearness index
#: GHI/ET is ill-conditioned (Sun on the horizon) and is reported as None.
_MIN_ET_FOR_CLEARNESS = 1.0

_GHI_MAX, _DNI_MAX, _DHI_MAX = 1600.0, 1400.0, 900.0


def _extra_radiation(index: pd.DatetimeIndex) -> np.ndarray:
    """Extraterrestrial normal irradiance [W/m2] (NREL method), Earth-Sun distance corrected."""
    return np.asarray(pvlib.irradiance.get_extra_radiation(index, method="nrel"), dtype=float)


def extraterrestrial_horizontal_w_m2(point: GeoPoint, when: datetime) -> float:
    """Top-of-atmosphere irradiance on a horizontal plane [W/m2].

    ``pvlib.irradiance.get_extra_radiation(method="nrel")`` (solar constant
    1366.1 W/m2 scaled by the Earth-Sun distance) times the cosine of the
    *geometric* solar zenith, clipped at 0 (night => exactly 0.0).
    """
    index = to_utc_index([when])
    frame = compute_frame(point, index)
    zenith = frame.data["zenith"].to_numpy(dtype=float)
    cos_z = np.cos(np.radians(zenith))
    value = float((_extra_radiation(index) * np.clip(cos_z, 0.0, None))[0])
    return max(value, 0.0)


def _linke_turbidity(
    point: GeoPoint, index: pd.DatetimeIndex, supplied: float | None
) -> tuple[np.ndarray, str]:
    if supplied is not None:
        if not (np.isfinite(supplied) and supplied >= 1.0):
            raise ValueError(f"linke_turbidity must be finite and >= 1, got {supplied!r}")
        return np.full(len(index), float(supplied)), f"linke_turbidity={supplied:.2f} (supplied)"
    try:
        tl = pvlib.clearsky.lookup_linke_turbidity(
            index, point.latitude_deg, point.longitude_deg, interp_turbidity=True
        )
        arr = np.asarray(tl, dtype=float)
        if not np.all(np.isfinite(arr)):
            raise ValueError("non-finite turbidity from climatology")
        return arr, (
            "linke_turbidity from pvlib bundled climatology "
            f"(monthly, interpolated; {arr.min():.2f}-{arr.max():.2f})"
        )
    except Exception as exc:  # documented fallback, recorded in provenance notes
        return (
            np.full(len(index), FALLBACK_LINKE_TURBIDITY),
            f"linke_turbidity={FALLBACK_LINKE_TURBIDITY} (CONSTANT FALLBACK: "
            f"climatology lookup failed: {type(exc).__name__})",
        )


def clear_sky_series(
    point: GeoPoint,
    times: Sequence[datetime],
    *,
    pressure_pa: float | None = None,
    temperature_c: float | None = None,
    linke_turbidity: float | None = None,
) -> list[IrradianceComponents]:
    """Clear-sky GHI/DNI/DHI [W/m2] at ``point`` for each instant (vectorised).

    Model: Ineichen-Perez. Inputs to pvlib: apparent zenith and absolute airmass
    from one SPA call (see :func:`rwi.solar.geometry.solar_positions`); site
    altitude ``point.elevation_or_zero_m``; surface pressure ``pressure_pa`` or
    the standard-atmosphere pressure for the altitude; Linke turbidity
    ``linke_turbidity`` or the monthly pvlib climatology. ``temperature_c`` only
    affects the refraction correction of the apparent zenith (Ineichen itself
    has no temperature term).

    When ``apparent_elevation_deg <= 0`` all three components are exactly 0.0.
    ``solar_elevation_deg`` is the apparent elevation; ``clearness_index`` is
    GHI / extraterrestrial horizontal irradiance, or None when that is < 1 W/m2.
    Output is clipped to the contract bounds and ``0 <= DHI <= GHI``.
    """
    if len(times) == 0:
        return []
    index = to_utc_index(times)
    frame = compute_frame(point, index, pressure_pa=pressure_pa, temperature_c=temperature_c)
    d = frame.data
    app_el = np.clip(d["apparent_elevation"].to_numpy(dtype=float), -90.0, 90.0)
    app_zen = d["apparent_zenith"].to_numpy(dtype=float)
    geo_zen = d["zenith"].to_numpy(dtype=float)

    tl, tl_note = _linke_turbidity(point, index, linke_turbidity)
    dni_extra = _extra_radiation(index)

    rel_am = pvlib.atmosphere.get_relative_airmass(pd.Series(app_zen), model="kastenyoung1989")
    abs_am = pvlib.atmosphere.get_absolute_airmass(rel_am, frame.pressure_pa).to_numpy(dtype=float)
    daylight = (app_el > 0.0) & np.isfinite(abs_am)

    res = pvlib.clearsky.ineichen(
        pd.Series(app_zen, index=index),
        pd.Series(np.where(daylight, abs_am, 1.0), index=index),
        pd.Series(tl, index=index),
        altitude=frame.altitude_m,
        dni_extra=pd.Series(dni_extra, index=index),
        perez_enhancement=True,
    )
    ghi = np.nan_to_num(res["ghi"].to_numpy(dtype=float), nan=0.0)
    dni = np.nan_to_num(res["dni"].to_numpy(dtype=float), nan=0.0)
    dhi = np.nan_to_num(res["dhi"].to_numpy(dtype=float), nan=0.0)

    ghi = np.where(daylight, np.clip(ghi, 0.0, _GHI_MAX), 0.0)
    dni = np.where(daylight, np.clip(dni, 0.0, _DNI_MAX), 0.0)
    dhi = np.where(daylight, np.clip(dhi, 0.0, np.minimum(ghi, _DHI_MAX)), 0.0)

    et_h = dni_extra * np.clip(np.cos(np.radians(geo_zen)), 0.0, None)

    retrieved = utc_now()
    notes = (
        f"Ineichen-Perez clear sky (pvlib.clearsky.ineichen, perez_enhancement=True); "
        f"{frame.notes}; {tl_note}"
    )
    out: list[IrradianceComponents] = []
    for i, ts in enumerate(times):
        when = ensure_utc(ts)
        kt: float | None = None
        if ghi[i] > 0.0 and et_h[i] >= _MIN_ET_FOR_CLEARNESS:
            kt = float(min(ghi[i] / et_h[i], 1.5))
        elif ghi[i] == 0.0:
            kt = 0.0
        prov = Provenance(
            provider="rwi",
            dataset="pvlib",
            kind=DataKind.CLEAR_SKY_MODEL,
            model_name="ineichen_perez",
            model_version=pvlib.__version__,
            retrieved_at_utc=retrieved,
            valid_time_utc=when,
            point=point,
            processing="clear_sky_ineichen_perez",
            notes=notes,
        )
        out.append(
            IrradianceComponents(
                time_utc=when,
                point=point,
                ghi_w_m2=float(ghi[i]),
                dni_w_m2=float(dni[i]),
                dhi_w_m2=float(dhi[i]),
                kind=DataKind.CLEAR_SKY_MODEL,
                provenance=prov,
                solar_elevation_deg=float(app_el[i]),
                clearness_index=kt,
            )
        )
    return out


def clear_sky_irradiance(
    point: GeoPoint,
    when: datetime,
    *,
    pressure_pa: float | None = None,
    temperature_c: float | None = None,
    linke_turbidity: float | None = None,
) -> IrradianceComponents:
    """Clear-sky GHI/DNI/DHI [W/m2] at one instant. See :func:`clear_sky_series`.

    Always ``kind=DataKind.CLEAR_SKY_MODEL``; exactly 0.0 for all components when
    the Sun's apparent centre is at or below the horizon.
    """
    return clear_sky_series(
        point,
        [when],
        pressure_pa=pressure_pa,
        temperature_c=temperature_c,
        linke_turbidity=linke_turbidity,
    )[0]
