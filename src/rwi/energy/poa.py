"""Plane-of-array (POA) irradiance: transposition of GHI/DNI/DHI onto the panel.

Units: every quantity here is irradiance in W/m2 (a power per unit area), plus
angles in degrees. Nothing in this module produces power (W) or energy (Wh).

Frame of reference: azimuths are clockwise from true north (0 = N, 90 = E,
180 = S, 270 = W), the same convention as :class:`rwi.contracts.SolarPosition`
and ``pvlib``. ``surface_tilt_deg`` is measured from the horizontal (0 = flat
facing the zenith, 90 = vertical).

Models (selected by name, all via :func:`pvlib.irradiance.get_total_irradiance`):

* ``isotropic``  - Liu & Jordan (1960); sky diffuse is uniform over the dome.
* ``klucher``    - Klucher (1979); adds horizon brightening and circumsolar term.
* ``haydavies``  - Hay & Davies (1980); anisotropy index AI = DNI / E0n splits
  diffuse into a circumsolar part (treated like beam) and an isotropic part.
  Default: needs only the three components and the extraterrestrial DNI.
* ``reindl``     - Reindl et al. (1990); Hay-Davies plus a horizon term.
* ``king``       - King's diffuse model (Sandia).
* ``perez``      - Perez et al. (1990), ``allsitescomposite1990`` coefficients;
  usually the most accurate on tilted planes but needs the relative air mass.

Ground-reflected diffuse is isotropic: ``GHI * albedo * (1 - cos(tilt)) / 2``.
For a horizontal plane this term is exactly zero.

Known limitations (see docs/science/pv.md): no horizon/terrain/vehicle shading,
no reflection (angle-of-incidence) loss on the cover glass, uniform albedo.
"""

from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import pandas as pd
from pvlib import atmosphere
from pvlib import irradiance as pvirr

from rwi.contracts import ArrayConfig, IrradianceComponents, POAIrradiance, SolarPosition
from rwi.core.errors import ModelConfigurationError
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import ensure_utc, utc_now
from rwi.core.units import SOLAR_CONSTANT_W_M2

__all__ = [
    "HORIZONTAL_AZIMUTH_CONVENTION_DEG",
    "TRANSPOSITION_MODELS",
    "poa_irradiance",
    "surface_azimuth_for_heading",
]

TRANSPOSITION_MODELS: frozenset[str] = frozenset(
    {"isotropic", "klucher", "haydavies", "reindl", "king", "perez"}
)

HORIZONTAL_AZIMUTH_CONVENTION_DEG = 180.0
"""Azimuth reported for a horizontal (tilt 0) surface.

Convention only. With tilt = 0 the surface normal is the zenith for every
azimuth, so cos(AOI) = cos(zenith) and the ground-view factor (1 - cos 0)/2 = 0
no matter what azimuth is used; the POA result is independent of this number.
180 deg is chosen because it is the conventional "equator-facing" default in
the northern hemisphere and keeps the pydantic range [0, 360] satisfied."""


def surface_azimuth_for_heading(array: ArrayConfig, heading_deg: float | None) -> float:
    """Panel azimuth (deg clockwise from true north) for an array on a moving vehicle.

    * ``mounting == "horizontal"``: tilt is 0, so azimuth is physically
      irrelevant (see :data:`HORIZONTAL_AZIMUTH_CONVENTION_DEG`); returns 180.0
      without needing a heading.
    * ``mounting == "fixed_tilt"``: returns ``array.surface_azimuth_deg``
      (a fixed world-frame azimuth, e.g. a stationary charging stop). Raises
      :class:`ModelConfigurationError` if that field is None.
    * ``mounting == "vehicle_relative"``: the panel is fixed in the vehicle
      frame, so its world azimuth depends on the vehicle heading.

    Vehicle-relative convention implemented
    ---------------------------------------
    ``array.surface_azimuth_deg`` is reinterpreted for this mounting as the
    **bearing of the panel's downslope direction relative to the vehicle's
    forward direction, clockwise seen from above**:

    * ``None`` or ``0``: panel tilted about the vehicle's *transverse* axis
      (pitched nose-down); its normal's horizontal projection points along
      the heading, so ``azimuth = heading``.
    * ``90``: tilted about the *longitudinal* axis towards the right-hand
      side (right edge low), so ``azimuth = heading + 90``.
    * ``270``: left edge low, ``azimuth = heading - 90``.
    * ``180``: nose-up tilt, facing the rear, ``azimuth = heading + 180``.

    The result is wrapped to [0, 360). This only matters if ``surface_tilt_deg``
    is > 0; a flat vehicle-relative array is insensitive to heading.

    Raises
    ------
    ModelConfigurationError
        ``vehicle_relative`` with ``heading_deg is None`` (a direction must
        never be guessed), a non-finite heading, or ``fixed_tilt`` without an
        azimuth.
    """
    if array.mounting == "horizontal":
        return HORIZONTAL_AZIMUTH_CONVENTION_DEG
    if array.mounting == "fixed_tilt":
        if array.surface_azimuth_deg is None:
            raise ModelConfigurationError(
                f"array {array.name!r}: fixed_tilt mounting requires surface_azimuth_deg"
            )
        return float(array.surface_azimuth_deg) % 360.0
    # vehicle_relative
    if heading_deg is None:
        raise ModelConfigurationError(
            f"array {array.name!r}: vehicle_relative mounting needs the vehicle heading; "
            "refusing to guess a panel direction"
        )
    if not math.isfinite(heading_deg):
        raise ModelConfigurationError(f"heading_deg is not finite: {heading_deg!r}")
    relative = 0.0 if array.surface_azimuth_deg is None else float(array.surface_azimuth_deg)
    return (heading_deg + relative) % 360.0


def _poa_provenance(
    irradiance: IrradianceComponents, model: str, notes: list[str]
) -> Provenance:
    src = irradiance.provenance
    return Provenance(
        provider="rwi",
        dataset="energy.poa",
        kind=DataKind.PHYSICAL_MODEL,
        retrieved_at_utc=utc_now(),
        valid_time_utc=irradiance.time_utc,
        model_name=f"pvlib.irradiance.get_total_irradiance[{model}]",
        point=irradiance.point,
        processing="transposition GHI/DNI/DHI -> POA",
        notes=(
            f"input irradiance: {src.provider}/{src.dataset} kind={irradiance.kind.value}"
            + ("; " + "; ".join(notes) if notes else "")
        ),
    )


def _zero_poa(
    when: datetime,
    solar: SolarPosition,
    irradiance: IrradianceComponents,
    tilt: float,
    azimuth: float,
    albedo: float,
    model: str,
) -> POAIrradiance:
    aoi = float(
        np.clip(
            pvirr.aoi(tilt, azimuth, solar.apparent_zenith_deg, solar.azimuth_deg), 0.0, 180.0
        )
    )
    return POAIrradiance(
        time_utc=when,
        poa_global_w_m2=0.0,
        poa_direct_w_m2=0.0,
        poa_sky_diffuse_w_m2=0.0,
        poa_ground_diffuse_w_m2=0.0,
        aoi_deg=aoi,
        surface_tilt_deg=tilt,
        surface_azimuth_deg=azimuth,
        albedo=albedo,
        transposition_model=model,
        provenance=_poa_provenance(irradiance, model, ["sun below horizon: all components 0"]),
    )


def poa_irradiance(
    solar: SolarPosition,
    irradiance: IrradianceComponents,
    *,
    surface_tilt_deg: float,
    surface_azimuth_deg: float,
    albedo: float = 0.2,
    model: str = "haydavies",
) -> POAIrradiance:
    """Transpose horizontal irradiance onto a tilted plane (all values W/m2).

    ``poa_global = poa_direct + poa_sky_diffuse + poa_ground_diffuse``

    * ``poa_direct = DNI * max(cos(AOI), 0)``; zero when AOI > 90 deg (sun behind
      the panel plane).
    * ``poa_sky_diffuse`` from the chosen transposition ``model``.
    * ``poa_ground_diffuse = GHI * albedo * (1 - cos(tilt)) / 2``.

    The solar zenith used is ``apparent_zenith_deg`` (refraction-corrected), the
    convention of ``pvlib.modelchain``. When ``apparent_elevation_deg <= 0``
    every component is exactly 0.0 (the AOI is still reported).

    Note that for a horizontal plane ``poa_global`` equals ``DNI*cos(z) + DHI``,
    which equals GHI only if the supplied components are closed
    (``GHI == DNI*cos(z) + DHI``). Decomposition-model output or independent
    forecast variables are generally not closed, so POA on a flat plane differs
    from the supplied GHI by that closure residual. The residual is reported in
    the provenance notes when it exceeds 5 % of GHI.

    Parameters
    ----------
    solar, irradiance
        Must describe the same instant (checked) and place.
    surface_tilt_deg
        0..90, from horizontal.
    surface_azimuth_deg
        Clockwise from true north. Irrelevant when tilt is 0.
    albedo
        Ground reflectance 0..1 (default 0.2: generic dry ground/asphalt mix;
        an assumption, not a measurement).
    model
        One of :data:`TRANSPOSITION_MODELS`.

    Raises
    ------
    ModelConfigurationError
        Unknown ``model``, angles/albedo out of range, or mismatched times.
    """
    if model not in TRANSPOSITION_MODELS:
        raise ModelConfigurationError(
            f"unknown transposition model {model!r}; choose from {sorted(TRANSPOSITION_MODELS)}"
        )
    if not (0.0 <= surface_tilt_deg <= 90.0) or not math.isfinite(surface_tilt_deg):
        raise ModelConfigurationError(f"surface_tilt_deg out of [0, 90]: {surface_tilt_deg!r}")
    if not math.isfinite(surface_azimuth_deg):
        raise ModelConfigurationError(f"surface_azimuth_deg not finite: {surface_azimuth_deg!r}")
    if not (0.0 <= albedo <= 1.0):
        raise ModelConfigurationError(f"albedo out of [0, 1]: {albedo!r}")
    when = ensure_utc(irradiance.time_utc)
    if abs((ensure_utc(solar.time_utc) - when).total_seconds()) > 1.0:
        raise ModelConfigurationError(
            f"solar position ({solar.time_utc.isoformat()}) and irradiance "
            f"({when.isoformat()}) are for different instants"
        )
    azimuth = float(surface_azimuth_deg) % 360.0
    tilt = float(surface_tilt_deg)

    if solar.apparent_elevation_deg <= 0.0:
        return _zero_poa(when, solar, irradiance, tilt, azimuth, albedo, model)

    notes: list[str] = []
    zenith = solar.apparent_zenith_deg
    cos_z = math.cos(math.radians(zenith))
    closure = irradiance.dni_w_m2 * cos_z + irradiance.dhi_w_m2
    ghi = irradiance.ghi_w_m2
    if ghi > 50.0 and abs(closure - ghi) > 0.05 * ghi:
        notes.append(
            f"components not closed: DNI*cos(z)+DHI={closure:.1f} vs GHI={ghi:.1f} W/m2"
        )

    index = pd.DatetimeIndex([when])
    dni_extra = pvirr.get_extra_radiation(index, solar_constant=SOLAR_CONSTANT_W_M2).to_numpy()
    airmass = solar.air_mass
    if airmass is None:
        airmass = float(atmosphere.get_relative_airmass(zenith))
        if not math.isfinite(airmass):
            airmass = None
        else:
            notes.append("air mass computed from apparent zenith (Kasten-Young 1989)")

    tot = pvirr.get_total_irradiance(
        tilt,
        azimuth,
        zenith,
        solar.azimuth_deg,
        irradiance.dni_w_m2,
        irradiance.ghi_w_m2,
        irradiance.dhi_w_m2,
        dni_extra=float(dni_extra[0]),
        airmass=airmass,
        albedo=albedo,
        model=model,
    )
    direct = float(np.nan_to_num(tot["poa_direct"], nan=0.0))
    ground = float(np.nan_to_num(tot["poa_ground_diffuse"], nan=0.0))
    sky = float(tot["poa_sky_diffuse"])
    if not math.isfinite(sky):
        # Perez/Reindl can return NaN at very low sun; fall back to the
        # isotropic sky so energy is not silently lost, and say so.
        sky = float(
            pvirr.isotropic(tilt, irradiance.dhi_w_m2)
        )
        notes.append(f"{model} sky diffuse undefined here; isotropic fallback used")
    direct = max(direct, 0.0)
    sky = max(sky, 0.0)
    ground = max(ground, 0.0)
    aoi = float(np.clip(pvirr.aoi(tilt, azimuth, zenith, solar.azimuth_deg), 0.0, 180.0))
    return POAIrradiance(
        time_utc=when,
        poa_global_w_m2=direct + sky + ground,
        poa_direct_w_m2=direct,
        poa_sky_diffuse_w_m2=sky,
        poa_ground_diffuse_w_m2=ground,
        aoi_deg=aoi,
        surface_tilt_deg=tilt,
        surface_azimuth_deg=azimuth,
        albedo=albedo,
        transposition_model=model,
        provenance=_poa_provenance(irradiance, model, notes),
    )
