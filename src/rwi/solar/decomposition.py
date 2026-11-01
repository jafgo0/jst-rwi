"""Decomposition of global horizontal irradiance into direct and diffuse parts.

A weather provider typically forecasts only GHI; the PV plane-of-array model
needs DNI and DHI. Empirical decomposition models estimate the split from the
clearness index ``kt = GHI / (I0 cos z)``:

* ``"erbs"``   - Erbs, Klein & Duffie (1982), Solar Energy 28(4), 293-302
  (``pvlib.irradiance.erbs``). Default.
* ``"disc"``   - Maxwell (1987), "A quasi-physical model for converting hourly
  global horizontal to direct normal insolation", SERI/TR-215-3087
  (``pvlib.irradiance.disc``); DHI = GHI - DNI cos z.
* ``"dirint"`` - Perez, Ineichen, Maxwell, Seals & Zelenka (1992), "Dynamic
  global-to-direct irradiance conversion models", ASHRAE Transactions 98(1),
  354-369 (``pvlib.irradiance.dirint``), used point-wise with
  ``use_delta_kt_prime=False`` and no dew point: a single value carries no
  time-series stability information, so this is DIRINT's static part only.

All models are hourly-mean correlations developed on mid-latitude Northern
Hemisphere data; skill degrades for sub-hourly data, high elevation, snow and
very low Sun. Their output is an *estimate*, not a measurement.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pvlib

from rwi.contracts import IrradianceComponents, SolarPosition
from rwi.core.provenance import DataKind, Provenance, QualityFlag
from rwi.solar._frame import to_utc_index

__all__ = ["DECOMPOSITION_MODELS", "decompose_ghi"]

DECOMPOSITION_MODELS = ("erbs", "disc", "dirint")

_GHI_MAX, _DNI_MAX, _DHI_MAX = 1600.0, 1400.0, 900.0

#: Provider GHI above this at night is flagged OUT_OF_RANGE (still returned as 0).
_NIGHT_GHI_TOLERANCE_W_M2 = 5.0


def _derived_provenance(provenance: Provenance, model: str, solar: SolarPosition) -> Provenance:
    """New Provenance built from the input (the input object is never mutated)."""
    data = provenance.model_dump()
    existing = data.get("processing")
    step = f"{model}_decomposition"
    data["processing"] = step if not existing else f"{existing}+{step}"
    note = (
        f"DNI/DHI estimated from GHI with {model} (pvlib {pvlib.__version__}); "
        "not measured components"
    )
    data["notes"] = f"{provenance.notes}; {note}" if provenance.notes else note
    if data.get("point") is None:
        data["point"] = solar.point
    if provenance.kind is DataKind.OBSERVATION:
        # Only GHI was observed; the DNI/DHI split is a model estimate.
        data["kind"] = DataKind.DERIVED
        data["observation_time_utc"] = provenance.observation_time_utc
    return Provenance(**data)


def decompose_ghi(
    ghi_w_m2: float,
    solar: SolarPosition,
    *,
    model: str = "erbs",
    provenance: Provenance,
) -> IrradianceComponents:
    """Split a GHI value [W/m2] into DNI and DHI [W/m2].

    ``solar`` supplies the geometry (geometric zenith is used for the cosine and
    the clearness index, as in the original model derivations). ``provenance``
    is the lineage of the GHI input; the result carries a **new** Provenance with
    the same ``kind`` (a decomposed forecast is still a forecast) and
    ``processing`` extended with ``"<model>_decomposition"``. Exception: an
    ``OBSERVATION`` input becomes ``DERIVED``, because DNI/DHI were not measured.

    Rules: ``apparent_elevation_deg <= 0`` => all components exactly 0.0 (a GHI
    above 5 W/m2 at night is zeroed and flagged ``OUT_OF_RANGE``); GHI = 0 =>
    all zeros; negative or non-finite GHI raises ``ValueError``. Results are
    clipped to ``0 <= DHI <= GHI`` and the contract bounds, so
    ``GHI ~= DNI cos z + DHI`` closes to numerical precision except where the
    models cap DNI (very low Sun, zenith > 87 deg), where DHI absorbs the GHI.
    """
    if model not in DECOMPOSITION_MODELS:
        raise ValueError(
            f"unknown decomposition model {model!r}; use one of {DECOMPOSITION_MODELS}"
        )
    if not math.isfinite(ghi_w_m2) or ghi_w_m2 < 0.0:
        raise ValueError(f"ghi_w_m2 must be finite and >= 0, got {ghi_w_m2!r}")
    if ghi_w_m2 > _GHI_MAX:
        raise ValueError(f"ghi_w_m2 {ghi_w_m2} exceeds the physical bound {_GHI_MAX} W/m2")

    new_prov = _derived_provenance(provenance, model, solar)

    def build(
        ghi: float, dni: float, dhi: float, kt: float | None, quality: QualityFlag
    ) -> IrradianceComponents:
        return IrradianceComponents(
            time_utc=solar.time_utc,
            point=solar.point,
            ghi_w_m2=ghi,
            dni_w_m2=dni,
            dhi_w_m2=dhi,
            kind=new_prov.kind,
            provenance=new_prov,
            solar_elevation_deg=solar.apparent_elevation_deg,
            clearness_index=kt,
            quality=quality,
        )

    if solar.apparent_elevation_deg <= 0.0 or ghi_w_m2 == 0.0:
        flag = (
            QualityFlag.OUT_OF_RANGE
            if solar.apparent_elevation_deg <= 0.0 and ghi_w_m2 > _NIGHT_GHI_TOLERANCE_W_M2
            else QualityFlag.OK
        )
        return build(0.0, 0.0, 0.0, 0.0, flag)

    index = to_utc_index([solar.time_utc])
    ghi_s = pd.Series([float(ghi_w_m2)], index=index)
    zen_s = pd.Series([solar.zenith_deg], index=index)
    if model == "erbs":
        res = pvlib.irradiance.erbs(ghi_s, zen_s, index)
        dni = float(res["dni"].iloc[0])
        dhi = float(res["dhi"].iloc[0])
        kt = float(res["kt"].iloc[0])
    else:
        if model == "disc":
            res = pvlib.irradiance.disc(ghi_s, zen_s, index)
            dni = float(res["dni"].iloc[0])
        else:
            dni = float(
                pvlib.irradiance.dirint(ghi_s, zen_s, index, use_delta_kt_prime=False).iloc[0]
            )
        cos_z = math.cos(math.radians(solar.zenith_deg))
        dhi = float(ghi_w_m2) - dni * cos_z
        kt = float("nan")

    if not np.isfinite(dni):
        dni = 0.0
    if not np.isfinite(dhi):
        dhi = float(ghi_w_m2)
    dni = min(max(dni, 0.0), _DNI_MAX)
    dhi = min(max(dhi, 0.0), float(ghi_w_m2), _DHI_MAX)
    clearness: float | None = min(kt, 1.5) if np.isfinite(kt) else None

    return build(float(ghi_w_m2), dni, dhi, clearness, QualityFlag.OK)
