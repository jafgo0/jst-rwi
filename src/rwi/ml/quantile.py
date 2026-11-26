"""LightGBM quantile regression, one booster per quantile (ADR 0008 §2).

LightGBM is in the optional ``[ml]`` extra and imported lazily. Training is
deterministic (one thread, fixed seed, ``deterministic=True``) so a retrain on
the same rows reproduces the same model text.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from rwi.ml.constraints import IRRADIANCE_VARIABLES, clip_to_spec, irradiance_from_k
from rwi.ml.dataset import Dataset
from rwi.ml.features import FEATURE_NAMES, SOURCE_FEATURE

__all__ = [
    "LGB_PARAMS",
    "MIN_TRAIN_ROWS",
    "QUANTILES",
    "MlUnavailableError",
    "QuantileModel",
    "lazy_lightgbm",
    "train_quantile_model",
]

FloatArray = NDArray[np.float64]

QUANTILES: tuple[float, float, float] = (0.1, 0.5, 0.9)
#: Below this many training rows a quantile model is noise; refuse to fit.
MIN_TRAIN_ROWS = 50
LGB_PARAMS: Mapping[str, Any] = {
    "objective": "quantile",
    "learning_rate": 0.05,
    "num_leaves": 15,
    "min_data_in_leaf": 20,
    "feature_fraction": 1.0,
    "bagging_fraction": 1.0,
    "num_threads": 1,
    "seed": 20270822,
    "deterministic": True,
    "force_row_wise": True,
    "verbose": -1,
}
NUM_BOOST_ROUND = 200


class MlUnavailableError(RuntimeError):
    """The optional ML dependencies are missing or a model cannot be used."""


def lazy_lightgbm() -> Any:
    """The ``lightgbm`` module, or `MlUnavailableError` without the ``[ml]`` extra."""
    try:
        import lightgbm
    except ImportError as exc:  # pragma: no cover - depends on the [ml] extra
        raise MlUnavailableError(
            "ML post-processing needs the optional [ml] extra (lightgbm)"
        ) from exc
    except OSError as exc:  # pragma: no cover - native runtime (libomp) missing
        raise MlUnavailableError(f"lightgbm is installed but cannot load: {exc}") from exc
    return lightgbm


@dataclass
class QuantileModel:
    """Trained boosters for one variable plus the feature envelope they saw."""

    variable: str
    boosters: dict[float, Any]
    source_vocabulary: tuple[str, ...]
    #: per-feature (min, max) over training rows; NaN where the column was all-NaN
    feature_min: FloatArray
    feature_max: FloatArray
    extra: dict[str, Any] = field(default_factory=dict)

    def in_range(self, x: FloatArray) -> NDArray[np.bool_]:
        """Rows whose every feature lies inside the training envelope.

        The categorical source code must be in the vocabulary. A NaN feature is
        accepted only where training also had no value range (all-NaN column).
        """
        x = np.atleast_2d(np.asarray(x, dtype=float))
        ok = np.ones(x.shape[0], dtype=bool)
        for j in range(x.shape[1]):
            col = x[:, j]
            if j == SOURCE_FEATURE:
                ok &= np.isfinite(col) & (col >= 0) & (col < len(self.source_vocabulary))
                continue
            lo, hi = self.feature_min[j], self.feature_max[j]
            if math.isnan(lo):
                ok &= np.isnan(col)
            else:
                ok &= np.isfinite(col) & (col >= lo) & (col <= hi)
        return ok

    def predict_raw(self, x: FloatArray) -> FloatArray:
        """Unconstrained model output ``(n, 3)`` (clear-sky index for irradiance)."""
        x = np.atleast_2d(np.asarray(x, dtype=float))
        return np.column_stack([self.boosters[q].predict(x) for q in QUANTILES])

    def predict(
        self, x: FloatArray, *, clear_sky: FloatArray, elevation_deg: FloatArray
    ) -> FloatArray:
        """Physical quantiles ``(n, 3)`` with every constraint of `rwi.ml.constraints`."""
        raw = self.predict_raw(x)
        if self.variable in IRRADIANCE_VARIABLES:
            return irradiance_from_k(raw, clear_sky, elevation_deg)
        return clip_to_spec(self.variable, raw)


def _envelope(x: FloatArray) -> tuple[FloatArray, FloatArray]:
    lo = np.full(x.shape[1], math.nan)
    hi = np.full(x.shape[1], math.nan)
    for j in range(x.shape[1]):
        col = x[:, j][np.isfinite(x[:, j])]
        if col.size:
            lo[j], hi[j] = float(col.min()), float(col.max())
    return lo, hi


def train_quantile_model(
    ds: Dataset, *, num_boost_round: int = NUM_BOOST_ROUND
) -> QuantileModel:
    """Fit one LightGBM booster per quantile on ``ds`` (deterministic)."""
    if len(ds) < MIN_TRAIN_ROWS:
        raise MlUnavailableError(
            f"{ds.variable}: {len(ds)} training rows < {MIN_TRAIN_ROWS}; refusing to fit"
        )
    lgb = lazy_lightgbm()
    boosters: dict[float, Any] = {}
    for q in QUANTILES:
        train_set = lgb.Dataset(
            ds.features,
            label=ds.target,
            feature_name=list(FEATURE_NAMES),
            categorical_feature=[SOURCE_FEATURE],
            free_raw_data=False,
        )
        params = {**LGB_PARAMS, "alpha": q}
        boosters[q] = lgb.train(params, train_set, num_boost_round=num_boost_round)
    lo, hi = _envelope(ds.features)
    return QuantileModel(
        variable=ds.variable,
        boosters=boosters,
        source_vocabulary=ds.source_vocabulary,
        feature_min=lo,
        feature_max=hi,
    )
