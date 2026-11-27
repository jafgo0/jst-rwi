"""P10/P50/P90 `QuantileBand` objects for the stable ``/api/v1`` surface (ADR 0011).

* `band_from_measured` turns one post-processed `Measured` (P50 value, P10-P90
  uncertainty) into a band; lineage is checked by `QuantileBand` itself.
* `project_uv` / `box_projection_bands` give head- and crosswind relative to the
  route heading from the u/v bands. The box is conservative: the P10-P90 corners
  of u and v are projected and the extremes kept, so ``interval_probability`` is
  ``None`` (>= 0.6 holds only if both marginals are calibrated at 0.8).
* `route_point_bands` picks the bands for one trajectory point and, for every
  variable without one, the reason why. A missing band is never filled with the
  raw upstream value.

Sign convention is `rwi.core.wind.relative_wind` at vehicle speed 0: headwind
positive opposes travel, crosswind positive blows from the vehicle's left.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from rwi.contracts import QuantileBand, WeatherSeries, WeatherStep
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Measured, QualityFlag
from rwi.core.timebase import ensure_utc
from rwi.ml.artifacts import ModelManifest
from rwi.ml.features import source_id
from rwi.ml.postprocess import ML_DATASET, ML_PROVIDER
from rwi.ml.quantile import QuantileModel
from rwi.ml.targets import UV_VARIABLES, latest_cutoff, upstream_value

__all__ = [
    "BOX_METHOD",
    "CROSSWIND",
    "HEADWIND",
    "ROUTE_RELATIVE_VARIABLES",
    "STEP_TOLERANCE_S",
    "BandReason",
    "PointBands",
    "band_from_measured",
    "box_projection_bands",
    "project_uv",
    "route_point_bands",
]

HEADWIND = "headwind_10m"
CROSSWIND = "crosswind_10m"
ROUTE_RELATIVE_VARIABLES: tuple[str, str] = (HEADWIND, CROSSWIND)
BOX_METHOD = "uv_box_projection"
#: Upstream step accepted for an ETA: within 90 min (the trajectory_weather window).
STEP_TOLERANCE_S = 5400.0

BandReason = Literal["no_model", "no_upstream", "not_applicable", "outside_training_range"]

Models = Mapping[str, tuple[QuantileModel, ModelManifest]]


def band_from_measured(m: Measured, *, point: GeoPoint | None = None) -> QuantileBand | None:
    """Band from a value with a P10-P90 uncertainty, or None when it has no interval."""
    u = m.uncertainty
    valid = m.provenance.valid_time_utc
    if m.value is None or u is None or u.lower is None or u.upper is None or valid is None:
        return None
    where = m.provenance.point or point
    if where is None:
        raise ValueError(f"{m.variable}: a band needs a point and the value carries none")
    prov = m.provenance
    return QuantileBand(
        variable=m.variable,
        unit=m.unit,
        valid_time_utc=valid,
        point=where,
        p10=u.lower,
        p50=m.value,
        p90=u.upper,
        method=u.method or "unspecified",
        interval_probability=u.interval_probability,
        quality=m.quality,
        ml_derived=prov.provider == ML_PROVIDER and prov.dataset == ML_DATASET,
        provenance=prov,
    )


def _coefficients(heading_deg: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """((a, b) of headwind, (a, b) of crosswind) as linear forms a*u + b*v."""
    h = math.radians(heading_deg)
    s, c = math.sin(h), math.cos(h)
    return (-s, -c), (c, -s)


def project_uv(u: float, v: float, heading_deg: float) -> tuple[float, float]:
    """(headwind, crosswind) in m/s of wind u/v for travel towards ``heading_deg``."""
    (ha, hb), (ca, cb) = _coefficients(heading_deg)
    return ha * u + hb * v, ca * u + cb * v


def _box(a: float, b: float, u: QuantileBand, v: QuantileBand) -> tuple[float, float, float]:
    au = (a * u.p10, a * u.p90)
    bv = (b * v.p10, b * v.p90)
    return min(au) + min(bv), a * u.p50 + b * v.p50, max(au) + max(bv)


def box_projection_bands(
    u: QuantileBand, v: QuantileBand, heading_deg: float
) -> tuple[QuantileBand, QuantileBand]:
    """(headwind, crosswind) bands from ML u/v bands by conservative box projection."""
    if (u.variable, v.variable) != UV_VARIABLES:
        raise ValueError(f"expected {UV_VARIABLES} bands, got ({u.variable}, {v.variable})")
    if u.valid_time_utc != v.valid_time_utc:
        raise ValueError("u and v bands must share a valid time")
    if not (u.ml_derived and v.ml_derived):
        raise ValueError("box projection is defined for ML u/v bands only")
    pu, pv = u.provenance, v.provenance
    assert pu.training_cutoff_utc is not None and pv.training_cutoff_utc is not None
    provenance = pu.model_copy(
        update={
            "model_name": f"{pu.model_name}+{pv.model_name}",
            "model_version": f"u:{pu.model_version}+v:{pv.model_version}",
            "training_cutoff_utc": latest_cutoff(pu.training_cutoff_utc, pv.training_cutoff_utc),
            "processing": (
                f"{BOX_METHOD} of wind_u_10m/wind_v_10m P10-P90 onto heading "
                f"{heading_deg:.1f} deg; {pu.processing}"
            ),
            "notes": "; ".join(n for n in (pu.notes, pv.notes) if n) or None,
        }
    )
    quality = (
        QualityFlag.OK
        if u.quality is v.quality is QualityFlag.OK
        else (u.quality if u.quality is not QualityFlag.OK else v.quality)
    )
    out: list[QuantileBand] = []
    for name, (a, b) in zip(ROUTE_RELATIVE_VARIABLES, _coefficients(heading_deg), strict=True):
        lo, mid, hi = _box(a, b, u, v)
        out.append(
            QuantileBand(
                variable=name,
                unit="m/s",
                valid_time_utc=u.valid_time_utc,
                point=u.point,
                p10=lo,
                p50=mid,
                p90=hi,
                method=BOX_METHOD,
                interval_probability=None,
                quality=quality,
                ml_derived=True,
                provenance=provenance,
            )
        )
    return out[0], out[1]


@dataclass(frozen=True)
class PointBands:
    """Bands for one trajectory point and, for every requested variable without one, why."""

    bands: dict[str, QuantileBand] = field(default_factory=dict)
    reasons: dict[str, BandReason] = field(default_factory=dict)


def _step_at(series: WeatherSeries | None, when: datetime) -> WeatherStep | None:
    return None if series is None else series.nearest_step(when, tolerance_s=0.0)


def _base_band(
    variable: str,
    upstream: WeatherSeries | None,
    ml: WeatherSeries | None,
    models: Models,
    eta_utc: datetime,
) -> QuantileBand | BandReason:
    if variable not in models:
        return "no_model"
    if upstream is None:
        return "no_upstream"
    model, _ = models[variable]
    init = upstream.model_init_time_utc
    if (
        upstream.kind is not DataKind.FORECAST
        or init is None
        or source_id(upstream.provider, upstream.model_name) not in model.source_vocabulary
    ):
        return "not_applicable"
    step = upstream.nearest_step(eta_utc, tolerance_s=STEP_TOLERANCE_S)
    if step is None or upstream_value(step, variable) is None:
        return "no_upstream"
    if ensure_utc(step.valid_time_utc) < ensure_utc(init):
        return "not_applicable"
    ml_step = _step_at(ml, ensure_utc(step.valid_time_utc))
    measured = None if ml_step is None else ml_step.values.get(variable)
    band = None if measured is None else band_from_measured(measured, point=upstream.point)
    return "outside_training_range" if band is None else band


def route_point_bands(
    upstream: WeatherSeries | None,
    ml: WeatherSeries | None,
    *,
    models: Models,
    eta_utc: datetime,
    heading_deg: float,
    variables: Sequence[str],
) -> PointBands:
    """Bands of ``variables`` at ``eta_utc`` from the ML series, with reasons for the rest."""
    eta = ensure_utc(eta_utc)
    cache: dict[str, QuantileBand | BandReason] = {}

    def base(variable: str) -> QuantileBand | BandReason:
        if variable not in cache:
            cache[variable] = _base_band(variable, upstream, ml, models, eta)
        return cache[variable]

    out = PointBands()
    for variable in variables:
        if variable in ROUTE_RELATIVE_VARIABLES:
            u, v = base(UV_VARIABLES[0]), base(UV_VARIABLES[1])
            if isinstance(u, str):
                out.reasons[variable] = u
                continue
            if isinstance(v, str):
                out.reasons[variable] = v
                continue
            head, cross = box_projection_bands(u, v, heading_deg)
            out.bands[variable] = head if variable == HEADWIND else cross
            continue
        result = base(variable)
        if isinstance(result, str):
            out.reasons[variable] = result
        else:
            out.bands[variable] = result
    return out
