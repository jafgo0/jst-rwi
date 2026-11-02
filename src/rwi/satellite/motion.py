"""Cloud motion between two satellite frames, by phase correlation.

Why this exists: a numerical weather model cannot say that a cloud band will
cross the car in twenty-five minutes; two consecutive 0.5 km satellite frames
can say how fast the cloud field is moving. The answer is only worth anything
when the field actually translated between the frames, so every estimate here
carries a ``confidence`` and callers are expected to reject low-confidence
vectors (see :data:`MIN_USABLE_CONFIDENCE` and :func:`is_usable_motion`).

Nothing in this module has been validated against real data. There is no
ground-truth wind or cloud-motion reference in this platform. The synthetic
tests show that the arithmetic and the signs are right; they do not show that
real clouds behave like translating fields.

Method
------
For frames ``a`` (earlier) and ``b`` (later), if ``b(x) = a(x - d)`` then their
Fourier transforms satisfy ``B = A * exp(-i w.d)``. The normalised cross-power
spectrum ``R = B conj(A) / |B conj(A)|`` is therefore a pure phase ramp whose
inverse transform is a delta function at ``d``. That peak location is the shift
of the *content* in array index space (rows down, columns right as stored), and
is converted to east/north using the grid's coordinate order.

Pre-processing, in this order:

1. **Validity mask.** A pixel is used only where it is finite in *both* frames.
   Using the intersection stops a gap in one frame from looking like structure.
   Fewer than :data:`MIN_VALID_FRACTION` valid pixels raises.
2. **Mean removal** over the valid pixels (otherwise the DC term dominates).
3. **Hann window** multiplied by the validity mask, in 2-D (outer product of 1-D
   Hann windows). The FFT assumes periodic data; without tapering, the
   mismatch between the left/right and top/bottom edges produces a strong
   spurious peak at zero shift and wraps cloud across the boundary. The window
   removes the edge discontinuity at the cost of down-weighting border pixels,
   so clouds that sit mostly in the outer ~10% of the domain contribute little.
4. **NaN handling.** Invalid pixels are set to zero *after* mean removal (so
   they are neutral) and carry zero window weight. NaN is never passed to the
   FFT, so it cannot spread through the transform.

Sub-pixel refinement: the integer peak is refined by evaluating the
cross-power correlation on a ``1/UPSAMPLE`` pixel grid within +/-1 pixel of the
integer peak with a matrix-multiply (local, upsampled) DFT in the style of
Guizar-Sicairos et al. 2008, with ``UPSAMPLE = 50``. This is a resolution of
0.02 px; the accuracy is limited by noise and by the window, not by the
resolution. Measured error on the synthetic fields in
``tests/unit/test_satellite_motion.py`` is stated there; it is a property of
those fields.

Confidence
----------
``confidence`` is derived from the peak-to-sidelobe ratio of the integer-pixel
correlation surface::

    psr        = (peak - mean(sidelobes)) / std(sidelobes)
    confidence = clip(1 - NOISE_PSR / psr, 0, 1)      # NOISE_PSR = 8

where the sidelobes are all surface samples more than 5 pixels (wrapped) from
the peak. A sharp, isolated peak (a field that translated) has a ``psr`` many
times ``NOISE_PSR``; a flat surface (noise, or a field that changed shape) has
a peak no taller than the largest noise sample, so ``confidence`` is about 0.
It is a heuristic score in [0, 1], not a probability.

``NOISE_PSR = 8`` is empirical: over 40 trials of 256x256 white noise and
independent cloud-like fields, the largest PSR seen was 7.5 and the mean 5.4.
(The textbook ``sqrt(2 ln N)`` = 4.7 for N independent samples underestimates
this because the low-passed surface is spatially correlated.)

Threshold: :data:`MIN_USABLE_CONFIDENCE` = 0.5, i.e. ``psr >= 16``, twice the
noise ceiling. In the synthetic noise experiment (see
``docs/science/nowcasting.md``) that corresponds to a median shift error of
about 0.7 px, about 0.6 m/s at 0.5 km pixels and a 10 minute interval. The
threshold comes from synthetic fields, not real imagery, and should be re-tuned
once real frames and a verification score exist.

Spectral low-pass: the whitened cross-power spectrum is multiplied by a
Gaussian ``exp(-(|f| / 0.1)^2)`` (f in cycles per pixel). Whitening gives every
frequency equal weight, so without this the noise-dominated high frequencies
swamp the signal. In the experiments it cut the median error at moderate noise
from 0.86 px to 0.2 px. Cost: structure smaller than roughly 10 px (5 km) does
not contribute to the motion estimate.

Refusals (``ValueError`` subclasses so callers can catch broadly):

* array/grid shape mismatch, wrong dimensionality, tiny arrays;
* interval not positive, shorter than :data:`MIN_INTERVAL_S` or longer than
  :data:`MAX_INTERVAL_S`;
* too few valid pixels, or a field with no spatial structure
  (:class:`InsufficientStructureError`) - a uniformly clear or uniformly
  overcast scene has no defined motion, and returning "0 m/s" would be a lie;
* ``resolution_km`` that disagrees with the grid coordinates by more than
  about 40 percent.

Speeds above :data:`MAX_PLAUSIBLE_SPEED_M_S` are returned with confidence 0.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

import numpy as np
from numpy.typing import NDArray

from rwi.contracts import CloudMotion, GeoGrid
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import ensure_utc, utc_now

__all__ = [
    "MAX_INTERVAL_S",
    "MAX_PLAUSIBLE_SPEED_M_S",
    "MIN_INTERVAL_S",
    "MIN_USABLE_CONFIDENCE",
    "InsufficientStructureError",
    "LocalMotion",
    "estimate_motion",
    "estimate_motion_blocks",
    "is_usable_motion",
    "pixel_pitch_m",
]

FloatArray = NDArray[np.float64]

MIN_USABLE_CONFIDENCE = 0.5
"""Estimates below this confidence must not be used to advect anything."""

MIN_INTERVAL_S = 120.0
"""Shorter than this a 0.1 px error becomes a speed error of order 0.4 m/s at
0.5 km pixels, and Himawari does not deliver frames this fast anyway."""

MAX_INTERVAL_S = 3600.0
"""Longer than this the cloud field has usually changed shape too much."""

MAX_PLAUSIBLE_SPEED_M_S = 80.0
"""Cirrus in a strong jet can approach this; faster is an artefact."""

MIN_VALID_FRACTION = 0.25
MIN_FIELD_STD = 1e-3
"""Smallest standard deviation (cloud-index units, ~[0, 1]) that counts as structure."""

MIN_ARRAY_SIDE = 16
UPSAMPLE = 50
_SIDELOBE_EXCLUSION_PX = 5
NOISE_PSR = 8.0
LOWPASS_CYCLES_PER_PX = 0.1
_RESOLUTION_TOLERANCE = (0.6, 1.7)
_M_PER_DEG_LAT = 111_195.0
_M_PER_DEG_LON_EQUATOR = 111_320.0

_METHOD = (
    "phase_correlation(hann_window,validity_mask,mean_removed,"
    f"gaussian_lowpass,local_dft_subpixel_x{UPSAMPLE},psr_confidence)"
)


class InsufficientStructureError(ValueError):
    """The frames have too little spatial structure for motion to be defined."""


@dataclass(frozen=True)
class LocalMotion:
    """EXTRA: one block's motion vector from :func:`estimate_motion_blocks`."""

    latitude_deg: float
    longitude_deg: float
    row_slice: tuple[int, int]
    col_slice: tuple[int, int]
    motion: CloudMotion

    @property
    def is_usable(self) -> bool:
        return is_usable_motion(self.motion)


def is_usable_motion(motion: CloudMotion) -> bool:
    """Whether a motion estimate is trustworthy enough to advect with."""
    return motion.confidence >= MIN_USABLE_CONFIDENCE


# --------------------------------------------------------------------------
# grid geometry
# --------------------------------------------------------------------------


def _axis_sign(coords: list[float], name: str, expected_len: int) -> float:
    if len(coords) != expected_len:
        raise ValueError(f"grid {name} has {len(coords)} entries but shape needs {expected_len}")
    if expected_len < 2:
        raise ValueError(f"grid {name} needs at least two entries to define a direction")
    diffs = np.diff(np.asarray(coords, dtype=float))
    if np.all(diffs > 0):
        return 1.0
    if np.all(diffs < 0):
        return -1.0
    raise ValueError(f"grid {name} must be strictly monotonic")


def _check_resolution(grid: GeoGrid) -> None:
    """Refuse a grid whose stated resolution disagrees with its coordinates."""
    if grid.resolution_km <= 0:
        raise ValueError("grid resolution_km must be positive")
    lat = np.asarray(grid.latitudes, dtype=float)
    lon = np.asarray(grid.longitudes, dtype=float)
    dlat_m = abs(float(np.mean(np.diff(lat)))) * _M_PER_DEG_LAT
    mid_lat = float(np.mean(lat))
    dlon_m = (
        abs(float(np.mean(np.diff(lon)))) * _M_PER_DEG_LON_EQUATOR * math.cos(math.radians(mid_lat))
    )
    lo, hi = _RESOLUTION_TOLERANCE
    for name, spacing in (("latitude", dlat_m), ("longitude", dlon_m)):
        ratio = spacing / (grid.resolution_km * 1000.0)
        if not lo <= ratio <= hi:
            raise ValueError(
                f"grid resolution_km={grid.resolution_km} disagrees with the {name} "
                f"spacing ({spacing:.0f} m per pixel): ratio {ratio:.2f}"
            )


def pixel_pitch_m(grid: GeoGrid) -> float:
    """Metres per pixel, taken from ``grid.resolution_km`` for both axes.

    The coordinates are only used to confirm it is not wildly wrong. On a regular
    lat/lon grid the east-west pitch differs from the north-south pitch by
    ``cos(lat)``-type factors; using one number is a documented simplification
    worth up to roughly 10 percent in speed at the latitudes of this race.
    """
    _check_resolution(grid)
    return grid.resolution_km * 1000.0


# --------------------------------------------------------------------------
# core phase correlation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Shift:
    d_row: float
    d_col: float
    psr: float
    confidence: float
    valid_fraction: float


def _hann2d(shape: tuple[int, int]) -> FloatArray:
    return np.outer(np.hanning(shape[0] + 2)[1:-1], np.hanning(shape[1] + 2)[1:-1])


def _phase_correlate(earlier: FloatArray, later: FloatArray) -> _Shift:
    """Shift of content from ``earlier`` to ``later`` in (rows, cols), plus quality."""
    a = np.asarray(earlier, dtype=np.float64)
    b = np.asarray(later, dtype=np.float64)
    valid = np.isfinite(a) & np.isfinite(b)
    n_valid = int(valid.sum())
    if n_valid == 0:
        raise ValueError("no pixel is finite in both frames: cannot estimate motion")
    fraction = n_valid / valid.size
    if fraction < MIN_VALID_FRACTION:
        raise ValueError(
            f"only {fraction:.0%} of pixels are valid in both frames "
            f"(minimum {MIN_VALID_FRACTION:.0%})"
        )
    a_mean = float(a[valid].mean())
    b_mean = float(b[valid].mean())
    a0 = np.where(valid, a - a_mean, 0.0)
    b0 = np.where(valid, b - b_mean, 0.0)
    if float(a0[valid].std()) < MIN_FIELD_STD or float(b0[valid].std()) < MIN_FIELD_STD:
        raise InsufficientStructureError(
            "a frame has no spatial structure (uniform field): motion is undefined"
        )

    weight = _hann2d(a.shape) * valid
    fa = np.fft.fft2(a0 * weight)
    fb = np.fft.fft2(b0 * weight)
    cross = fb * np.conj(fa)
    magnitude = np.abs(cross)
    floor = 1e-9 * float(magnitude.max())
    spectrum = cross / np.maximum(magnitude, max(floor, 1e-300))
    spectrum[magnitude <= floor] = 0.0
    fy = np.fft.fftfreq(a.shape[0])
    fx = np.fft.fftfreq(a.shape[1])
    radius = np.sqrt(fy[:, None] ** 2 + fx[None, :] ** 2)
    spectrum = spectrum * np.exp(-((radius / LOWPASS_CYCLES_PER_PX) ** 2))

    surface = np.fft.ifft2(spectrum).real
    n_rows, n_cols = surface.shape
    peak_idx = np.unravel_index(int(np.argmax(surface)), surface.shape)
    pk_r, pk_c = int(peak_idx[0]), int(peak_idx[1])
    peak = float(surface[pk_r, pk_c])

    # Peak-to-sidelobe ratio, with wrapped distance from the peak.
    rr = np.abs(((np.arange(n_rows) - pk_r + n_rows // 2) % n_rows) - n_rows // 2)
    cc = np.abs(((np.arange(n_cols) - pk_c + n_cols // 2) % n_cols) - n_cols // 2)
    near = (rr[:, None] <= _SIDELOBE_EXCLUSION_PX) & (cc[None, :] <= _SIDELOBE_EXCLUSION_PX)
    side = surface[~near]
    side_mean = float(side.mean())
    side_std = max(float(side.std()), 1e-12 * max(abs(peak), 1e-300))
    psr = (peak - side_mean) / side_std
    confidence = float(np.clip(1.0 - NOISE_PSR / psr, 0.0, 1.0)) if psr > 0 else 0.0

    # Local upsampled DFT around the integer peak.
    int_r = pk_r - n_rows if pk_r > n_rows // 2 else pk_r
    int_c = pk_c - n_cols if pk_c > n_cols // 2 else pk_c
    offsets = np.arange(-UPSAMPLE, UPSAMPLE + 1) / UPSAMPLE
    d_rows = int_r + offsets
    d_cols = int_c + offsets
    f_r = np.fft.fftfreq(n_rows)
    f_c = np.fft.fftfreq(n_cols)
    e_r = np.exp(2j * np.pi * d_rows[:, None] * f_r[None, :])
    e_c = np.exp(2j * np.pi * f_c[:, None] * d_cols[None, :])
    fine = (e_r @ spectrum @ e_c).real
    fi = np.unravel_index(int(np.argmax(fine)), fine.shape)
    return _Shift(
        d_row=float(d_rows[int(fi[0])]),
        d_col=float(d_cols[int(fi[1])]),
        psr=float(psr),
        confidence=confidence,
        valid_fraction=fraction,
    )


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


def _validate_times(
    earlier_time: datetime, later_time: datetime
) -> tuple[datetime, datetime, float]:
    t0 = ensure_utc(earlier_time)
    t1 = ensure_utc(later_time)
    dt = (t1 - t0).total_seconds()
    if dt <= 0:
        raise ValueError("later_time must be after earlier_time")
    if dt < MIN_INTERVAL_S:
        raise ValueError(
            f"interval {dt:.0f} s is shorter than the {MIN_INTERVAL_S:.0f} s minimum: "
            "sub-pixel noise would dominate the speed"
        )
    if dt > MAX_INTERVAL_S:
        raise ValueError(
            f"interval {dt:.0f} s is longer than the {MAX_INTERVAL_S:.0f} s maximum: "
            "the cloud field will have changed shape"
        )
    return t0, t1, dt


def _check_inputs(earlier: FloatArray, later: FloatArray, grid: GeoGrid) -> tuple[float, float]:
    a = np.asarray(earlier)
    b = np.asarray(later)
    if a.ndim != 2 or b.ndim != 2:
        raise ValueError("frames must be 2-D arrays")
    if a.shape != b.shape:
        raise ValueError(f"frame shapes differ: {a.shape} vs {b.shape}")
    if tuple(grid.shape) != a.shape:
        raise ValueError(f"grid shape {tuple(grid.shape)} does not match frames {a.shape}")
    if min(a.shape) < MIN_ARRAY_SIDE:
        raise ValueError(f"frames must be at least {MIN_ARRAY_SIDE} pixels on a side")
    row_sign = _axis_sign(grid.latitudes, "latitudes", a.shape[0])
    col_sign = _axis_sign(grid.longitudes, "longitudes", a.shape[1])
    return row_sign, col_sign


def _motion_from_shift(
    shift: _Shift,
    *,
    pitch_m: float,
    row_sign: float,
    col_sign: float,
    dt: float,
    t0: datetime,
    t1: datetime,
    extra: str = "",
) -> CloudMotion:
    # Array rows increase northward when latitudes ascend (row_sign = +1).
    u = col_sign * shift.d_col * pitch_m / dt
    v = row_sign * shift.d_row * pitch_m / dt
    speed = math.hypot(u, v)
    confidence = shift.confidence
    note = ""
    if speed > MAX_PLAUSIBLE_SPEED_M_S:
        confidence = 0.0
        note = f";REJECTED:speed>{MAX_PLAUSIBLE_SPEED_M_S:.0f}m/s"
    direction = math.degrees(math.atan2(u, v)) % 360.0 if speed > 0 else 0.0
    method = (
        f"{_METHOD};shift_px(row,col)=({shift.d_row:+.2f},{shift.d_col:+.2f});"
        f"psr={shift.psr:.1f};valid={shift.valid_fraction:.2f}"
        f"{';direction_undefined_at_zero_speed' if speed == 0 else ''}{note}{extra}"
    )
    return CloudMotion(
        from_time_utc=t0,
        to_time_utc=t1,
        u_east_m_s=u,
        v_north_m_s=v,
        speed_m_s=speed,
        direction_to_deg=direction,
        confidence=confidence,
        method=method,
        provenance=Provenance(
            provider="rwi",
            dataset="cloud-motion",
            kind=DataKind.DERIVED,
            retrieved_at_utc=utc_now(),
            valid_time_utc=t1,
            model_name="phase-correlation",
            model_version="1",
            spatial_resolution_km=pitch_m / 1000.0,
            temporal_resolution_s=dt,
            processing="hann window, validity mask, local upsampled DFT sub-pixel peak",
            notes=(
                "single translation vector for the whole domain; derived from image "
                "texture, not measured wind; not validated against any reference"
            ),
        ),
    )


def estimate_motion(
    earlier: FloatArray,
    later: FloatArray,
    grid: GeoGrid,
    *,
    earlier_time: datetime,
    later_time: datetime,
) -> CloudMotion:
    """Estimate the single translation vector of the cloud field between two frames.

    ``earlier`` and ``later`` are cloud-index (or reflectance) fields on the same
    ``grid``; non-finite pixels are gaps. The returned vector is where the field
    moves TOWARDS (``direction_to_deg`` is a compass bearing, 90 = moving east).

    Windowing (Hann), NaN handling, sub-pixel method and the confidence measure
    are described in the module docstring. Check ``is_usable_motion`` before
    using the vector: ``confidence < MIN_USABLE_CONFIDENCE`` means "do not use".

    Raises ``ValueError`` (or :class:`InsufficientStructureError`) rather than
    guessing; see the module docstring for the list.
    """
    row_sign, col_sign = _check_inputs(earlier, later, grid)
    t0, t1, dt = _validate_times(earlier_time, later_time)
    pitch = pixel_pitch_m(grid)
    shift = _phase_correlate(np.asarray(earlier), np.asarray(later))
    return _motion_from_shift(
        shift, pitch_m=pitch, row_sign=row_sign, col_sign=col_sign, dt=dt, t0=t0, t1=t1
    )


def estimate_motion_blocks(
    earlier: FloatArray,
    later: FloatArray,
    grid: GeoGrid,
    *,
    earlier_time: datetime,
    later_time: datetime,
    block_px: int = 64,
) -> list[LocalMotion]:
    """EXTRA, not required by the contract: one vector per non-overlapping block.

    Blocks that raise (uniform, mostly NaN) are omitted. Blocks are small, so
    each vector is noisier than the domain-wide one and cannot see shifts
    beyond about a third of ``block_px``; check each ``is_usable``. Nothing in
    the nowcast uses these yet: the advection assumes one vector per domain.
    """
    row_sign, col_sign = _check_inputs(earlier, later, grid)
    t0, t1, dt = _validate_times(earlier_time, later_time)
    pitch = pixel_pitch_m(grid)
    if block_px < MIN_ARRAY_SIDE:
        raise ValueError(f"block_px must be at least {MIN_ARRAY_SIDE}")
    a = np.asarray(earlier)
    b = np.asarray(later)
    out: list[LocalMotion] = []
    for r0 in range(0, a.shape[0] - block_px + 1, block_px):
        for c0 in range(0, a.shape[1] - block_px + 1, block_px):
            rs = slice(r0, r0 + block_px)
            cs = slice(c0, c0 + block_px)
            try:
                shift = _phase_correlate(a[rs, cs], b[rs, cs])
            except ValueError:
                continue
            motion = _motion_from_shift(
                shift,
                pitch_m=pitch,
                row_sign=row_sign,
                col_sign=col_sign,
                dt=dt,
                t0=t0,
                t1=t1,
                extra=f";block_px={block_px}",
            )
            rc = r0 + block_px // 2
            cc = c0 + block_px // 2
            out.append(
                LocalMotion(
                    latitude_deg=float(grid.latitudes[rc]),
                    longitude_deg=float(grid.longitudes[cc]),
                    row_slice=(r0, r0 + block_px),
                    col_slice=(c0, c0 + block_px),
                    motion=motion,
                )
            )
    return out
