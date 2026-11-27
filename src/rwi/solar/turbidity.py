"""Dynamic Linke turbidity from forecast aerosol and water vapour (ADR 0014).

Method, per hour:

1. AOD at 550 nm (CAMS via Open-Meteo) -> AOD at 700 nm with the Angstrom law,
   ``pvlib.atmosphere.angstrom_aod_at_lambda``. The exponent is a configured
   value (``RWI_AOD_ANGSTROM_EXPONENT``), never pvlib's built-in default: CAMS
   through Open-Meteo gives no exponent, and the right one depends on the
   aerosol type (smoke vs dust).
2. AOD700 is taken as the broadband AOD, following Molineaux, Ineichen &
   O'Neill (1998), Appl. Opt. 37(30), 7008-7018, doi:10.1364/AO.37.007008.
3. Precipitable water [cm] from 2 m temperature and relative humidity,
   ``pvlib.atmosphere.gueymard94_pw`` (Gueymard 1994).
4. ``pvlib.atmosphere.kasten96_lt`` (Kasten 1996, Solar Energy 56(3), 239-244,
   doi:10.1016/0038-092X(95)00114-7; Ineichen 2008, Solar Energy 82(11),
   1095-1097, doi:10.1016/j.solener.2008.04.010) evaluated at absolute air mass
   2, the TL(AM2) convention of the SoDa climatology that the Ineichen-Perez
   clear-sky model expects.

The fit is published for precipitable water below 5 cm. An hour with missing
inputs, pw >= 5 cm, or a non-finite TL or TL < 1 keeps the bundled monthly
climatology instead, and the note counts how many hours did.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pvlib

from rwi.core.geo import GeoPoint
from rwi.solar._frame import to_utc_index
from rwi.solar.clearsky import _linke_turbidity

__all__ = [
    "KASTEN96_AIRMASS",
    "KASTEN96_MAX_PW_CM",
    "TurbidityEstimate",
    "climatology_linke_turbidity",
    "dynamic_linke_turbidity",
]

#: TL is defined at absolute air mass 2 (Ineichen 2008; SoDa climatology convention).
KASTEN96_AIRMASS = 2.0
#: Upper end of the precipitable-water range the fit was published for (Ineichen 2008).
KASTEN96_MAX_PW_CM = 5.0
_AOD_REFERENCE_NM = 550.0
_AOD_BROADBAND_NM = 700.0


@dataclass(frozen=True)
class TurbidityEstimate:
    """Linke turbidity per time, which hours are dynamic, and a provenance note."""

    values: np.ndarray
    dynamic: np.ndarray
    """True where the value came from kasten96_lt, False where from the climatology."""
    note: str


def climatology_linke_turbidity(
    point: GeoPoint, times: Sequence[datetime]
) -> tuple[np.ndarray, str]:
    """Monthly SoDa Linke turbidity at ``point`` and its note, exactly as clear_sky_series uses."""
    return _linke_turbidity(point, to_utc_index(times), None)


def _floats(values: Sequence[float | None], n: int, name: str) -> np.ndarray:
    if len(values) != n:
        raise ValueError(f"{name} has {len(values)} values for {n} times")
    return np.array([np.nan if v is None else float(v) for v in values], dtype=float)


def dynamic_linke_turbidity(
    point: GeoPoint,
    times: Sequence[datetime],
    *,
    aod550: Sequence[float | None],
    temperature_c: Sequence[float | None],
    relative_humidity_pct: Sequence[float | None],
    angstrom_exponent: float,
) -> TurbidityEstimate:
    """TL(AM2) per hour from AOD550, T and RH via kasten96_lt; climatology where invalid."""
    if not np.isfinite(angstrom_exponent):
        raise ValueError(f"angstrom_exponent must be finite, got {angstrom_exponent!r}")
    n = len(times)
    aod = _floats(aod550, n, "aod550")
    temp = _floats(temperature_c, n, "temperature_c")
    rh = _floats(relative_humidity_pct, n, "relative_humidity_pct")
    climatology, climatology_note = (
        climatology_linke_turbidity(point, times) if n else (np.array([], dtype=float), "")
    )

    ok_in = np.isfinite(aod) & (aod >= 0.0) & np.isfinite(temp) & np.isfinite(rh) & (rh > 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        aod700 = np.asarray(
            pvlib.atmosphere.angstrom_aod_at_lambda(
                np.where(ok_in, aod, 0.0),
                _AOD_REFERENCE_NM,
                alpha=angstrom_exponent,
                lambda1=_AOD_BROADBAND_NM,
            ),
            dtype=float,
        )
        pw = np.asarray(
            pvlib.atmosphere.gueymard94_pw(np.where(ok_in, temp, 20.0), np.where(ok_in, rh, 50.0)),
            dtype=float,
        )
        tl = np.asarray(
            pvlib.atmosphere.kasten96_lt(KASTEN96_AIRMASS, pw, aod700),
            dtype=float,
        )
    in_range = ok_in & (pw < KASTEN96_MAX_PW_CM)
    dynamic = in_range & np.isfinite(tl) & (tl >= 1.0)
    values = np.where(dynamic, tl, climatology)

    n_dyn = int(dynamic.sum())
    parts = [
        f"linke_turbidity dynamic for {n_dyn}/{n} h: kasten96_lt at AM{KASTEN96_AIRMASS:g} "
        f"from CAMS AOD550 -> AOD700 (Angstrom alpha={angstrom_exponent:g}, configured) "
        "and gueymard94_pw"
    ]
    if n_dyn:
        parts.append(f"dynamic TL {values[dynamic].min():.2f}-{values[dynamic].max():.2f}")
    reasons = {
        "missing AOD/T/RH": int((~ok_in).sum()),
        f"pw >= {KASTEN96_MAX_PW_CM:g} cm (outside the fit)": int((ok_in & ~in_range).sum()),
        "TL non-finite or < 1": int((in_range & ~dynamic).sum()),
    }
    fallback = [f"{k}: {v} h" for k, v in reasons.items() if v]
    if fallback:
        parts.append(f"{climatology_note} kept for " + ", ".join(fallback))
    return TurbidityEstimate(values=values, dynamic=dynamic, note="; ".join(parts))
