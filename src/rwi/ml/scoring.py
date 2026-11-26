"""Holdout scoring: raw upstream sources vs the ML post-processor (ADR 0008 §4).

Every system is scored on the same holdout rows with the same CRPS scheme
(`crps_quantiles` on P10/P50/P90). A raw point forecast is scored as three equal
quantiles, which makes its CRPS exactly its absolute error, so the comparison
is like-for-like. Scores are broken down by lead-time bucket and, when route
distance is known, by route segment.
"""

import math
from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict

from rwi.ml.dataset import Dataset
from rwi.ml.quantile import QUANTILES, QuantileModel
from rwi.verification.metrics import (
    DEFAULT_LEAD_TIME_BUCKETS_S,
    bucket_for,
    bucket_label,
    crps_quantiles,
)

__all__ = [
    "ALL",
    "ROUTE_SEGMENT_KM",
    "ScoreRow",
    "coverage_80",
    "score_holdout",
    "summary_metrics",
]

FloatArray = NDArray[np.float64]

#: Group label covering every row.
ALL = "all"
#: Route segment width for the per-segment breakdown.
ROUTE_SEGMENT_KM = 500.0


class ScoreRow(BaseModel):
    """Scores of one system over one group of holdout rows."""

    model_config = ConfigDict(frozen=True)

    system: str
    """``ml`` or an upstream source id such as ``open-meteo/best_match``."""
    group_kind: str
    """``all``, ``lead_time`` or ``route_segment``."""
    group: str
    n: int
    mae: float | None
    rmse: float | None
    bias: float | None
    crps: float | None
    coverage_80: float | None = None
    """Fraction of observations inside P10-P90 (ML only; nominal 0.80)."""


def coverage_80(observed: FloatArray, quantiles: FloatArray) -> float | None:
    """Fraction of ``observed`` inside ``[P10, P90]``; None when empty."""
    if observed.size == 0:
        return None
    inside = (observed >= quantiles[:, 0]) & (observed <= quantiles[:, -1])
    return float(np.mean(inside))


def _scores(observed: FloatArray, q: FloatArray) -> tuple[float, float, float, float]:
    err = q[:, 1] - observed
    crps = math.fsum(
        crps_quantiles(float(o), [float(v) for v in row], QUANTILES)
        for o, row in zip(observed, q, strict=True)
    ) / observed.size
    return (
        float(np.mean(np.abs(err))),
        float(math.sqrt(float(np.mean(err**2)))),
        float(np.mean(err)),
        crps,
    )


def _row(
    system: str, kind: str, group: str, observed: FloatArray, q: FloatArray, with_cov: bool
) -> ScoreRow:
    if observed.size == 0:
        return ScoreRow(
            system=system, group_kind=kind, group=group, n=0,
            mae=None, rmse=None, bias=None, crps=None,
        )
    mae, rmse, bias, crps = _scores(observed, q)
    return ScoreRow(
        system=system,
        group_kind=kind,
        group=group,
        n=int(observed.size),
        mae=mae,
        rmse=rmse,
        bias=bias,
        crps=crps,
        coverage_80=coverage_80(observed, q) if with_cov else None,
    )


def _groups(
    ds: Dataset, buckets: Sequence[tuple[float, float]]
) -> list[tuple[str, str, NDArray[np.bool_]]]:
    out: list[tuple[str, str, NDArray[np.bool_]]] = [(ALL, ALL, np.ones(len(ds), dtype=bool))]
    labels = [bucket_for(float(t), buckets) for t in ds.lead_time_s]
    for b in buckets:
        mask = np.asarray([lab == b for lab in labels], dtype=bool)
        if mask.any():
            out.append(("lead_time", bucket_label(b), mask))
    dist = ds.route_distance_km
    if np.isfinite(dist).any():
        seg = np.floor(np.where(np.isfinite(dist), dist, -1.0) / ROUTE_SEGMENT_KM)
        for s in sorted({int(v) for v in seg[np.isfinite(dist)]}):
            lo = s * ROUTE_SEGMENT_KM
            label = f"{lo:g}-{lo + ROUTE_SEGMENT_KM:g}km"
            out.append(("route_segment", label, np.isfinite(dist) & (seg == s)))
    return out


def score_holdout(
    model: QuantileModel,
    holdout: Dataset,
    *,
    buckets: Sequence[tuple[float, float]] = DEFAULT_LEAD_TIME_BUCKETS_S,
) -> list[ScoreRow]:
    """ML and per-source raw scores on ``holdout`` rows the model accepts (in range).

    Rows outside the training envelope are excluded for every system alike, so
    each system is scored on exactly the same rows.
    """
    ok = model.in_range(holdout.features) if len(holdout) else np.zeros(0, dtype=bool)
    ds = holdout.subset(ok)
    if not len(ds):
        return [_row("ml", ALL, ALL, np.zeros(0), np.zeros((0, 3)), True)]
    q_ml = model.predict(ds.features, clear_sky=ds.clear_sky, elevation_deg=ds.elevation_deg)
    q_raw = np.repeat(ds.forecast[:, np.newaxis], len(QUANTILES), axis=1)
    sources = np.asarray(ds.sources)
    rows: list[ScoreRow] = []
    for kind, group, mask in _groups(ds, buckets):
        obs = ds.observed[mask]
        rows.append(_row("ml", kind, group, obs, q_ml[mask], True))
        for src in sorted(set(sources[mask])):
            m = mask & (sources == src)
            rows.append(_row(src, kind, group, ds.observed[m], q_raw[m], False))
    return rows


def summary_metrics(rows: Sequence[ScoreRow]) -> dict[str, float | None]:
    """Manifest metrics: the ML ``all`` row plus the best raw source's CRPS."""
    ml = next((r for r in rows if r.system == "ml" and r.group_kind == ALL), None)
    raw = [r.crps for r in rows if r.system != "ml" and r.group_kind == ALL and r.crps is not None]
    return {
        "n_scored": float(ml.n) if ml else 0.0,
        "mae": ml.mae if ml else None,
        "rmse": ml.rmse if ml else None,
        "bias": ml.bias if ml else None,
        "crps": ml.crps if ml else None,
        "coverage_80": ml.coverage_80 if ml else None,
        "best_raw_crps": min(raw) if raw else None,
    }
