"""Leak-free training store for the ML post-processor (ADR 0008 §2).

Rows come from `SqliteArchive.pairs`, which only returns forecasts recorded
before their valid time. The split is strictly temporal, never K-fold:

* **train**: rows whose forecast was issued at or before the cutoff AND whose
  observation (valid time) is at or before the cutoff, so nothing the model
  learns from was unknown at the cutoff;
* **holdout**: rows whose forecast was issued after the cutoff;
* rows issued before but verified after the cutoff are dropped (the gap).

"Issued" is ``forecast_recorded_at_utc`` (when this platform had the value),
which is never earlier than the model's init time, so it is the stricter clock.
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

import numpy as np
from numpy.typing import NDArray

from rwi.contracts import ForecastObservationPair
from rwi.core.geo import GeoPoint
from rwi.core.timebase import ensure_utc
from rwi.ml.constraints import IRRADIANCE_VARIABLES, K_MAX, MIN_TRAIN_ELEVATION_DEG, ML_VARIABLES
from rwi.ml.features import build_features, solar_context, source_id

__all__ = ["Dataset", "Split", "dataset_from_pairs", "split_by_cutoff"]

FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class Dataset:
    """Feature rows plus targets for one variable; arrays are aligned by row."""

    variable: str
    features: FloatArray
    #: clear-sky index k for irradiance, physical value otherwise
    target: FloatArray
    observed: FloatArray
    forecast: FloatArray
    #: pvlib clear-sky W/m2 of the variable (NaN for non-irradiance)
    clear_sky: FloatArray
    elevation_deg: FloatArray
    lead_time_s: FloatArray
    route_distance_km: FloatArray
    issued_at_utc: tuple[datetime, ...]
    valid_time_utc: tuple[datetime, ...]
    sources: tuple[str, ...]
    source_vocabulary: tuple[str, ...]

    def __len__(self) -> int:
        return len(self.issued_at_utc)

    def subset(self, mask: NDArray[np.bool_]) -> "Dataset":
        """Rows where ``mask`` is true, same vocabulary."""
        idx = np.nonzero(mask)[0]
        return replace(
            self,
            features=self.features[idx],
            target=self.target[idx],
            observed=self.observed[idx],
            forecast=self.forecast[idx],
            clear_sky=self.clear_sky[idx],
            elevation_deg=self.elevation_deg[idx],
            lead_time_s=self.lead_time_s[idx],
            route_distance_km=self.route_distance_km[idx],
            issued_at_utc=tuple(self.issued_at_utc[i] for i in idx),
            valid_time_utc=tuple(self.valid_time_utc[i] for i in idx),
            sources=tuple(self.sources[i] for i in idx),
        )

    @property
    def data_window(self) -> tuple[datetime, datetime] | None:
        """(earliest issue time, latest valid time), or None when empty."""
        if not len(self):
            return None
        return min(self.issued_at_utc), max(self.valid_time_utc)


@dataclass(frozen=True)
class Split:
    """A temporal train/holdout split and the cutoff it honours."""

    train: Dataset
    holdout: Dataset
    #: latest forecast issue time actually present in ``train`` (None if empty)
    training_cutoff_utc: datetime | None


def _finite(p: ForecastObservationPair) -> bool:
    return math.isfinite(p.forecast_value) and math.isfinite(p.observed_value)


def dataset_from_pairs(
    pairs: Sequence[ForecastObservationPair],
    *,
    variable: str,
    source_vocabulary: Sequence[str] | None = None,
    route_distance_km: Callable[[GeoPoint], float | None] | None = None,
) -> Dataset:
    """Training rows for ``variable`` from archive pairs (forecast point, recorded time).

    Irradiance rows below `MIN_TRAIN_ELEVATION_DEG` are dropped and the target is
    the clear-sky index clipped to ``[0, K_MAX]``. Non-finite pairs and pairs of
    other variables are ignored. ``source_vocabulary`` fixes the source coding
    (inference must reuse the model's); without it the sorted sources seen are used.
    """
    if variable not in ML_VARIABLES:
        raise ValueError(f"{variable!r} is not an ML variable; choose from {ML_VARIABLES}")
    rows = sorted(
        (p for p in pairs if p.variable == variable and _finite(p)),
        key=lambda p: (p.forecast_recorded_at_utc, p.valid_time_utc, p.provider),
    )
    sources_all = [source_id(p.provider, p.model_name) for p in rows]
    vocab = tuple(source_vocabulary) if source_vocabulary is not None else tuple(
        sorted(set(sources_all))
    )
    code_of = {s: i for i, s in enumerate(vocab)}
    rows = [p for p, s in zip(rows, sources_all, strict=True) if s in code_of]
    sources = [source_id(p.provider, p.model_name) for p in rows]
    points = [p.forecast_point for p in rows]
    valid = [ensure_utc(p.valid_time_utc) for p in rows]
    solar = solar_context(points, valid)
    route = [route_distance_km(pt) if route_distance_km else None for pt in points]
    x = build_features(
        source_codes=[code_of[s] for s in sources],
        forecast_values=[p.forecast_value for p in rows],
        points=points,
        lead_times_s=[p.lead_time_s for p in rows],
        solar=solar,
        route_distance_km=route,
    )
    observed = np.asarray([p.observed_value for p in rows], dtype=float)
    keep = np.ones(len(rows), dtype=bool)
    if variable in IRRADIANCE_VARIABLES:
        cs = solar.clear_sky_for(variable)
        keep = (solar.elevation_deg >= MIN_TRAIN_ELEVATION_DEG) & (cs > 0.0)
        with np.errstate(divide="ignore", invalid="ignore"):
            target = np.clip(np.where(cs > 0.0, observed / cs, 0.0), 0.0, K_MAX)
    else:
        cs = np.full(len(rows), math.nan)
        target = observed.copy()
    ds = Dataset(
        variable=variable,
        features=x,
        target=target,
        observed=observed,
        forecast=np.asarray([p.forecast_value for p in rows], dtype=float),
        clear_sky=np.asarray(cs, dtype=float),
        elevation_deg=solar.elevation_deg,
        lead_time_s=np.asarray([p.lead_time_s for p in rows], dtype=float),
        route_distance_km=np.asarray([math.nan if d is None else d for d in route], dtype=float),
        issued_at_utc=tuple(ensure_utc(p.forecast_recorded_at_utc) for p in rows),
        valid_time_utc=tuple(valid),
        sources=tuple(sources),
        source_vocabulary=vocab,
    )
    return ds.subset(keep)


def split_by_cutoff(ds: Dataset, cutoff_utc: datetime) -> Split:
    """Temporal split at ``cutoff_utc``: no training row is issued or verified after it."""
    cutoff = ensure_utc(cutoff_utc)
    issued = np.asarray([t > cutoff for t in ds.issued_at_utc], dtype=bool)
    verified_after = np.asarray([t > cutoff for t in ds.valid_time_utc], dtype=bool)
    train = ds.subset(~issued & ~verified_after)
    holdout = ds.subset(issued)
    actual = max(train.issued_at_utc) if len(train) else None
    return Split(train=train, holdout=holdout, training_cutoff_utc=actual)
