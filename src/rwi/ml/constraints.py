"""Physical constraints applied to every ML quantile forecast (ADR 0008 §2).

Enforced after prediction, never left to the model:

* quantiles are sorted per row (no crossing);
* irradiance is predicted as a clear-sky index ``k`` clipped to ``[0, K_MAX]``
  and converted with the pvlib clear-sky value, so GHI/DNI >= 0 and
  DNI <= DNI_cs * K_MAX;
* GHI/DNI are exactly 0.0 when the apparent solar elevation is <= 0 (hard rule 5);
* every other variable is clipped to its canonical range (cloud cover [0, 100]).
"""

import numpy as np
from numpy.typing import NDArray

from rwi.core.variables import spec_for

__all__ = [
    "IRRADIANCE_VARIABLES",
    "K_MAX",
    "MIN_TRAIN_ELEVATION_DEG",
    "ML_VARIABLES",
    "clip_to_spec",
    "irradiance_from_k",
    "sort_quantiles",
]

FloatArray = NDArray[np.float64]

#: Variables the post-processor learns (ADR 0008 §2; temperature and u/v per ADR 0011).
ML_VARIABLES: tuple[str, ...] = (
    "ghi",
    "dni",
    "cloud_cover",
    "wind_speed_10m",
    "temperature_2m",
    "wind_u_10m",
    "wind_v_10m",
)
#: Variables predicted as a clear-sky index and multiplied back by pvlib clear sky.
IRRADIANCE_VARIABLES: frozenset[str] = frozenset({"ghi", "dni"})

#: Upper bound on the clear-sky index. Cloud enhancement lifts surface GHI above
#: the clear-sky value; Gueymard (2017, "Cloud and albedo enhancement impacts on
#: solar irradiance using high-frequency measurements from thermopile and
#: photodiode radiometers", Solar Energy 153, 755-765) reports 1-min enhancement
#: up to about 1.5x clear sky. Hourly values sit well below this, so 1.5 is a
#: bound, not a typical value. Pinned by a science test.
K_MAX = 1.5

#: Irradiance training rows with the Sun lower than this are dropped: k = obs/cs
#: is numerically unstable near the horizon and carries no energy anyway.
MIN_TRAIN_ELEVATION_DEG = 5.0


def sort_quantiles(q: FloatArray) -> FloatArray:
    """Rows of quantile values sorted ascending (removes quantile crossing)."""
    return np.sort(np.asarray(q, dtype=float), axis=-1)


def irradiance_from_k(
    k_quantiles: FloatArray, clear_sky_w_m2: FloatArray, elevation_deg: FloatArray
) -> FloatArray:
    """W/m2 quantiles from clear-sky-index quantiles, clipped, sorted and zero at night."""
    k = sort_quantiles(np.clip(np.asarray(k_quantiles, dtype=float), 0.0, K_MAX))
    cs = np.maximum(np.asarray(clear_sky_w_m2, dtype=float), 0.0)
    out = k * cs[:, np.newaxis]
    night = np.asarray(elevation_deg, dtype=float) <= 0.0
    out[night, :] = 0.0
    return out


def clip_to_spec(variable: str, q: FloatArray) -> FloatArray:
    """Quantiles clipped to the variable's canonical physical range, then sorted."""
    spec = spec_for(variable)
    return sort_quantiles(np.clip(np.asarray(q, dtype=float), spec.minimum, spec.maximum))
