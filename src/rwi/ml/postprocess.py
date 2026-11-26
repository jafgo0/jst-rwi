"""Apply trained quantile models to an upstream forecast series (ADR 0008 §2).

The output is a new `WeatherSeries` from provider ``rwi`` / dataset
``ml-postprocess``, kind FORECAST: P50 as the value, P10-P90 as an 80 %
`Uncertainty`. It never replaces the upstream series; a higher layer keeps both.

Refusals are silent omissions, never pass-through: a step whose features fall
outside the training envelope gets no ML value at all, so a raw upstream number
is never relabelled as post-processed.
"""

from collections.abc import Mapping
from datetime import datetime

import numpy as np

from rwi.contracts import WeatherSeries, WeatherStep
from rwi.core.provenance import DataKind, Measured, Provenance, QualityFlag, Uncertainty
from rwi.core.timebase import ensure_utc, utc_now
from rwi.core.variables import spec_for
from rwi.ml.artifacts import ModelManifest
from rwi.ml.constraints import IRRADIANCE_VARIABLES
from rwi.ml.features import build_features, solar_context, source_id
from rwi.ml.quantile import QuantileModel

__all__ = ["ML_DATASET", "ML_PROVIDER", "UNCERTAINTY_METHOD", "MlPostprocessor"]

ML_PROVIDER = "rwi"
ML_DATASET = "ml-postprocess"
UNCERTAINTY_METHOD = "quantile_regression:lightgbm"
#: P10-P90 nominal probability
INTERVAL_PROBABILITY = 0.8


class MlPostprocessor:
    """Quantile post-processing of upstream forecasts with loaded models."""

    def __init__(self, models: Mapping[str, tuple[QuantileModel, ModelManifest]]) -> None:
        self._models = dict(models)

    @property
    def variables(self) -> tuple[str, ...]:
        """Variables a model is loaded for."""
        return tuple(sorted(self._models))

    def apply(
        self,
        series: WeatherSeries,
        *,
        now_utc: datetime | None = None,
        route_distance_km: float | None = None,
    ) -> WeatherSeries | None:
        """ML quantile forecast for ``series`` (provenance ``rwi/ml-postprocess``), or None.

        None when the series is not a forecast, has no init time (no lead time),
        comes from a source no model was trained on, or no step is in range.
        """
        if series.kind is not DataKind.FORECAST or series.model_init_time_utc is None:
            return None
        init = ensure_utc(series.model_init_time_utc)
        src = source_id(series.provider, series.model_name)
        retrieved = ensure_utc(now_utc) if now_utc else utc_now()
        times = [ensure_utc(s.valid_time_utc) for s in series.steps]
        if not times:
            return None
        solar = solar_context([series.point] * len(times), times)
        out_values: list[dict[str, Measured]] = [{} for _ in times]
        any_value = False
        for variable, (model, manifest) in self._models.items():
            if src not in model.source_vocabulary:
                continue
            code = model.source_vocabulary.index(src)
            rows = [
                i
                for i, step in enumerate(series.steps)
                if step.value(variable) is not None and times[i] >= init
            ]
            if not rows:
                continue
            x = build_features(
                source_codes=[code] * len(rows),
                forecast_values=[float(series.steps[i].value(variable) or 0.0) for i in rows],
                points=[series.point] * len(rows),
                lead_times_s=[(times[i] - init).total_seconds() for i in rows],
                solar=solar.take(rows),
                route_distance_km=[route_distance_km] * len(rows),
            )
            ok = model.in_range(x)
            if variable in IRRADIANCE_VARIABLES:
                cs = solar.clear_sky_for(variable)[rows]
            else:
                cs = np.full(len(rows), np.nan)
            elev = solar.elevation_deg[rows]
            q = model.predict(x, clear_sky=cs, elevation_deg=elev)
            # Below the horizon the answer is physics, not the model: 0.0 regardless of range.
            night = elev <= 0.0
            for j, i in enumerate(rows):
                if not ok[j] and not (variable in IRRADIANCE_VARIABLES and night[j]):
                    continue
                out_values[i][variable] = _measured(
                    variable, q[j], manifest, series, times[i], init, retrieved
                )
                any_value = True
        if not any_value:
            return None
        steps = [
            WeatherStep(valid_time_utc=t, values=v)
            for t, v in zip(times, out_values, strict=True)
            if v
        ]
        return WeatherSeries(
            point=series.point,
            requested_point=series.requested_point,
            provider=ML_PROVIDER,
            dataset=ML_DATASET,
            kind=DataKind.FORECAST,
            model_name="+".join(sorted({m.model_id for _, m in self._models.values()})),
            model_version=None,
            model_init_time_utc=init,
            retrieved_at_utc=retrieved,
            spatial_resolution_km=series.spatial_resolution_km,
            temporal_resolution_s=series.temporal_resolution_s,
            license=series.license,
            source_url=None,
            steps=steps,
        )


def _measured(
    variable: str,
    q: np.ndarray,
    manifest: ModelManifest,
    upstream: WeatherSeries,
    valid: datetime,
    init: datetime,
    retrieved: datetime,
) -> Measured:
    p10, p50, p90 = (float(v) for v in q)
    provenance = Provenance(
        provider=ML_PROVIDER,
        dataset=ML_DATASET,
        kind=DataKind.FORECAST,
        retrieved_at_utc=retrieved,
        valid_time_utc=valid,
        model_name=manifest.model_id,
        model_version=manifest.model_version,
        model_init_time_utc=init,
        training_cutoff_utc=manifest.training_cutoff_utc,
        point=upstream.point,
        processing=(
            f"quantile post-processing of {source_id(upstream.provider, upstream.model_name)}"
            f" (target {manifest.target_kind})"
        ),
        license=upstream.license,
        notes=None if manifest.calibrated else "holdout P10-P90 coverage below 0.70",
    )
    return Measured(
        variable=variable,
        value=p50,
        unit=spec_for(variable).unit,
        provenance=provenance,
        quality=QualityFlag.OK if manifest.calibrated else QualityFlag.UNVALIDATED,
        uncertainty=Uncertainty(
            lower=p10,
            upper=p90,
            interval_probability=INTERVAL_PROBABILITY,
            method=UNCERTAINTY_METHOD,
        ),
    )
