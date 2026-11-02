"""Satellite-derived global horizontal irradiance (GHI): a retrieval, not a measurement.

``retrieve_ghi`` / ``retrieve_field`` turn a decoded visible-band scene into GHI:

    pixel -> cloud index n -> clear-sky index k* -> clip -> GHI = k* x GHI_clear

(:mod:`rwi.satellite.cloudindex` holds the arithmetic; the clear-sky reference
GHI comes from :mod:`rwi.solar.clearsky`.)

Honesty rules enforced here, because the platform has **no ground-truth
irradiance measurement at all**:

* The result is ``DataKind.DERIVED`` - never ``OBSERVATION``. Nothing on the
  ground measured it. Its provenance names the satellite, band, scan slot,
  method and the image's age when the retrieval was issued.
* Its error is **unknown**. ``Measured.uncertainty`` is left ``None`` rather
  than filled with an invented figure, and the quality flag is ``UNVALIDATED``.
* With the Sun low or down a visible-band retrieval is *unavailable*. That is
  a different statement from "ground-level irradiance is zero". Unavailable is
  reported as ``value=None`` (point) or NaN (field) together with a stated
  reason - never 0.0 presented as a measurement of darkness, never a number.
* Missing references, a point outside the scene, or a NaN pixel likewise give
  "unavailable with a reason", not a guess.

Validation status of every function in this module: see
``docs/science/satellite-irradiance.md``. None is validated against real data.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

import numpy as np

from rwi.contracts import IrradianceComponents, SatelliteScene, SolarPosition
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Measured, Provenance, QualityFlag
from rwi.core.timebase import ensure_utc, utc_now
from rwi.satellite.cloudindex import (
    KSTAR_CLIP_MAX,
    KSTAR_CLIP_MIN,
    ClippedIndex,
    clear_sky_index,
    clip_clear_sky_index,
    cloud_index,
    normalise_reflectance_by_sun,
)

__all__ = [
    "MAX_ALIGNMENT_S",
    "METHOD_NAME",
    "USABLE_SOLAR_ZENITH_DEG",
    "AgreementMetrics",
    "FieldRetrieval",
    "PointRetrieval",
    "RetrievalStatus",
    "compare_with_measurements",
    "retrieve_field",
    "retrieve_field_detailed",
    "retrieve_ghi",
    "retrieve_ghi_detailed",
]

METHOD_NAME: Final = "heliosat2_hammer2003_rigollier2004"
_METHOD_VERSION: Final = "0.1-unvalidated"

#: Solar zenith at and beyond which a visible-band retrieval is refused. This
#: mirrors ``SatelliteScene.is_usable_visible`` (zenith < 80 deg), which is the
#: platform-wide rule; it is repeated here only so a *point* can be gated more
#: tightly than the scene's min/max range allows.
USABLE_SOLAR_ZENITH_DEG: Final = 80.0

#: A clear-sky value or solar position computed for a time more than this many
#: seconds from the scan slot is refused as a mismatch (a 10-minute scan plus
#: margin). Silently pairing a satellite image with another time's clear-sky
#: reference would bias GHI by whatever the sun did in between.
MAX_ALIGNMENT_S: Final = 1200.0

_SLOT_CADENCE_S: Final = 600.0


class RetrievalStatus(StrEnum):
    OK = "ok"
    NIGHT_OR_LOW_SUN = "night_or_low_sun"
    UNSUPPORTED_QUANTITY = "unsupported_quantity"
    MISSING_REFERENCE = "missing_reference"
    OUTSIDE_SCENE = "outside_scene"
    NO_DATA = "no_data"


@dataclass(frozen=True)
class PointRetrieval:
    """Everything about one point retrieval, including why it failed if it did."""

    measured: Measured
    status: RetrievalStatus
    reason: str
    cloud_index: float | None = None
    clear_sky_index: float | None = None
    clipped: bool = False

    @property
    def available(self) -> bool:
        return self.status is RetrievalStatus.OK


@dataclass(frozen=True)
class FieldRetrieval:
    """A retrieved GHI field [W/m2] with the intermediate fields and the reason if refused.

    When ``status`` is not OK, ``ghi`` is all NaN and ``reason`` says why.
    """

    ghi: np.ndarray
    clear_sky_index: np.ndarray
    cloud_index: np.ndarray
    status: RetrievalStatus
    reason: str
    clip: ClippedIndex | None = None

    @property
    def available(self) -> bool:
        return self.status is RetrievalStatus.OK


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------


def _check_alignment(label: str, when: datetime, slot: datetime) -> None:
    offset = abs((ensure_utc(when) - ensure_utc(slot)).total_seconds())
    if offset > MAX_ALIGNMENT_S:
        raise ValueError(
            f"{label} is for {ensure_utc(when).isoformat()}, {offset:.0f} s from the scan slot "
            f"{ensure_utc(slot).isoformat()} (limit {MAX_ALIGNMENT_S:.0f} s): compute it at the "
            "scan time, not at 'now'"
        )


def _scene_gate(scene: SatelliteScene) -> tuple[RetrievalStatus, str] | None:
    """Scene-level refusal, or None if the scene may be retrieved from."""
    if scene.quantity == "brightness_temperature":
        return (
            RetrievalStatus.UNSUPPORTED_QUANTITY,
            "the scene holds brightness temperature (infrared); this Heliosat retrieval needs "
            "visible reflectance or a cloud index",
        )
    if scene.quantity == "reflectance" and scene.sun_normalised is None:
        # Integrator note. A Level-2 reflectance factor is usually NOT divided
        # by cos(solar zenith), while the Heliosat cloud index assumes it is.
        # Guessing either way puts a sun-angle-dependent bias into every
        # retrieval: it grows through the morning and evening, vanishes at
        # midday, and is invisible to this module's tests. With no ground-truth
        # irradiance anywhere in this platform it would never surface at all.
        # So an undeclared convention is refused, not defaulted.
        return (
            RetrievalStatus.MISSING_REFERENCE,
            "the scene does not declare whether its reflectance is sun-normalised "
            "(SatelliteScene.sun_normalised is None). Refusing rather than assuming: "
            "the wrong assumption biases every retrieval by a factor that depends on "
            "the solar zenith angle",
        )
    if not scene.is_usable_visible:
        zr = scene.solar_zenith_range_deg
        why = (
            "the scene declares no solar zenith range, so it cannot be shown to be daylight"
            if zr is None
            else f"minimum solar zenith {zr[0]:.1f} deg is at or beyond "
            f"{USABLE_SOLAR_ZENITH_DEG:.0f} deg"
        )
        return (
            RetrievalStatus.NIGHT_OR_LOW_SUN,
            f"visible-band retrieval unavailable: {why}. This is 'no retrieval', not "
            "'zero irradiance'",
        )
    return None


def _clear_sky_value(clear_sky_ghi: IrradianceComponents | float, slot: datetime) -> float:
    if isinstance(clear_sky_ghi, IrradianceComponents):
        if clear_sky_ghi.kind is not DataKind.CLEAR_SKY_MODEL:
            raise ValueError(
                f"clear_sky_ghi must be a CLEAR_SKY_MODEL result, got {clear_sky_ghi.kind.value}"
            )
        _check_alignment("clear_sky_ghi", clear_sky_ghi.time_utc, slot)
        value = clear_sky_ghi.ghi_w_m2
    else:
        value = float(clear_sky_ghi)
    if not (np.isfinite(value) and value >= 0.0):
        raise ValueError(f"clear-sky GHI must be finite and >= 0 W/m2, got {value!r}")
    return float(value)


def _check_shape(name: str, array: np.ndarray, scene: SatelliteScene) -> None:
    if array.shape != tuple(scene.grid.shape):
        raise ValueError(f"{name} has shape {array.shape}, scene grid is {scene.grid.shape}")


def _nearest_pixel(scene: SatelliteScene, point: GeoPoint) -> tuple[int, int] | None:
    """Nearest pixel centre, or None when the point is more than one pixel off the grid."""
    lats = np.asarray(scene.grid.latitudes, dtype=float)
    lons = np.asarray(scene.grid.longitudes, dtype=float)
    if lats.size == 0 or lons.size == 0:
        return None
    pix_deg = scene.grid.resolution_km / 111.32
    dlat = float(np.median(np.abs(np.diff(lats)))) if lats.size > 1 else pix_deg
    dlon = float(np.median(np.abs(np.diff(lons)))) if lons.size > 1 else pix_deg
    row = int(np.argmin(np.abs(lats - point.latitude_deg)))
    col = int(np.argmin(np.abs(lons - point.longitude_deg)))
    if abs(lats[row] - point.latitude_deg) > dlat or abs(lons[col] - point.longitude_deg) > dlon:
        return None
    return row, col


def _sample(field: np.ndarray | float | None, row: int, col: int) -> float | None:
    if field is None:
        return None
    arr = np.asarray(field, dtype=float)
    return float(arr) if arr.ndim == 0 else float(arr[row, col])


def _to_index(
    scene: SatelliteScene,
    values: np.ndarray,
    clear_sky_reflectance: np.ndarray | float | None,
    max_reflectance: np.ndarray | float | None,
    solar_zenith_deg: np.ndarray | float | None = None,
) -> tuple[np.ndarray | None, str]:
    """Cloud index from the pixel values, or (None, reason) when it cannot be formed.

    When the scene declares ``sun_normalised=False`` the reflectance factor is
    divided by cos(solar zenith) here, before the cloud index is formed. That
    conversion needs a zenith, so a scene that needs it and has none is refused
    rather than retrieved from under the wrong convention.
    """
    if scene.quantity == "cloud_index":
        return values, ""
    if scene.quantity == "reflectance" and scene.sun_normalised is False:
        if solar_zenith_deg is None:
            return None, (
                "the scene holds a reflectance factor that is NOT sun-normalised, and no "
                "solar zenith was supplied to normalise it. The cloud index assumes a "
                "sun-normalised reflectance, so proceeding would bias the retrieval"
            )
        values = normalise_reflectance_by_sun(values, solar_zenith_deg)
    if clear_sky_reflectance is None or max_reflectance is None:
        missing = [
            name
            for name, ref in (
                ("clear_sky_reflectance", clear_sky_reflectance),
                ("max_reflectance", max_reflectance),
            )
            if ref is None
        ]
        return None, (
            f"{' and '.join(missing)} not supplied; the scene holds reflectance and the cloud "
            "index cannot be formed without both references. No default constant is used "
            "(see estimate_clear_sky_reflectance / estimate_max_reflectance)"
        )
    return (
        cloud_index(
            values, clear_sky_reflectance=clear_sky_reflectance, max_reflectance=max_reflectance
        ),
        "",
    )


def _provenance(
    scene: SatelliteScene,
    point: GeoPoint,
    now: datetime,
    notes: str,
    processing: str,
) -> Provenance:
    src = scene.provenance
    age_s = (now - ensure_utc(scene.slot_time_utc)).total_seconds()
    return Provenance(
        provider="rwi",
        dataset=f"{scene.satellite}/{scene.band}",
        kind=DataKind.DERIVED,
        retrieved_at_utc=now,
        valid_time_utc=ensure_utc(scene.slot_time_utc),
        model_name=METHOD_NAME,
        model_version=_METHOD_VERSION,
        point=point,
        spatial_resolution_km=scene.grid.resolution_km,
        temporal_resolution_s=_SLOT_CADENCE_S,
        processing=processing,
        license=src.license,
        source_url=src.source_url,
        notes=(
            f"SATELLITE RETRIEVAL, NOT A MEASUREMENT: nothing on the ground measured this; "
            f"error unknown (no ground-truth irradiance exists in this platform). "
            f"satellite={scene.satellite}; band={scene.band}; "
            f"slot_time_utc={ensure_utc(scene.slot_time_utc).isoformat()}; "
            f"image_age_at_issue_s={age_s:.0f}; source={src.label()}; {notes}"
        ),
    )


# ---------------------------------------------------------------------------
# B. Point retrieval
# ---------------------------------------------------------------------------


def retrieve_ghi_detailed(
    scene: SatelliteScene,
    values: np.ndarray,
    point: GeoPoint,
    *,
    solar_position: SolarPosition,
    clear_sky_ghi: IrradianceComponents | float,
    clear_sky_reflectance: np.ndarray | float | None = None,
    max_reflectance: np.ndarray | float | None = None,
    max_clear_sky_index: float = KSTAR_CLIP_MAX,
    now: datetime | None = None,
) -> PointRetrieval:
    """Satellite GHI at ``point``, with the status and reason alongside the number.

    ``values`` is the pixel array matching ``scene.grid`` (``scene.quantity``
    says whether it is reflectance or already a cloud index). For reflectance,
    both reference fields are required; pass ``clear_sky_reflectance`` and
    ``max_reflectance`` (scalars or grid-shaped arrays), typically from
    :func:`rwi.satellite.cloudindex.estimate_clear_sky_reflectance` /
    ``estimate_max_reflectance``.

    ``solar_position`` and ``clear_sky_ghi`` must be for ``point`` at the scan
    slot time (a :class:`ValueError` is raised if they are more than
    :data:`MAX_ALIGNMENT_S` away). ``clear_sky_ghi`` is the Ineichen-Perez
    result from :func:`rwi.solar.clearsky.clear_sky_irradiance` or a bare W/m2.

    Returns a :class:`PointRetrieval`. Its ``measured`` is always
    ``DataKind.DERIVED``; when the retrieval is unavailable its value is
    ``None`` (never 0.0) and ``reason`` says why.
    """
    values = np.asarray(values, dtype=float)
    _check_shape("values", values, scene)
    when = ensure_utc(now) if now is not None else utc_now()
    slot = ensure_utc(scene.slot_time_utc)
    _check_alignment("solar_position", solar_position.time_utc, slot)
    cs = _clear_sky_value(clear_sky_ghi, slot)

    def refuse(status: RetrievalStatus, reason: str) -> PointRetrieval:
        prov = _provenance(scene, point, when, f"UNAVAILABLE ({status.value}): {reason}",
                           f"{METHOD_NAME};unavailable")
        m = Measured(variable="ghi", value=None, unit="W/m2", provenance=prov,
                     quality=QualityFlag.MISSING)
        return PointRetrieval(measured=m, status=status, reason=reason)

    gate = _scene_gate(scene)
    if gate is not None:
        return refuse(*gate)
    zen = solar_position.apparent_zenith_deg
    if not solar_position.is_daylight or zen >= USABLE_SOLAR_ZENITH_DEG:
        return refuse(
            RetrievalStatus.NIGHT_OR_LOW_SUN,
            f"visible-band retrieval unavailable: solar zenith at the point is "
            f"{zen:.1f} deg (limit {USABLE_SOLAR_ZENITH_DEG:.0f}). "
            "This is 'no retrieval', not 'zero irradiance'",
        )

    pixel = _nearest_pixel(scene, point)
    if pixel is None:
        return refuse(RetrievalStatus.OUTSIDE_SCENE,
                      "point lies more than one pixel outside the scene grid")
    row, col = pixel

    n_field, why = _to_index(
        scene, values, clear_sky_reflectance, max_reflectance, solar_zenith_deg=zen
    )
    if n_field is None:
        return refuse(RetrievalStatus.MISSING_REFERENCE, why)
    n = float(n_field[row, col])
    if not np.isfinite(n):
        return refuse(
            RetrievalStatus.NO_DATA,
            f"pixel ({row}, {col}) has no usable value (NaN pixel, or the clear-ground/"
            "cloud reference contrast is too small or missing there)",
        )

    k = clear_sky_index(np.float64(n))
    clip = clip_clear_sky_index(k, lower=KSTAR_CLIP_MIN, upper=max_clear_sky_index)
    kstar = float(clip.values)
    ghi = float(np.clip(kstar * cs, 0.0, 1600.0))
    notes = (
        f"pixel=({row},{col}) at lat={scene.grid.latitudes[row]:.4f}, "
        f"lon={scene.grid.longitudes[col]:.4f}; n={n:.4f}; k*={kstar:.4f} "
        f"({clip.describe()}); clear_sky_ghi={cs:.1f} W/m2; "
        f"solar_zenith={solar_position.apparent_zenith_deg:.1f} deg; "
        "relation=Hammer2003/Rigollier2004 piecewise; NOT VALIDATED"
    )
    prov = _provenance(scene, point, when, notes, f"{METHOD_NAME};nearest_pixel;clip_kstar")
    m = Measured(
        variable="ghi", value=ghi, unit="W/m2", provenance=prov, quality=QualityFlag.UNVALIDATED
    )
    return PointRetrieval(
        measured=m,
        status=RetrievalStatus.OK,
        reason="",
        cloud_index=n,
        clear_sky_index=kstar,
        clipped=clip.was_clipped,
    )


def retrieve_ghi(
    scene: SatelliteScene,
    values: np.ndarray,
    point: GeoPoint,
    *,
    solar_position: SolarPosition,
    clear_sky_ghi: IrradianceComponents | float,
    clear_sky_reflectance: np.ndarray | float | None = None,
    max_reflectance: np.ndarray | float | None = None,
    max_clear_sky_index: float = KSTAR_CLIP_MAX,
    now: datetime | None = None,
) -> Measured:
    """Satellite GHI [W/m2] at ``point`` as a :class:`Measured` (``DataKind.DERIVED``).

    See :func:`retrieve_ghi_detailed` for the arguments. When unavailable
    (night, low Sun, missing references, off-scene, NaN pixel) ``value`` is
    ``None`` with ``quality=MISSING``, and ``provenance.notes`` carries the
    reason; use ``retrieve_ghi_detailed`` for the reason as a structured field.
    """
    return retrieve_ghi_detailed(
        scene,
        values,
        point,
        solar_position=solar_position,
        clear_sky_ghi=clear_sky_ghi,
        clear_sky_reflectance=clear_sky_reflectance,
        max_reflectance=max_reflectance,
        max_clear_sky_index=max_clear_sky_index,
        now=now,
    ).measured


# ---------------------------------------------------------------------------
# B. Whole-field retrieval
# ---------------------------------------------------------------------------


def retrieve_field_detailed(
    scene: SatelliteScene,
    values: np.ndarray,
    *,
    clear_sky_ghi_field: np.ndarray,
    clear_sky_reflectance: np.ndarray | float | None = None,
    max_reflectance: np.ndarray | float | None = None,
    solar_zenith_deg_field: np.ndarray | None = None,
    max_clear_sky_index: float = KSTAR_CLIP_MAX,
) -> FieldRetrieval:
    """Retrieve GHI [W/m2] for every pixel; refusals return an all-NaN field with a reason.

    ``clear_sky_ghi_field`` is the grid-shaped clear-sky GHI at the scan time.
    If ``solar_zenith_deg_field`` is given, pixels at or beyond
    :data:`USABLE_SOLAR_ZENITH_DEG` become NaN individually. If it is not
    given and the scene's zenith range reaches that limit anywhere, the whole
    field is refused, because the scene metadata cannot say which pixels are
    sunlit enough.
    """
    values = np.asarray(values, dtype=float)
    cs = np.asarray(clear_sky_ghi_field, dtype=float)
    _check_shape("values", values, scene)
    _check_shape("clear_sky_ghi_field", cs, scene)
    nan_field = np.full(values.shape, np.nan)

    def refuse(status: RetrievalStatus, reason: str) -> FieldRetrieval:
        return FieldRetrieval(ghi=nan_field, clear_sky_index=nan_field.copy(),
                              cloud_index=nan_field.copy(), status=status, reason=reason)

    gate = _scene_gate(scene)
    if gate is not None:
        return refuse(*gate)

    sunlit = np.ones(values.shape, dtype=bool)
    if solar_zenith_deg_field is not None:
        zen = np.asarray(solar_zenith_deg_field, dtype=float)
        _check_shape("solar_zenith_deg_field", zen, scene)
        with np.errstate(invalid="ignore"):
            sunlit = zen < USABLE_SOLAR_ZENITH_DEG
        if not sunlit.any():
            return refuse(RetrievalStatus.NIGHT_OR_LOW_SUN,
                          "no pixel has solar zenith below the usable limit; 'no retrieval', "
                          "not 'zero irradiance'")
    else:
        zr = scene.solar_zenith_range_deg
        if zr is not None and zr[1] >= USABLE_SOLAR_ZENITH_DEG:
            return refuse(
                RetrievalStatus.NIGHT_OR_LOW_SUN,
                f"scene solar zenith spans {zr[0]:.1f}-{zr[1]:.1f} deg, reaching the "
                f"{USABLE_SOLAR_ZENITH_DEG:.0f} deg limit, and no per-pixel zenith field was "
                "supplied to say which pixels are usable",
            )

    n, why = _to_index(
        scene,
        values,
        clear_sky_reflectance,
        max_reflectance,
        solar_zenith_deg=solar_zenith_deg_field,
    )
    if n is None:
        return refuse(RetrievalStatus.MISSING_REFERENCE, why)

    k = clear_sky_index(n)
    clip = clip_clear_sky_index(k, lower=KSTAR_CLIP_MIN, upper=max_clear_sky_index)
    with np.errstate(invalid="ignore"):
        ghi = np.clip(clip.values * cs, 0.0, 1600.0)
    ghi = np.where(sunlit, ghi, np.nan)
    kout = np.where(sunlit, clip.values, np.nan)
    nout = np.where(sunlit, n, np.nan)
    return FieldRetrieval(
        ghi=np.asarray(ghi, dtype=float),
        clear_sky_index=np.asarray(kout, dtype=float),
        cloud_index=np.asarray(nout, dtype=float),
        status=RetrievalStatus.OK,
        reason="",
        clip=clip,
    )


def retrieve_field(
    scene: SatelliteScene,
    values: np.ndarray,
    *,
    clear_sky_ghi_field: np.ndarray,
    clear_sky_reflectance: np.ndarray | float | None = None,
    max_reflectance: np.ndarray | float | None = None,
    solar_zenith_deg_field: np.ndarray | None = None,
    max_clear_sky_index: float = KSTAR_CLIP_MAX,
) -> np.ndarray:
    """Retrieved GHI [W/m2] for the whole grid; NaN wherever unavailable.

    NaN means "no retrieval" (night, low Sun, missing reference, NaN pixel),
    never zero irradiance. The array alone cannot say why; call
    :func:`retrieve_field_detailed` for the reason and the clip record.
    """
    return retrieve_field_detailed(
        scene,
        values,
        clear_sky_ghi_field=clear_sky_ghi_field,
        clear_sky_reflectance=clear_sky_reflectance,
        max_reflectance=max_reflectance,
        solar_zenith_deg_field=solar_zenith_deg_field,
        max_clear_sky_index=max_clear_sky_index,
    ).ghi


# ---------------------------------------------------------------------------
# B. Agreement with measurements (for the day measurements exist)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgreementMetrics:
    """Bias, MAE and RMSE of retrieved GHI against supplied measured GHI [W/m2].

    ``bias`` is mean(retrieved - measured): positive means the retrieval is too
    bright. Computed only on the pairs supplied.
    """

    n_pairs: int
    n_discarded: int
    bias_w_m2: float
    mae_w_m2: float
    rmse_w_m2: float
    mean_measured_w_m2: float
    note: str = (
        "computed on the supplied pairs only. No measured GHI exists in this platform yet, so "
        "this has never been run on real data and the retrieval error remains unknown"
    )


def compare_with_measurements(
    retrieved: Sequence[float] | np.ndarray, measured: Sequence[float] | np.ndarray
) -> AgreementMetrics:
    """Bias, MAE, RMSE of ``retrieved`` against ``measured`` GHI [W/m2], pair by pair.

    The two sequences must be the same length and paired (same place and time).
    Pairs where either side is NaN/None are discarded and counted; if none
    remain a :class:`ValueError` is raised instead of returning NaN metrics.
    Night-time retrievals are NaN by design and so fall out automatically.
    """
    r = np.asarray([np.nan if v is None else v for v in retrieved], dtype=float)
    m = np.asarray([np.nan if v is None else v for v in measured], dtype=float)
    if r.shape != m.shape or r.ndim != 1:
        raise ValueError(
            f"retrieved and measured must be 1-D and the same length, got {r.shape} and {m.shape}"
        )
    ok = np.isfinite(r) & np.isfinite(m)
    if not ok.any():
        raise ValueError("no valid (finite) retrieved/measured pairs to compare")
    err = r[ok] - m[ok]
    return AgreementMetrics(
        n_pairs=int(ok.sum()),
        n_discarded=int((~ok).sum()),
        bias_w_m2=float(err.mean()),
        mae_w_m2=float(np.abs(err).mean()),
        rmse_w_m2=float(np.sqrt((err**2).mean())),
        mean_measured_w_m2=float(m[ok].mean()),
    )
