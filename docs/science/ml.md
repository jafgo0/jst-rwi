# ML post-processing and nowcast (`rwi.ml`)

Decision record: ADR 0008. This page covers the methods, assumptions and validation status.

## 1. What it does

It corrects raw point forecasts (Open-Meteo, BoM, AIFS, …) and turns them into
**P10/P50/P90** quantile forecasts for `ghi`, `dni`, `cloud_cover` and
`wind_speed_10m`. Each variable has one LightGBM model per quantile level.
The output is a `WeatherSeries` with `provider="rwi"`, `dataset="ml-postprocess"` and
`kind=FORECAST`. It is never an observation.

## 2. Training data: leak-free by construction

- Input: `ForecastObservationPair` rows from the archive (`rwi.archive`).
- `split_by_cutoff(ds, cutoff)`:
  - **train** = rows issued ≤ cutoff **and** valid ≤ cutoff;
  - **holdout** = rows issued > cutoff;
  - rows issued before the cutoff but verifying after it are **dropped**, because their
    observation was not yet known at the cutoff.
- The manifest records `training_cutoff_utc` (the latest issue time in train) and the data window.

## 3. Features and target

- Features: the source code, the raw forecast value, pvlib clear-sky GHI/DNI,
  solar elevation, lead time, hour angle (sin/cos), lat/lon, and route km
  (`rwi.ml.features.FEATURE_NAMES`). `feature_schema_hash()` is stored in the manifest,
  and loading refuses on a mismatch.
- Irradiance target: the clear-sky index k = obs / clear sky, clipped to [0, K_MAX].
  Training uses only rows with elevation ≥ 5° (small denominators make k unstable).
- Other variables: the physical value directly.

## 4. Physical constraints (`constraints.py`)

1. Quantiles are sorted, so they never cross.
2. Irradiance = k·clear sky, with k ∈ [0, 1.5]. K_MAX = 1.5 is the cloud-enhancement bound
   (Gueymard 2017, RSER 74).
3. Sun ≤ 0° elevation ⇒ irradiance is **exactly 0.0** (hard rule 5), whatever the model says.
4. Other variables are clipped to the canonical spec range (`rwi.core.variables`).

## 5. Applicability envelope

`QuantileModel.in_range` rejects a row whose source is not in the training vocabulary or
whose feature lies outside the training min/max. Such steps are **omitted**, never
extrapolated. Night irradiance steps are always emitted as 0.

## 6. Scoring (`scoring.py`)

- MAE, RMSE, bias, CRPS (on three quantiles) and 80 % coverage. They are given for all rows,
  per lead-time bucket, and per 500 km route segment.
- ML and every raw source are scored on **the same rows** (the in-range holdout). A raw
  forecast is scored as three equal quantiles, so its CRPS equals its MAE, which makes the
  comparison like for like (verification.md §11).
- `calibrated` = coverage_80 ≥ 0.70. An uncalibrated model gives `quality=UNVALIDATED`
  with a note.

## 7. Artifacts and the CLI

- Layout: `data/models/pp-<var>/<version>/{q10,q50,q90}.txt, manifest.json`. A version is
  immutable, and an overwrite is refused. `data/models/` is git-ignored.
- Commands:
  - `python -m rwi.ml retrain --variable ghi --cutoff 2027-08-01T00:00Z --target-kind bom_observation`
  - `python -m rwi.ml eval --variable ghi [--version v] [--after ts]`
  - `python -m rwi.ml list`

## 7a. API and strategy (phase 3, experimental)

- `/api/ml/models`: every readable manifest under `RWI_MODELS_DIR` (default `data/models`).
- `/api/ml/scores`: holdout metrics, cutoff, `calibrated` per model version.
- `/api/ml/bands?lat&lon&hours&variables`: the latest model per variable applied to a live
  upstream forecast. Only variables with a loaded model are fetched. The result is one of:
  - `ok` with a `WeatherSeries` (`rwi/ml-postprocess`, kind `forecast`);
  - `no_model` with the reason per variable, and the upstream is never called;
  - `not_applicable` when the upstream source, init time or range falls outside training.

  The answer is never an empty band.
- Tier `experimental` (`X-Rwi-Product-Tier`). Not part of `openapi.v0.json`'s stable surface;
  promotion into `/api/weather` needs the phase-4 validation.
- Strategy: `rwi.strategy.segments.generation_band_from_ghi` turns these bands into segment
  P10/P90 generation (see `docs/science/vehicle.md` §5.6a).

## 8. Nowcast (`nowcast.py`)

`Nowcaster` is a Protocol. `PersistenceNowcaster` and `AdvectionNowcaster` wrap
`rwi.satellite.nowcast`. `NeuralNowcaster` is a placeholder that raises
`NotImplementedError` until there is training data. `extract_patch` cuts out the satellite
patch around a point (its future input).

## 9. Validation status

| Part | Status |
|---|---|
| Leak-free split, features, constraints | tested |
| Quantile training, artifacts, post-processor, CLI | tested on synthetic data; **skipped without libomp** |
| K_MAX = 1.5, night = 0 | science tests |
| `/api/ml/*`, strategy generation band | tested (API with mocked upstream) |
| Skill on real race data | **not validated**: needs an archive with real pairs |

Runtime requirement: lightgbm needs `libomp` on macOS. Without it, `lazy_lightgbm()` raises
`MlUnavailableError` and everything that needs training is skipped.
