"""Heliosat-style cloud index and clear-sky index from visible reflectance.

This module is the *pure arithmetic* half of the satellite irradiance retrieval:
no I/O, no scene objects, only arrays in and arrays out. See
``docs/science/satellite-irradiance.md`` for the whole chain and its caveats.

The method (Cano et al. 1986, Beyer et al. 1996, Hammer et al. 2003,
Rigollier et al. 2004 - "Heliosat") rests on one observation: seen from orbit
in the visible band, a cloud is brighter than the ground beneath it, and the
brighter the pixel, the less sunlight reaches the surface. It does so in two
steps.

1. **Cloud index** ``n`` - where the observed reflectance ``rho`` sits between
   two references, the *clear-ground* reflectance ``rho_clear`` (what this
   pixel looks like with no cloud over it) and the *brightest-cloud*
   reflectance ``rho_max``::

       n = (rho - rho_clear) / (rho_max - rho_clear)

   ``n`` = 0 is clear ground, ``n`` = 1 is the brightest cloud seen.

2. **Clear-sky index** ``k*`` (also written ``kc``) - the piecewise relation of
   Hammer et al. (2003) as adopted by Rigollier et al. (2004), mapping ``n`` to
   the ratio of actual to clear-sky global horizontal irradiance,
   ``k* = GHI / GHI_clear``. See :func:`clear_sky_index` for the exact form.

Both references are *estimated from the image archive itself* (a per-pixel low
percentile over recent scenes for the ground, a high percentile for the cloud)
or supplied by the caller. They are the weakest part of the method: **a wrong
clear-ground reference biases every retrieval**, and over bright red desert -
which looks like thin cloud - it is hard to get right. If a reference cannot be
estimated this module raises or returns NaN; it never substitutes a constant
that would yield plausible-looking numbers.

Units: reflectance and indices are dimensionless. NaN means "no data" and is
propagated, never turned into a number.
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np

__all__ = [
    "KSTAR_CLIP_MAX",
    "KSTAR_CLIP_MIN",
    "KSTAR_ENHANCEMENT_REFERENCE",
    "MIN_REFERENCE_CONTRAST",
    "N_BREAK_CLEAR",
    "N_BREAK_CLOUDY",
    "N_BREAK_OVERCAST",
    "N_CLAMP_HIGH",
    "N_CLAMP_LOW",
    "ClippedIndex",
    "ReferenceEstimationError",
    "clamp_cloud_index",
    "clear_sky_index",
    "clip_clear_sky_index",
    "cloud_index",
    "estimate_clear_sky_reflectance",
    "estimate_max_reflectance",
    "normalise_reflectance_by_sun",
]

# ---------------------------------------------------------------------------
# The published piecewise relation (Hammer et al. 2003; Rigollier et al. 2004)
# ---------------------------------------------------------------------------

#: Lower breakpoint: at or below this ``n`` the pixel is darker than the clear
#: reference (n < -0.2), taken as clear sky with a modest enhancement, k* = 1.2.
N_BREAK_CLEAR: Final = -0.2
#: ``-0.2 < n <= 0.8`` is the linear regime, k* = 1 - n.
N_BREAK_CLOUDY: Final = 0.8
#: ``0.8 < n <= 1.1`` is the quadratic roll-off to the thick-cloud floor.
N_BREAK_OVERCAST: Final = 1.1
#: k* for n <= -0.2 (published value).
_KSTAR_BELOW_CLEAR: Final = 1.2
#: k* for n > 1.1 (published value): thick cloud still passes ~5 % as diffuse light.
_KSTAR_OVERCAST: Final = 0.05
#: Coefficients of the quadratic segment, k* = a + b n + c n^2 (published values).
_QUAD_A: Final = 2.0667
_QUAD_B: Final = -3.6667
_QUAD_C: Final = 1.6667

# ---------------------------------------------------------------------------
# Clipping policy for k*
# ---------------------------------------------------------------------------

#: Documented reference: k* modestly above 1 is a real effect (cloud
#: enhancement: sunlight scattered off the sides of cumulus adds to the direct
#: beam near cloud edges). Values up to this level are preserved untouched.
KSTAR_ENHANCEMENT_REFERENCE: Final = 1.1
#: Upper clip. Above ~1.3 a retrieval pixel is almost certainly an artefact
#: (a wrong clear-ground reference, sun glint, a shadowed or dark surface),
#: because instantaneous enhancement spikes seen by ground sensors are far too
#: short and small to survive averaging over a 0.5-1 km pixel and a 10-minute
#: scan. The published relation itself never exceeds 1.2, so this clip only
#: fires for callers who feed in k* from elsewhere, or after a future change of
#: relation; it is kept so a bug cannot pass an absurd value downstream.
KSTAR_CLIP_MAX: Final = 1.3
#: Lower clip. Irradiance cannot be negative; the published floor is 0.05.
KSTAR_CLIP_MIN: Final = 0.0

#: Minimum ``rho_max - rho_clear`` for the cloud index to be meaningful. Below
#: it the denominator is dominated by noise and n explodes: the result is NaN.
MIN_REFERENCE_CONTRAST: Final = 0.05


class ReferenceEstimationError(ValueError):
    """A reference field could not be estimated honestly (too few scenes, no data)."""


@dataclass(frozen=True)
class ClippedIndex:
    """Clipped k* with a record of how much clipping happened.

    ``n_clipped_high`` counts finite values that exceeded ``upper`` and
    ``n_clipped_low`` those below ``lower``. NaN pixels are neither.
    """

    values: np.ndarray
    n_clipped_low: int
    n_clipped_high: int
    n_finite: int
    lower: float
    upper: float

    @property
    def n_clipped(self) -> int:
        return self.n_clipped_low + self.n_clipped_high

    @property
    def was_clipped(self) -> bool:
        return self.n_clipped > 0

    def describe(self) -> str:
        if not self.was_clipped:
            return f"no clipping (k* kept within [{self.lower}, {self.upper}])"
        return (
            f"clipped {self.n_clipped_high} high (> {self.upper}) and "
            f"{self.n_clipped_low} low (< {self.lower}) of {self.n_finite} finite k* values"
        )


# ---------------------------------------------------------------------------
# Sun-geometry normalisation helper
# ---------------------------------------------------------------------------


def normalise_reflectance_by_sun(
    reflectance_factor: np.ndarray | float, solar_zenith_deg: np.ndarray | float
) -> np.ndarray:
    """Divide a reflectance *factor* by cos(solar zenith) -> sun-normalised reflectance.

    Level-2 visible "CMI" products are commonly reflectance *factors*
    (``pi * L * d^2 / E_sun``), which still contain the cos(theta_s) of the
    illumination. Heliosat compares pixels against references taken at other
    sun angles, so the factor must first be normalised::

        rho = rho_factor / cos(theta_s)

    Whether a given ``SatelliteScene`` with ``quantity="reflectance"`` is
    already normalised is the ingest module's declaration to make; this
    function exists so the caller can do it explicitly. Pixels at or beyond
    90 degrees zenith (cos <= 0) become NaN, never infinity.
    """
    rf = np.asarray(reflectance_factor, dtype=float)
    cos_z = np.cos(np.radians(np.asarray(solar_zenith_deg, dtype=float)))
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(cos_z > 0.0, rf / cos_z, np.nan)
    return np.asarray(out, dtype=float)


# ---------------------------------------------------------------------------
# A. Cloud index
# ---------------------------------------------------------------------------


def cloud_index(
    reflectance: np.ndarray | float,
    *,
    clear_sky_reflectance: np.ndarray | float,
    max_reflectance: np.ndarray | float,
    min_contrast: float = MIN_REFERENCE_CONTRAST,
) -> np.ndarray:
    """Heliosat cloud index ``n = (rho - rho_clear) / (rho_max - rho_clear)``.

    ``reflectance`` is the observed (sun-normalised) reflectance, any shape.
    ``clear_sky_reflectance`` and ``max_reflectance`` are scalars or arrays that
    broadcast against it. ``n`` is *not* clipped here: values below 0 (darker
    than the clear reference) and above 1 (brighter than the cloud reference)
    are meaningful inputs to :func:`clear_sky_index`, whose published relation
    handles both ends.

    NaN in any input gives NaN. Where ``rho_max - rho_clear < min_contrast``
    (or the references are not finite, or are in the wrong order) the result is
    NaN rather than a number made from a noise-dominated denominator.
    """
    rho = np.asarray(reflectance, dtype=float)
    clear = np.asarray(clear_sky_reflectance, dtype=float)
    top = np.asarray(max_reflectance, dtype=float)
    span = top - clear
    with np.errstate(divide="ignore", invalid="ignore"):
        n = (rho - clear) / span
    ok = np.isfinite(span) & (span >= min_contrast)
    return np.asarray(np.where(ok, n, np.nan), dtype=float)


# ---------------------------------------------------------------------------
# A. Clear-sky index (the published piecewise relation)
# ---------------------------------------------------------------------------


#: Heliosat-2 limits the cloud index itself, independently of the k* clip.
#: Source: Mines ParisTech "Description of the software Heliosat-2", which
#: states the cloud index is limited to -0.5 and 1.5. Values outside that are
#: retrieval artefacts (specular reflection, bad reference fields), not weather.
N_CLAMP_LOW: Final = -0.5
N_CLAMP_HIGH: Final = 1.5

# ---------------------------------------------------------------------------
# VERIFICATION STATUS OF THE n -> k* RELATION, checked by the integrator
# 2026-11-02. Read this before trusting a retrieved irradiance.
#
# The coefficients below were written from memory of the literature by the
# engineer who implemented them, and an attempt to confirm them against a
# primary source in that session FAILED: the Mines ParisTech software
# description PDF has no text layer, and the arXiv paper tried as a secondary
# source yielded no extractable text either.
#
# Independently CORROBORATED by literature search:
#   * the linear middle branch k* = 1 - n. A Heliosat-2 description states
#     that a cloud index above 0.2 means a clear-sky index below 0.8, which is
#     exactly k*(0.2) = 0.8 under this branch.
#   * the existence of a quadratic branch above n = 0.8, introduced because a
#     linear relation underestimates k* for overcast skies.
#
# NOT corroborated, and therefore NOT to be trusted without checking:
#   * the constant k* = 1.2 below n = -0.2;
#   * the quadratic coefficients 2.0667, -3.6667, 1.6667;
#   * the floor k* = 0.05 above n = 1.1.
#
# TO RESOLVE: read Rigollier, Lefevre and Wald (2004), Solar Energy 77(2),
# 159-169, section on the cloud index, or the Heliosat-2 software description
# from Mines ParisTech, and confirm or correct the four branches. Until then
# every retrieved irradiance carries an unquantified systematic error on top
# of the unknown error from having no ground truth at all.
#
# A separate and equally important caveat: this relation was developed for
# Meteosat over Europe and Africa. Central Australia is bright red desert
# under a different viewing geometry, which is the hardest case for the
# method, and no one has tuned it for Himawari.
# ---------------------------------------------------------------------------


def clamp_cloud_index(cloud_index: np.ndarray | float) -> np.ndarray:
    """Apply Heliosat-2's limits on ``n`` itself: ``[-0.5, 1.5]``.

    Deliberately NOT applied inside :func:`cloud_index`, which returns the raw
    index so that a caller can see how far outside the plausible range a pixel
    fell - that is diagnostic information about the reference fields, and
    hiding it would make a bad clear-ground estimate invisible.

    Use this wherever ``n`` is shown or advected rather than immediately
    converted to ``k*``: the piecewise relation already saturates outside its
    breakpoints, but a raw ``n`` of -40 from a near-zero reference span would
    otherwise be displayed, or smeared across a nowcast, as if it meant
    something.

    NaN is preserved: unknown is not clamped into a value.
    """
    values = np.asarray(cloud_index, dtype=float)
    return np.clip(values, N_CLAMP_LOW, N_CLAMP_HIGH)


def clear_sky_index(cloud_index: np.ndarray | float) -> np.ndarray:
    """Clear-sky index ``k* = GHI / GHI_clear`` from the cloud index ``n``.

    The piecewise relation of Hammer et al. (2003), as used in Heliosat-2 by
    Rigollier et al. (2004), with exactly these breakpoints::

        k* = 1.2                                   n <= -0.2
        k* = 1 - n                         -0.2 <  n <=  0.8
        k* = 2.0667 - 3.6667 n + 1.6667 n^2  0.8 <  n <=  1.1
        k* = 0.05                                  n >   1.1

    The pieces join (to the rounding of the published coefficients): k*(-0.2) =
    1.2, k*(0.8) = 0.2 (quadratic gives 0.2000), k*(1.1) = 0.05. It is
    monotonically non-increasing in ``n`` - the quadratic's slope,
    ``-3.6667 + 3.3333 n``, reaches 0 only at ``n = 1.1``.

    References:
        Hammer, A., Heinemann, D., Hoyer, C., Kuhlemann, R., Lorenz, E.,
        Mueller, R., Beyer, H.G. (2003). Solar energy assessment using remote
        sensing technologies. Remote Sensing of Environment 86(3), 423-432.

        Rigollier, C., Lefevre, M., Wald, L. (2004). The method Heliosat-2 for
        deriving shortwave solar radiation from satellite images. Solar Energy
        77(2), 159-169.

    The breakpoints were entered from the literature as the team knows it; the
    paper itself should be re-read against ``docs/science/satellite-
    irradiance.md`` before anyone relies on the third decimal.

    The relation was developed for Meteosat-class imagery over Europe and
    Africa. Its applicability to Himawari/AHI band 3 over central Australia is
    an *assumption* that nothing in this platform can test. NaN stays NaN.
    The output is **not** clipped here; see :func:`clip_clear_sky_index`.
    """
    n = np.asarray(cloud_index, dtype=float)
    with np.errstate(invalid="ignore"):
        quad = _QUAD_A + _QUAD_B * n + _QUAD_C * n * n
        k = np.where(
            n <= N_BREAK_CLEAR,
            _KSTAR_BELOW_CLEAR,
            np.where(
                n <= N_BREAK_CLOUDY,
                1.0 - n,
                np.where(n <= N_BREAK_OVERCAST, quad, _KSTAR_OVERCAST),
            ),
        )
    return np.asarray(np.where(np.isnan(n), np.nan, k), dtype=float)


def clip_clear_sky_index(
    kstar: np.ndarray | float,
    *,
    lower: float = KSTAR_CLIP_MIN,
    upper: float = KSTAR_CLIP_MAX,
) -> ClippedIndex:
    """Clip k* to ``[lower, upper]`` and record how many pixels were touched.

    Policy: k* up to about 1.1 is cloud enhancement and is preserved, as is
    anything up to ``upper`` (1.3 by default); beyond that it is treated as a
    retrieval artefact and clipped to ``upper``. The clip is *recorded*
    (:attr:`ClippedIndex.n_clipped_high`), not silent, so a field that is
    mostly clipped can be recognised as a failed retrieval. NaN is never
    clipped into a number.
    """
    if not (np.isfinite(lower) and np.isfinite(upper) and lower <= upper):
        raise ValueError(f"need finite lower <= upper, got {lower!r}, {upper!r}")
    k = np.asarray(kstar, dtype=float)
    finite = np.isfinite(k)
    with np.errstate(invalid="ignore"):
        low = finite & (k < lower)
        high = finite & (k > upper)
    clipped = np.where(finite, np.clip(k, lower, upper), np.nan)
    return ClippedIndex(
        values=np.asarray(clipped, dtype=float),
        n_clipped_low=int(low.sum()),
        n_clipped_high=int(high.sum()),
        n_finite=int(finite.sum()),
        lower=float(lower),
        upper=float(upper),
    )


# ---------------------------------------------------------------------------
# A. Reference fields
# ---------------------------------------------------------------------------


def _as_stack(scenes: Sequence[np.ndarray] | np.ndarray, *, what: str) -> np.ndarray:
    try:
        stack = np.asarray(scenes, dtype=float)
    except ValueError as exc:  # ragged scenes
        raise ReferenceEstimationError(
            f"cannot estimate {what}: scenes do not share one shape ({exc})"
        ) from exc
    if stack.ndim == 2:
        stack = stack[np.newaxis, ...]
    if stack.ndim != 3:
        raise ReferenceEstimationError(
            f"cannot estimate {what}: expected a (scenes, rows, cols) stack, got {stack.ndim}-D"
        )
    return stack


def estimate_clear_sky_reflectance(
    scenes: Sequence[np.ndarray] | np.ndarray,
    *,
    percentile: float = 5.0,
    min_scenes: int = 10,
    min_valid_samples: int = 5,
) -> np.ndarray:
    """Per-pixel clear-ground reflectance from a time series of scenes.

    ``scenes`` is a ``(time, rows, cols)`` stack of sun-normalised reflectance
    on one grid. The reference is the per-pixel ``percentile`` (default the
    5th) over time, ignoring NaN: the darkest the pixel is *usually* seen to be
    is taken as the cloud-free ground.

    For this to mean anything the stack should hold **the same time of day on
    many recent days** (so sun and view geometry, and hence the surface's
    bidirectional reflectance, match the scene being retrieved) and span enough
    days that most pixels were clear at least once.

    Raises :class:`ReferenceEstimationError` when fewer than ``min_scenes``
    scenes are supplied or when no pixel has ``min_valid_samples`` finite
    values. Pixels that individually lack ``min_valid_samples`` finite values
    are NaN in the result (and so give a NaN retrieval). No constant is ever
    substituted.

    Known failure modes (not detectable from the data alone):

    * a pixel that was cloudy in every scene: the "clear" reference is then
      cloud, ``n`` is biased low, and every retrieval is **too bright** in GHI;
    * a percentile that catches cloud shadow or noise: reference too dark,
      ``n`` biased high, retrievals too dark in GHI;
    * bright red desert, where clear ground is nearly as bright as thin cloud
      (small ``rho_max - rho_clear``): the index is then very sensitive to
      the reference, and thin cloud may be invisible.
    """
    if min_scenes < 2:
        raise ValueError("min_scenes must be at least 2")
    if not (0.0 <= percentile <= 100.0):
        raise ValueError(f"percentile must be in [0, 100], got {percentile!r}")
    stack = _as_stack(scenes, what="the clear-sky reflectance")
    if stack.shape[0] < min_scenes:
        raise ReferenceEstimationError(
            f"cannot estimate the clear-sky reflectance: {stack.shape[0]} scene(s) supplied, "
            f"at least {min_scenes} required. Supply clear_sky_reflectance explicitly, or "
            "accumulate more scenes; no default constant is used"
        )
    n_valid = np.isfinite(stack).sum(axis=0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)  # all-NaN slices
        ref = np.nanpercentile(stack, percentile, axis=0)
    ref = np.where(n_valid >= min_valid_samples, ref, np.nan)
    if not np.isfinite(ref).any():
        raise ReferenceEstimationError(
            "cannot estimate the clear-sky reflectance: no pixel has "
            f"{min_valid_samples} or more finite samples across {stack.shape[0]} scenes"
        )
    return np.asarray(ref, dtype=float)


def estimate_max_reflectance(
    scenes: Sequence[np.ndarray] | np.ndarray,
    *,
    percentile: float = 98.0,
    min_scenes: int = 10,
    min_valid_samples: int = 5,
    pooled: bool = True,
) -> np.ndarray | float:
    """Brightest-cloud reflectance from a time series of scenes.

    With ``pooled=True`` (default) one scalar: the ``percentile`` (default the
    98th) of every finite pixel in every scene. Thick-cloud tops are about
    equally bright wherever they are, so pooling is far more stable than a
    per-pixel high percentile, which over a desert is dominated by whichever
    few clouds happened to pass. With ``pooled=False`` a per-pixel field.

    Raises :class:`ReferenceEstimationError` when there are fewer than
    ``min_scenes`` scenes or no usable data; never falls back to a constant.
    Too low a ``max_reflectance`` makes ``n`` too large (retrievals too dark);
    too high makes them too bright. A scene sequence containing no thick cloud
    at all will under-estimate it.
    """
    if min_scenes < 2:
        raise ValueError("min_scenes must be at least 2")
    if not (0.0 <= percentile <= 100.0):
        raise ValueError(f"percentile must be in [0, 100], got {percentile!r}")
    stack = _as_stack(scenes, what="the maximum (cloud) reflectance")
    if stack.shape[0] < min_scenes:
        raise ReferenceEstimationError(
            f"cannot estimate the maximum reflectance: {stack.shape[0]} scene(s) supplied, "
            f"at least {min_scenes} required. Supply max_reflectance explicitly; "
            "no default constant is used"
        )
    if pooled:
        finite = stack[np.isfinite(stack)]
        if finite.size < min_valid_samples:
            raise ReferenceEstimationError(
                "cannot estimate the maximum reflectance: no finite pixels in the scenes"
            )
        return float(np.percentile(finite, percentile))
    n_valid = np.isfinite(stack).sum(axis=0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        ref = np.nanpercentile(stack, percentile, axis=0)
    ref = np.where(n_valid >= min_valid_samples, ref, np.nan)
    if not np.isfinite(ref).any():
        raise ReferenceEstimationError(
            "cannot estimate the maximum reflectance: no pixel has "
            f"{min_valid_samples} or more finite samples"
        )
    return np.asarray(ref, dtype=float)
