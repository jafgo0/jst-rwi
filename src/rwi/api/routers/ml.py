"""Experimental ML post-processing endpoints (ADR 0008 §6, phase 3).

``/api/ml/models`` lists saved artifacts, ``/api/ml/scores`` their holdout
metrics, and ``/api/ml/bands`` applies the loaded models to a live upstream
forecast. When no model can be loaded the answer is ``status: "no_model"``
with the reason - never an empty or invented band. Output is a forecast
(``rwi/ml-postprocess``), never an observation.
"""

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from rwi.api.capability import require_attr
from rwi.api.deps import LatQuery, LonQuery, resolve_point
from rwi.config import Settings, get_settings
from rwi.contracts import WeatherSeries
from rwi.core.provenance import DataKind
from rwi.core.state import StateStore, get_state_store
from rwi.core.timebase import TimeWindow, utc_now

router = APIRouter(prefix="/api/ml", tags=["ml"])

StoreDep = Annotated[StateStore, Depends(get_state_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

#: Variables ADR 0008 post-processes first: irradiance and wind.
DEFAULT_VARIABLES: tuple[str, ...] = ("ghi", "wind_speed_10m")

_CAPABILITY = "ML post-processing"


class MlModelScore(BaseModel):
    """Holdout metrics of one saved model version (ADR 0008 §5)."""

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    model_id: str
    model_version: str
    variable: str
    target_kind: str
    training_cutoff_utc: datetime
    calibrated: bool
    n_train: int
    n_holdout: int
    metrics: dict[str, float | None]


class MlLoadedModel(BaseModel):
    """Which model version produced a band."""

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    variable: str
    model_id: str
    model_version: str
    calibrated: bool


class MlBands(BaseModel):
    """ML quantile bands for a point, or an honest reason there are none."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "no_model", "not_applicable"]
    reason: str | None = None
    variables: tuple[str, ...]
    loaded: list[MlLoadedModel] = Field(default_factory=list)
    #: variable -> why no model was loaded for it
    missing: dict[str, str] = Field(default_factory=dict)
    #: P10/P50/P90 as ``Measured.value`` + ``uncertainty`` (80 % interval); None unless ok
    series: WeatherSeries | None = None


def _variables(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return DEFAULT_VARIABLES
    return tuple(dict.fromkeys(v.strip() for v in raw.split(",") if v.strip()))


@router.get("/models")
async def read_models(settings: SettingsDep) -> list[dict[str, Any]]:
    """Every readable model manifest under ``settings.models_dir``, oldest first."""
    list_manifests = require_attr("rwi.ml.artifacts", "list_manifests", capability=_CAPABILITY)
    return [m.model_dump(mode="json") for m in list_manifests(settings.models_dir)]


@router.get("/scores", response_model=list[MlModelScore])
async def read_scores(settings: SettingsDep) -> Any:
    """Holdout metrics per model version; empty when nothing is trained yet."""
    list_manifests = require_attr("rwi.ml.artifacts", "list_manifests", capability=_CAPABILITY)
    return [
        MlModelScore(
            model_id=m.model_id,
            model_version=m.model_version,
            variable=m.variable,
            target_kind=m.target_kind,
            training_cutoff_utc=m.training_cutoff_utc,
            calibrated=m.calibrated,
            n_train=m.n_train,
            n_holdout=m.n_holdout,
            metrics=m.metrics,
        )
        for m in list_manifests(settings.models_dir)
    ]


def _load_models(
    settings: Settings, variables: tuple[str, ...]
) -> tuple[dict[str, Any], dict[str, str]]:
    load_model = require_attr("rwi.ml.artifacts", "load_model", capability=_CAPABILITY)
    unavailable = require_attr("rwi.ml.quantile", "MlUnavailableError", capability=_CAPABILITY)
    models: dict[str, Any] = {}
    missing: dict[str, str] = {}
    for variable in variables:
        try:
            models[variable] = load_model(settings.models_dir, variable)
        except unavailable as exc:
            missing[variable] = str(exc)
    return models, missing


@router.get("/bands", response_model=MlBands)
async def read_bands(
    store: StoreDep,
    settings: SettingsDep,
    lat: Annotated[float | None, LatQuery] = None,
    lon: Annotated[float | None, LonQuery] = None,
    elev: Annotated[float | None, Query()] = None,
    hours: Annotated[float, Query(gt=0.0, le=240.0)] = 48.0,
    variables: Annotated[str | None, Query(description="comma-separated canonical names")] = None,
) -> Any:
    """P10/P50/P90 forecast from the latest model per variable, or ``no_model``."""
    wanted = _variables(variables)
    models, missing = _load_models(settings, wanted)
    if not models:
        return MlBands(
            status="no_model",
            reason="no ML model could be loaded for the requested variables",
            variables=wanted,
            missing=missing,
        )
    loaded = [
        MlLoadedModel(
            variable=v,
            model_id=manifest.model_id,
            model_version=manifest.model_version,
            calibrated=manifest.calibrated,
        )
        for v, (_, manifest) in sorted(models.items())
    ]

    available = require_attr(
        "rwi.providers.registry", "available_providers", capability="weather providers"
    )
    fetch_with_fallback = require_attr(
        "rwi.providers.registry", "fetch_with_fallback", capability="weather providers"
    )
    postprocessor_cls = require_attr(
        "rwi.ml.postprocess", "MlPostprocessor", capability=_CAPABILITY
    )
    point = resolve_point(lat, lon, elev, store)
    now = utc_now()
    window = TimeWindow(start_utc=now, end_utc=now + timedelta(hours=hours))
    upstream = await fetch_with_fallback(
        available(settings), point, window, tuple(sorted(models)), kind=DataKind.FORECAST
    )
    series = postprocessor_cls(models).apply(upstream, now_utc=now)
    if series is None:
        return MlBands(
            status="not_applicable",
            reason=(
                f"the loaded models do not apply to the upstream {upstream.provider}/"
                f"{upstream.model_name} forecast (source not trained on, no init time, "
                "or every step outside the training range)"
            ),
            variables=wanted,
            loaded=loaded,
            missing=missing,
        )
    return MlBands(status="ok", variables=wanted, loaded=loaded, missing=missing, series=series)
