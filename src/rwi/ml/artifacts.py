"""Versioned model artifacts on disk (ADR 0008 §5).

Layout: ``<root>/<model_id>/<model_version>/`` holding one LightGBM text model
per quantile (``q10.txt``, ``q50.txt``, ``q90.txt``) and ``manifest.json``.
Never pickle: text models are inspectable and cannot execute code on load.
The loader refuses a manifest whose feature-schema hash differs from the code's.
"""

import json
import math
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from rwi.core.timebase import utc_now
from rwi.ml.constraints import K_MAX
from rwi.ml.features import FEATURE_NAMES, feature_schema_hash
from rwi.ml.quantile import QUANTILES, MlUnavailableError, QuantileModel, lazy_lightgbm

__all__ = [
    "TARGET_KINDS",
    "ModelArtifactError",
    "ModelManifest",
    "git_sha",
    "list_manifests",
    "load_model",
    "model_id_for",
    "save_model",
]

#: Where the training target came from (ADR 0008 §2, irradiance rank order).
TargetKind = Literal["telemetry", "bom_one_minute", "era5_analysis", "bom_observation"]
TARGET_KINDS: tuple[str, ...] = ("telemetry", "bom_one_minute", "era5_analysis", "bom_observation")
_FILES = {q: f"q{round(q * 100):02d}.txt" for q in QUANTILES}


class ModelArtifactError(MlUnavailableError):
    """An artifact is missing, malformed or incompatible with this code."""


class ModelManifest(BaseModel):
    """Everything needed to trust (or refuse) a saved model."""

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    model_id: str
    model_version: str
    variable: str
    target_kind: TargetKind
    training_cutoff_utc: datetime
    data_window_start_utc: datetime
    data_window_end_utc: datetime
    feature_schema_hash: str
    feature_names: tuple[str, ...]
    quantiles: tuple[float, ...]
    k_max: float
    source_vocabulary: tuple[str, ...]
    feature_min: list[float | None]
    feature_max: list[float | None]
    n_train: int
    n_holdout: int
    #: holdout metrics, e.g. {"mae": ..., "crps": ..., "coverage_80": ...}
    metrics: dict[str, float | None] = Field(default_factory=dict)
    #: False when holdout 80 % interval coverage < 0.70 (outputs flagged UNVALIDATED)
    calibrated: bool
    git_sha: str | None = None
    lightgbm_version: str | None = None
    created_at_utc: datetime


def model_id_for(variable: str) -> str:
    """Model id for the post-processor of ``variable``."""
    return f"pp-{variable}"


def git_sha() -> str | None:
    """Current commit SHA, or None outside a git checkout."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _nan_to_none(a: np.ndarray) -> list[float | None]:
    return [None if math.isnan(float(v)) else float(v) for v in a]


def save_model(
    model: QuantileModel,
    *,
    root: Path,
    target_kind: TargetKind,
    training_cutoff_utc: datetime,
    data_window: tuple[datetime, datetime],
    n_train: int,
    n_holdout: int,
    metrics: dict[str, float | None],
    calibrated: bool,
    model_version: str | None = None,
) -> ModelManifest:
    """Write text models + manifest under ``root``; refuses to overwrite a version."""
    created = utc_now()
    version = model_version or created.strftime("%Y%m%dT%H%M%SZ")
    manifest = ModelManifest(
        model_id=model_id_for(model.variable),
        model_version=version,
        variable=model.variable,
        target_kind=target_kind,
        training_cutoff_utc=training_cutoff_utc,
        data_window_start_utc=data_window[0],
        data_window_end_utc=data_window[1],
        feature_schema_hash=feature_schema_hash(),
        feature_names=FEATURE_NAMES,
        quantiles=QUANTILES,
        k_max=K_MAX,
        source_vocabulary=model.source_vocabulary,
        feature_min=_nan_to_none(model.feature_min),
        feature_max=_nan_to_none(model.feature_max),
        n_train=n_train,
        n_holdout=n_holdout,
        metrics=metrics,
        calibrated=calibrated,
        git_sha=git_sha(),
        lightgbm_version=getattr(lazy_lightgbm(), "__version__", None),
        created_at_utc=created,
    )
    target = root / manifest.model_id / version
    if target.exists():
        raise ModelArtifactError(f"{target} already exists; versions are immutable")
    target.mkdir(parents=True)
    for q, name in _FILES.items():
        (target / name).write_text(model.boosters[q].model_to_string(), encoding="utf-8")
    (target / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return manifest


def _read_manifest(path: Path) -> ModelManifest:
    try:
        return ModelManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise ModelArtifactError(f"unreadable manifest {path}: {exc}") from exc


def list_manifests(root: Path, model_id: str | None = None) -> list[ModelManifest]:
    """Every readable manifest under ``root`` (optionally one model id), oldest first."""
    if not root.is_dir():
        return []
    pattern = f"{model_id}/*/manifest.json" if model_id else "*/*/manifest.json"
    out: list[ModelManifest] = []
    for path in sorted(root.glob(pattern)):
        try:
            out.append(_read_manifest(path))
        except ModelArtifactError:
            continue
    return sorted(out, key=lambda m: (m.model_id, m.created_at_utc, m.model_version))


def _floats(values: list[float | None]) -> np.ndarray:
    return np.asarray([math.nan if v is None else v for v in values], dtype=float)


def load_model(
    root: Path, variable: str, model_version: str | None = None
) -> tuple[QuantileModel, ModelManifest]:
    """Load a model (latest version by default); refuses an incompatible schema."""
    model_id = model_id_for(variable)
    if model_version is None:
        candidates = list_manifests(root, model_id)
        if not candidates:
            raise ModelArtifactError(f"no {model_id} model under {root}")
        model_version = candidates[-1].model_version
    folder = root / model_id / model_version
    manifest = _read_manifest(folder / "manifest.json")
    if manifest.feature_schema_hash != feature_schema_hash():
        raise ModelArtifactError(
            f"{model_id}/{model_version} was trained on another feature schema; retrain it"
        )
    if tuple(manifest.quantiles) != QUANTILES or manifest.variable != variable:
        raise ModelArtifactError(f"{model_id}/{model_version}: manifest does not match code")
    lgb = lazy_lightgbm()
    boosters: dict[float, Any] = {}
    for q, name in _FILES.items():
        try:
            text = (folder / name).read_text(encoding="utf-8")
        except OSError as exc:
            raise ModelArtifactError(f"missing model file {folder / name}") from exc
        boosters[q] = lgb.Booster(model_str=text)
    model = QuantileModel(
        variable=variable,
        boosters=boosters,
        source_vocabulary=tuple(manifest.source_vocabulary),
        feature_min=_floats(manifest.feature_min),
        feature_max=_floats(manifest.feature_max),
    )
    return model, manifest
