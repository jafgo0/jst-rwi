"""Advection nowcast of satellite cloud index to point irradiance.

Why this exists: the decision this platform supports is "slow down now to bank
charge" versus "push, it stays clear". A numerical model on a 9-25 km grid
updated every six hours cannot answer that for a cloud band 25 minutes away.
Satellite imagery can - within severe limits that this module encodes rather
than footnotes.

THE SKILL OF THIS NOWCAST IS UNKNOWN. There is no ground-truth irradiance
measurement anywhere in this platform, so nothing here has been validated
against real data. Every result carries ``validation_status`` saying so, and
every result carries :func:`persistence_nowcast`'s baseline so that the
verification module can later settle whether advection earns anything.

What is modelled
----------------
1. The cloud-index field is assumed to *translate* with one vector for the
   whole domain (from :mod:`rwi.satellite.motion`). Clouds neither grow, decay,
   nor change layer. That assumption fails exactly when it matters most:
   convective build-up over central Australia on a hot afternoon.
2. The value at the point at lead ``t`` is the field sampled upstream, at
   ``point - motion * (image_age + lead)``, by bilinear interpolation.
3. Cloud index is mapped to a clear-sky index ``k`` and then ``GHI = k *
   clear_sky_GHI``. The default mapping is the Heliosat-2 style piecewise
   function (Rigollier et al. 2004); pass ``to_clear_sky_index`` to use the
   retrieval module's own mapping. Neither is validated here.

Time arithmetic (the part a strategist must not get wrong)
----------------------------------------------------------
A frame arrives about :data:`DEFAULT_IMAGE_LATENCY_S` = 840 s (14 min) after its
nominal slot time. A nowcast issued on arrival for "30 minutes from now" must
therefore carry the cloud field forward ``840 + 1800 = 2640 s``: it is a
30-minute forecast *from an observation already 14 minutes old*. This is
exposed as ``image_age_s`` and ``effective_lead_s`` on every result. The
nominal slot time is the scan start; the full disk is scanned over several
minutes, so a pixel over central Australia may have been observed some minutes
later than its label. That is not corrected here.

Horizon cap: the *effective* lead (image age + requested lead) may not exceed
:data:`MAX_EFFECTIVE_LEAD_S` = 7200 s (2 h). Skill of pure advection decays
quickly - clouds grow, decay and change speed over tens of minutes - and is
usually poor beyond about two hours, so a longer request is refused rather
than answered with a confident-looking number. With the nominal 14 minute age
that leaves 106 minutes of requested lead.

Domain edges: where the upstream location falls outside the image the answer
is unknown, not clear. Samples are never filled with zero; the series is cut
at the first unknown sample and the result says so (``domain_truncated``).

Refusals: a scene whose visible band is unusable (``is_usable_visible`` False,
including unknown solar zenith) raises :class:`NowcastRefusedError`, as does an
unknown value at the point, a point outside the grid, or a horizon beyond the
cap. If the cloud motion cannot be estimated or its confidence is below
:data:`rwi.satellite.motion.MIN_USABLE_CONFIDENCE`, the result falls back to
persistence and says so in ``method`` and ``limitations``; it never advects with
a vector it does not trust.

Not validated, not operationally ready.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from itertools import pairwise

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict
from scipy import ndimage

from rwi.contracts import CloudMotion, GeoGrid, IrradianceNowcast, SatelliteScene
from rwi.core.errors import RwiError
from rwi.core.geo import GeoPoint, initial_bearing_deg
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import ensure_utc
from rwi.satellite.motion import (
    MIN_USABLE_CONFIDENCE,
    estimate_motion,
    is_usable_motion,
    pixel_pitch_m,
)

__all__ = [
    "DEFAULT_IMAGE_LATENCY_S",
    "MAX_EFFECTIVE_LEAD_S",
    "NowcastComparison",
    "NowcastRefusedError",
    "SatelliteNowcast",
    "advect",
    "default_clear_sky_index",
    "nowcast_point",
    "nowcast_with_baseline",
    "parallax_displacement_km",
    "persistence_nowcast",
]

FloatArray = NDArray[np.float64]
ClearSky = Callable[[datetime], float] | Sequence[float]

DEFAULT_IMAGE_LATENCY_S = 840.0
"""Measured 2026-11-02: slot 17:50Z created 18:04Z, 14 minutes after the slot."""

MAX_EFFECTIVE_LEAD_S = 7200.0
"""Image age plus requested lead may not exceed this (see module docstring)."""

SATELLITE_LONGITUDE_DEG = 140.7
_EARTH_RADIUS_KM = 6378.137
_SAT_HEIGHT_KM = 35785.831
_REFERENCE_CLOUD_HEIGHTS_M = (2000.0, 10000.0)
_MIN_STEP_S = 60.0
_SNAP = 1e-9

_METHOD_ADVECTION = (
    "satellite cloud-index advection: phase-correlation motion, bilinear upstream sample, "
    "Heliosat-style clear-sky index; clouds translate, no growth or decay"
)
_METHOD_PERSISTENCE = (
    "persistence of satellite cloud index: latest frame held fixed, no motion, "
    "clear-sky GHI re-evaluated at each valid time"
)


class NowcastRefusedError(RwiError, ValueError):
    """The nowcast cannot be produced honestly; no number is returned."""


class SatelliteNowcast(IrradianceNowcast):
    """:class:`~rwi.contracts.IrradianceNowcast` plus the time arithmetic.

    A subclass of the contract type (``isinstance`` holds, it serialises to a
    superset), adding what a strategist needs and the contract has no field for.
    """

    model_config = ConfigDict(frozen=True)

    image_time_utc: datetime
    image_age_s: float
    requested_lead_s: list[float]
    effective_lead_s: list[float]
    max_effective_lead_s: float
    domain_truncated: bool = False
    used_motion: bool = True


class NowcastComparison(BaseModel):
    """Advection nowcast and its persistence baseline, on the same time axis."""

    model_config = ConfigDict(frozen=True)

    advection: SatelliteNowcast
    persistence: SatelliteNowcast
    ghi_difference_w_m2: list[float]
    """advection minus persistence at the common valid times (advection's length)."""


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------


def _signs(grid: GeoGrid) -> tuple[float, float]:
    lat = np.diff(np.asarray(grid.latitudes, dtype=float))
    lon = np.diff(np.asarray(grid.longitudes, dtype=float))
    if lat.size == 0 or lon.size == 0 or not (np.all(lat > 0) or np.all(lat < 0)):
        raise ValueError("grid latitudes must be strictly monotonic with at least two rows")
    if not (np.all(lon > 0) or np.all(lon < 0)):
        raise ValueError("grid longitudes must be strictly monotonic")
    return (1.0 if lat[0] > 0 else -1.0), (1.0 if lon[0] > 0 else -1.0)


def _snap(value: float) -> float:
    rounded = round(value)
    return float(rounded) if abs(value - rounded) < _SNAP else value


def _pixel_shift(
    grid: GeoGrid, motion: CloudMotion, seconds: float
) -> tuple[float, float]:
    """Displacement of the content, in array (rows, cols), after ``seconds``."""
    row_sign, col_sign = _signs(grid)
    pitch = pixel_pitch_m(grid)
    d_row = row_sign * motion.v_north_m_s * seconds / pitch
    d_col = col_sign * motion.u_east_m_s * seconds / pitch
    return _snap(d_row), _snap(d_col)


def _fractional_index(coords: Sequence[float], value: float, name: str) -> float:
    arr = np.asarray(coords, dtype=float)
    idx = np.arange(arr.size, dtype=float)
    if arr[0] > arr[-1]:
        arr, idx = arr[::-1], idx[::-1]
    if not arr[0] <= value <= arr[-1]:
        raise NowcastRefusedError(
            f"point {name} {value} is outside the satellite grid [{arr[0]}, {arr[-1]}]"
        )
    return float(np.interp(value, arr, idx))


# --------------------------------------------------------------------------
# advection
# --------------------------------------------------------------------------


def advect(
    values: FloatArray, grid: GeoGrid, motion: CloudMotion, *, seconds: float
) -> FloatArray:
    """Carry a field forward by ``motion`` for ``seconds``.

    The content moves WITH the vector: a cloud under ``u > 0`` ends up further
    east. Whole-pixel shifts are exact copies; fractional shifts use bilinear
    interpolation (which smooths sharp edges slightly).

    Edge handling: pixels whose upstream source lies outside the domain are
    UNKNOWN and are returned as NaN, never as zero (zero would claim clear sky).
    NaN already in the input stays NaN and, with bilinear interpolation, also
    spreads to the neighbours that would have used it. ``seconds=0`` returns an
    unchanged copy. Negative seconds back-advect. The caller is responsible for
    checking that ``motion`` is usable; this function does not judge.
    """
    field = np.asarray(values, dtype=np.float64)
    if field.ndim != 2 or tuple(grid.shape) != field.shape:
        raise ValueError(f"values {field.shape} do not match grid shape {tuple(grid.shape)}")
    if not math.isfinite(seconds):
        raise ValueError("seconds must be finite")
    d_row, d_col = _pixel_shift(grid, motion, seconds)
    if d_row == 0.0 and d_col == 0.0:
        return field.copy()
    n_rows, n_cols = field.shape
    out = np.full_like(field, np.nan)
    if d_row == int(d_row) and d_col == int(d_col):
        i, j = int(d_row), int(d_col)
        if abs(i) >= n_rows or abs(j) >= n_cols:
            return out
        dst_r = slice(max(i, 0), n_rows + min(i, 0))
        src_r = slice(max(-i, 0), n_rows + min(-i, 0))
        dst_c = slice(max(j, 0), n_cols + min(j, 0))
        src_c = slice(max(-j, 0), n_cols + min(-j, 0))
        out[dst_r, dst_c] = field[src_r, src_c]
        return out
    rows, cols = np.meshgrid(
        np.arange(n_rows, dtype=float) - d_row,
        np.arange(n_cols, dtype=float) - d_col,
        indexing="ij",
    )
    result: FloatArray = ndimage.map_coordinates(
        field, [rows, cols], order=1, mode="constant", cval=np.nan
    )
    return result


def _sample_upstream(
    values: FloatArray,
    grid: GeoGrid,
    point_rc: tuple[float, float],
    motion: CloudMotion | None,
    seconds: float,
) -> float:
    """Value at the point after advecting for ``seconds`` (NaN when unknown)."""
    d_row, d_col = (0.0, 0.0) if motion is None else _pixel_shift(grid, motion, seconds)
    src_r = point_rc[0] - d_row
    src_c = point_rc[1] - d_col
    n_rows, n_cols = values.shape
    if not (0.0 <= src_r <= n_rows - 1 and 0.0 <= src_c <= n_cols - 1):
        return float("nan")
    sampled = ndimage.map_coordinates(
        values, [[src_r], [src_c]], order=1, mode="constant", cval=np.nan
    )
    return float(sampled[0])


# --------------------------------------------------------------------------
# cloud index -> clear-sky index
# --------------------------------------------------------------------------


def default_clear_sky_index(cloud_index: FloatArray) -> FloatArray:
    """Map cloud index ``n`` to clear-sky index ``k*``, via the ONE implementation.

    This used to carry its own copy of the Heliosat piecewise relation, written
    from memory. Two copies of a relation that is itself not yet verified
    against its primary source is strictly worse than one: they can drift
    apart, and a correction applied to the authoritative one would silently
    leave the nowcast using the old numbers.

    So this delegates to :func:`rwi.satellite.cloudindex.clear_sky_index`,
    which carries the verification record. The import is lazy to keep the two
    modules independently testable.

    Raises rather than falling back. A fallback here would reintroduce exactly
    the second implementation this function exists to remove.
    """
    from rwi.satellite.cloudindex import clear_sky_index

    result: FloatArray = np.asarray(clear_sky_index(cloud_index), dtype=np.float64)
    return result


# --------------------------------------------------------------------------
# parallax
# --------------------------------------------------------------------------


def parallax_displacement_km(point: GeoPoint, cloud_height_m: float) -> tuple[float, float]:
    """Rough image displacement of a cloud top at ``cloud_height_m`` above ``point``.

    Returns ``(distance_km, bearing_deg)``: the satellite places the cloud
    ``distance_km`` from its true ground position, along ``bearing_deg`` (away
    from the sub-satellite point at 0 N, 140.7 E). Spherical Earth, flat-layer
    approximation ``h * tan(satellite_zenith)``; good to perhaps 10 percent for
    heights of a few kilometres, which is far better than the unknown cloud
    height. The sun-side shadow offset (``h * tan(solar_zenith)``, opposite the
    Sun) is a second, separate displacement that is NOT included.
    """
    lat = math.radians(point.latitude_deg)
    dlon = math.radians(point.longitude_deg - SATELLITE_LONGITUDE_DEG)
    cos_gamma = math.cos(lat) * math.cos(dlon)
    gamma = math.acos(max(-1.0, min(1.0, cos_gamma)))
    ratio = _EARTH_RADIUS_KM / (_EARTH_RADIUS_KM + _SAT_HEIGHT_KM)
    denominator = cos_gamma - ratio
    if denominator <= 0:
        raise ValueError("point is beyond the satellite's horizon")
    tan_zenith = math.sin(gamma) / denominator
    distance = (cloud_height_m / 1000.0) * tan_zenith
    bearing = initial_bearing_deg(
        GeoPoint(latitude_deg=0.0, longitude_deg=SATELLITE_LONGITUDE_DEG), point
    )
    return distance, bearing


# --------------------------------------------------------------------------
# nowcast
# --------------------------------------------------------------------------


def _check_inputs(
    scenes: Sequence[SatelliteScene], values: Sequence[FloatArray]
) -> tuple[list[SatelliteScene], list[FloatArray]]:
    if len(scenes) == 0 or len(scenes) != len(values):
        raise ValueError("scenes and values must be non-empty and the same length")
    pairs = sorted(zip(scenes, values, strict=True), key=lambda p: p[0].slot_time_utc)
    ordered = [p[0] for p in pairs]
    arrays = [np.asarray(p[1], dtype=np.float64) for p in pairs]
    first = ordered[0]
    for scene, arr in zip(ordered, arrays, strict=True):
        if scene.quantity != "cloud_index":
            raise ValueError(
                f"scene quantity is {scene.quantity!r}; nowcasting needs a cloud_index field"
            )
        if scene.grid != first.grid:
            raise ValueError("all scenes must be on the identical grid")
        if arr.shape != tuple(scene.grid.shape):
            raise ValueError(f"values shape {arr.shape} does not match grid {scene.grid.shape}")
    times = [ensure_utc(s.slot_time_utc) for s in ordered]
    if any(b <= a for a, b in pairwise(times)):
        raise ValueError("scene slot times must be distinct")
    latest = ordered[-1]
    if not latest.is_usable_visible:
        raise NowcastRefusedError(
            "latest scene is not usable in the visible band (night, low sun or unknown solar "
            f"zenith: {latest.solar_zenith_range_deg}); refusing rather than returning a number"
        )
    return ordered, arrays


def _clear_sky_values(clear_sky: ClearSky, times: list[datetime]) -> list[float]:
    if callable(clear_sky):
        out = [float(clear_sky(t)) for t in times]
    else:
        out = [float(v) for v in clear_sky]
        if len(out) != len(times):
            raise ValueError(
                f"clear_sky sequence has {len(out)} values but the nowcast needs {len(times)}"
            )
    if any((not math.isfinite(v)) or v < 0 for v in out):
        raise ValueError("clear-sky GHI must be finite and non-negative")
    return out


def _limitations(
    *,
    point: GeoPoint,
    scene: SatelliteScene,
    image_age_s: float,
    horizon_s: float,
    used_motion: bool,
    motion: CloudMotion | None,
    motion_note: str,
    truncated: bool,
) -> list[str]:
    eff_first = image_age_s
    eff_last = image_age_s + horizon_s
    lims = [
        "NOT VALIDATED: no ground-truth irradiance exists in this platform, so the skill of "
        "this nowcast, and whether it beats persistence, is unknown.",
        (
            "Pure advection assumes clouds move but neither grow nor dissipate. It fails "
            "exactly when it matters most: convective build-up over central Australia on a "
            "hot afternoon, and dissipation of thin cloud, produce errors advection cannot "
            "represent."
        ),
        (
            f"Effective lead time is image age plus requested lead: this image is "
            f"{image_age_s / 60:.1f} min old, so the series spans {eff_first / 60:.1f} to "
            f"{eff_last / 60:.1f} min of forecast from the observation. A 'requested "
            f"{horizon_s / 60:.0f} minute' nowcast is really a "
            f"{eff_last / 60:.0f}-minute forecast from a {image_age_s / 60:.0f}-minute-old "
            f"observation (see effective_lead_s)."
        ),
        (
            f"Horizon cap: effective lead is limited to {MAX_EFFECTIVE_LEAD_S / 3600:.0f} h "
            "because advection skill decays quickly (clouds change over tens of minutes) and "
            "is usually poor beyond about two hours; longer requests are refused. Skill is "
            "likely much lower than that cap suggests, and this has not been measured."
        ),
        (
            "Single motion vector for the whole domain: low and high cloud layers move "
            "differently, and the vector comes from image texture, not from measured wind."
        ),
        "The mapping from cloud index to clear-sky index is a simple piecewise function and is "
        "not calibrated to any ground measurement.",
    ]
    d2, bearing = parallax_displacement_km(point, _REFERENCE_CLOUD_HEIGHTS_M[0])
    d10, _ = parallax_displacement_km(point, _REFERENCE_CLOUD_HEIGHTS_M[1])
    lims.append(
        f"Parallax is NOT corrected: seen from the satellite at 140.7 E, a cloud top at 2 km "
        f"appears about {d2:.1f} km, and one at 10 km about {d10:.1f} km, from the ground "
        f"point it is above, displaced away from the sub-satellite point (bearing "
        f"{bearing:.0f} deg) at this location. Its shadow is displaced a further "
        "h*tan(solar zenith) away from the Sun, also uncorrected. Cloud height is unknown, "
        "so the true offset is only bracketed by these two figures."
    )
    lims.append(
        "A visible-band retrieval degrades towards sunset and is meaningless at night; the "
        "last hour of driving is the least reliable part of the day. Scene solar zenith range "
        f"is {scene.solar_zenith_range_deg} deg; valid times past sunset are not checked here "
        "beyond the clear-sky GHI supplied by the caller, which is zero at night."
    )
    lims.append(
        "Frames are labelled with the nominal scan start; the disk is scanned over several "
        "minutes, so the true observation time over this point may differ from the label by a "
        "few minutes. Not corrected."
    )
    if used_motion and motion is not None:
        lims.append(
            f"Cloud motion {motion.speed_m_s:.1f} m/s towards {motion.direction_to_deg:.0f} deg, "
            f"confidence {motion.confidence:.2f} (threshold {MIN_USABLE_CONFIDENCE:.2f}); "
            f"estimated from two frames {motion.interval_s / 60:.0f} min apart."
        )
    else:
        lims.append(
            f"No cloud motion applied: {motion_note} This result is plain persistence of "
            "the latest frame."
        )
    if truncated:
        lims.append(
            "Series cut short: the upstream location moved outside the image (or onto a gap), "
            "where the cloud field is unknown. It is not assumed clear."
        )
    return lims


def _build(
    point: GeoPoint,
    scenes: Sequence[SatelliteScene],
    values: Sequence[FloatArray],
    *,
    horizon_s: float,
    step_s: float,
    clear_sky: ClearSky,
    issued_at_utc: datetime | None,
    to_clear_sky_index: Callable[[FloatArray], FloatArray] | None,
    use_motion: bool,
) -> SatelliteNowcast:
    if not math.isfinite(horizon_s) or horizon_s < 0:
        raise ValueError("horizon_s must be finite and non-negative")
    if not math.isfinite(step_s) or step_s < _MIN_STEP_S:
        raise ValueError(f"step_s must be at least {_MIN_STEP_S:.0f} s")
    ordered, arrays = _check_inputs(scenes, values)
    latest, latest_values = ordered[-1], arrays[-1]
    grid = latest.grid
    image_time = ensure_utc(latest.slot_time_utc)
    issued = (
        ensure_utc(issued_at_utc)
        if issued_at_utc is not None
        else image_time + timedelta(seconds=DEFAULT_IMAGE_LATENCY_S)
    )
    image_age = (issued - image_time).total_seconds()
    if image_age < 0:
        raise NowcastRefusedError("issued_at_utc precedes the latest image time")
    if image_age + horizon_s > MAX_EFFECTIVE_LEAD_S:
        allowed = MAX_EFFECTIVE_LEAD_S - image_age
        raise NowcastRefusedError(
            f"effective lead {(image_age + horizon_s) / 60:.0f} min (image age "
            f"{image_age / 60:.0f} min + requested {horizon_s / 60:.0f} min) exceeds the "
            f"{MAX_EFFECTIVE_LEAD_S / 60:.0f} min cap; the longest allowed request is "
            f"{max(allowed, 0.0) / 60:.0f} min"
        )

    row_idx = _fractional_index(grid.latitudes, point.latitude_deg, "latitude")
    col_idx = _fractional_index(grid.longitudes, point.longitude_deg, "longitude")

    n_steps = math.floor(horizon_s / step_s + 1e-9) + 1
    requested = [k * step_s for k in range(n_steps)]
    valid_times = [issued + timedelta(seconds=s) for s in requested]
    cs_all = _clear_sky_values(clear_sky, valid_times)

    motion: CloudMotion | None = None
    motion_note = "persistence requested."
    if use_motion:
        if len(ordered) < 2:
            motion_note = "only one frame was supplied, so no motion could be estimated."
        else:
            try:
                candidate = estimate_motion(
                    arrays[-2],
                    latest_values,
                    grid,
                    earlier_time=ordered[-2].slot_time_utc,
                    later_time=latest.slot_time_utc,
                )
            except ValueError as exc:
                motion_note = f"motion estimation refused ({exc})."
            else:
                if is_usable_motion(candidate):
                    motion = candidate
                else:
                    motion_note = (
                        f"motion confidence {candidate.confidence:.2f} is below the "
                        f"{MIN_USABLE_CONFIDENCE:.2f} threshold (the field changed shape or "
                        "has too little texture), so the vector was rejected."
                    )
    advecting = motion is not None

    mapper = to_clear_sky_index or default_clear_sky_index
    kept_times: list[datetime] = []
    ghi: list[float] = []
    cs_kept: list[float] = []
    kcs: list[float] = []
    eff: list[float] = []
    req_kept: list[float] = []
    truncated = False
    for req, when, cs in zip(requested, valid_times, cs_all, strict=True):
        seconds = (when - image_time).total_seconds()
        n_val = _sample_upstream(latest_values, grid, (row_idx, col_idx), motion, seconds)
        if not math.isfinite(n_val):
            if not kept_times:
                raise NowcastRefusedError(
                    "cloud index is unknown at the point (gap in the image or upstream "
                    "location outside the domain)"
                )
            truncated = True
            break
        k = float(mapper(np.asarray([n_val]))[0])
        kept_times.append(when)
        kcs.append(k)
        ghi.append(k * cs)
        cs_kept.append(cs)
        eff.append(seconds)
        req_kept.append(req)

    prov = Provenance(
        provider="rwi",
        dataset="satellite-nowcast",
        kind=DataKind.DERIVED,
        retrieved_at_utc=issued,
        model_name="advection" if advecting else "persistence",
        model_version="1",
        point=point,
        spatial_resolution_km=grid.resolution_km,
        temporal_resolution_s=step_s,
        processing="bilinear sample of cloud index; clear-sky index mapping; times GHI clear-sky",
        notes=(
            f"from {latest.satellite} {latest.band} frame {image_time.isoformat()}; "
            f"image age {image_age:.0f} s; not validated"
        ),
    )
    limitations = _limitations(
        point=point,
        scene=latest,
        image_age_s=image_age,
        horizon_s=requested[len(kept_times) - 1] if kept_times else horizon_s,
        used_motion=advecting,
        motion=motion,
        motion_note=motion_note,
        truncated=truncated,
    )
    method = _METHOD_ADVECTION if advecting else _METHOD_PERSISTENCE
    if use_motion and not advecting:
        method = f"{_METHOD_PERSISTENCE} (advection requested but not applied: {motion_note})"
    return SatelliteNowcast(
        point=point,
        issued_at_utc=issued,
        valid_times_utc=kept_times,
        ghi_w_m2=ghi,
        clear_sky_ghi_w_m2=cs_kept,
        clear_sky_index=kcs,
        horizon_s=requested[len(kept_times) - 1] if kept_times else 0.0,
        motion=motion,
        method=method,
        validation_status=(
            "NOT VALIDATED: no ground-truth irradiance measurement exists for this platform, "
            "so the error of this nowcast is unknown; implemented, not validated"
        ),
        limitations=limitations,
        provenance=prov,
        image_time_utc=image_time,
        image_age_s=image_age,
        requested_lead_s=req_kept,
        effective_lead_s=eff,
        max_effective_lead_s=MAX_EFFECTIVE_LEAD_S,
        domain_truncated=truncated,
        used_motion=advecting,
    )


def nowcast_point(
    point: GeoPoint,
    scenes: Sequence[SatelliteScene],
    values: Sequence[FloatArray],
    *,
    horizon_s: float,
    step_s: float,
    clear_sky: ClearSky,
    issued_at_utc: datetime | None = None,
    to_clear_sky_index: Callable[[FloatArray], FloatArray] | None = None,
) -> SatelliteNowcast:
    """Advection nowcast of GHI at ``point`` from consecutive cloud-index scenes.

    ``scenes``/``values`` are aligned, any order (sorted internally); the last
    two frames give the cloud motion, the last one is advected. ``clear_sky`` is
    either a callable ``datetime -> clear-sky GHI [W/m2]`` or a sequence of
    values, one per valid time (``floor(horizon_s / step_s) + 1`` of them).

    Valid times are ``issued_at_utc + k * step_s``; ``issued_at_utc`` defaults to
    the latest slot time plus the measured :data:`DEFAULT_IMAGE_LATENCY_S`. The
    cloud field is carried forward by ``image_age + lead`` seconds. Returns a
    :class:`SatelliteNowcast` (a subclass of ``IrradianceNowcast``) with
    ``kind = DERIVED`` (never OBSERVATION), ``validation_status``, honest
    ``limitations`` and the effective-lead arithmetic.

    Falls back to persistence, labelled as such, when motion cannot be trusted.
    Raises :class:`NowcastRefusedError` for the refusals in the module docstring.
    """
    return _build(
        point,
        scenes,
        values,
        horizon_s=horizon_s,
        step_s=step_s,
        clear_sky=clear_sky,
        issued_at_utc=issued_at_utc,
        to_clear_sky_index=to_clear_sky_index,
        use_motion=True,
    )


def persistence_nowcast(
    point: GeoPoint,
    scenes: Sequence[SatelliteScene],
    values: Sequence[FloatArray],
    *,
    horizon_s: float,
    step_s: float,
    clear_sky: ClearSky,
    issued_at_utc: datetime | None = None,
    to_clear_sky_index: Callable[[FloatArray], FloatArray] | None = None,
) -> SatelliteNowcast:
    """Baseline: the latest frame's cloud index at the point stays put.

    The clear-sky index is held constant and GHI follows the clear-sky curve.
    Any advection nowcast that cannot beat this has earned nothing. Same
    arguments and refusals as :func:`nowcast_point`; one frame is enough.
    """
    return _build(
        point,
        scenes,
        values,
        horizon_s=horizon_s,
        step_s=step_s,
        clear_sky=clear_sky,
        issued_at_utc=issued_at_utc,
        to_clear_sky_index=to_clear_sky_index,
        use_motion=False,
    )


def nowcast_with_baseline(
    point: GeoPoint,
    scenes: Sequence[SatelliteScene],
    values: Sequence[FloatArray],
    *,
    horizon_s: float,
    step_s: float,
    clear_sky: ClearSky,
    issued_at_utc: datetime | None = None,
    to_clear_sky_index: Callable[[FloatArray], FloatArray] | None = None,
) -> NowcastComparison:
    """Return advection and persistence together so they can be verified side by side."""
    adv = nowcast_point(
        point,
        scenes,
        values,
        horizon_s=horizon_s,
        step_s=step_s,
        clear_sky=clear_sky,
        issued_at_utc=issued_at_utc,
        to_clear_sky_index=to_clear_sky_index,
    )
    per = persistence_nowcast(
        point,
        scenes,
        values,
        horizon_s=horizon_s,
        step_s=step_s,
        clear_sky=clear_sky,
        issued_at_utc=issued_at_utc,
        to_clear_sky_index=to_clear_sky_index,
    )
    diff = [a - p for a, p in zip(adv.ghi_w_m2, per.ghi_w_m2[: len(adv.ghi_w_m2)], strict=True)]
    return NowcastComparison(advection=adv, persistence=per, ghi_difference_w_m2=diff)
