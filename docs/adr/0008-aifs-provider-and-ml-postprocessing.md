# ADR 0008 — ECMWF AIFS as a gridded provider, and a quantile ML post-processing layer

Status: **Accepted** 2026-11-26. Owner answered A, B and C yes. §1 is implemented (`rwi.providers.gridded`, `rwi.providers.ecmwf_aifs`); §2–§6 are in progress.
Date: 2026-11-26
Requirements: #69 Machine Learning, #70 AI models, #58 Local Downscaling, #76 Statistical Calibration, #103 Cloud Shadow Prediction

## Context

The platform reaches ECMWF only through Open-Meteo, which ADR 0004 and `docs/research/data-sources.md` §3 chose for phase 1. Every forecast is deterministic, apart from the Open-Meteo ensemble that `rwi.ensemble` summarises. The goal now is:

1. Ingest ECMWF AIFS directly from ECMWF open data (GRIB2), with run-level provenance.
2. Learn a correction from forecast to observation that outputs P10/P50/P90 for GHI, DNI, cloud cover and u/v wind, under physical constraints.
3. Score every source against the others: Open-Meteo, AIFS and the ML model.
4. Feed the bands to strategy, so that a P10 day plans differently from a P50 day.

The disk state on 2026-11-26 constrains what is achievable:

- **The archive cannot train an irradiance model.** `data/archive/rwi.sqlite3` holds 3,084 Open-Meteo forecast series at 21 sites, from 2026-11-02 to 2026-11-26. It holds 6 BoM observation series, all from 2026-11-02. It holds no irradiance observations at all.
- **No ML dependencies are installed.** torch, lightgbm, ecmwf-opendata, xarray, cfgrib and eccodes are all missing from `.venv`.
- **AIFS history is short.** ECMWF open data keeps a rolling window of recent runs. Unless the cloud mirrors keep a longer history (unverified), the AIFS training data begins on the day archiving begins.

The race is 22–29 August 2027. A model trained before then will have seen roughly nine months of corridor weather, none of it from an August.

## Decision

### 1. AIFS is a gridded source behind the existing `WeatherProvider` protocol

- New `rwi.providers.gridded` defines a provider-internal protocol:
  - `GriddedSource.fetch_run(init_time, steps, variables, bbox) -> GriddedRun`.
  - `GriddedRun` holds one model run cropped to a bbox, as plain numpy arrays (t, lat, lon); xarray is only needed to decode GRIB.
  - It stays inside `rwi.providers` and never crosses a module boundary, so it is not a contract type.
- New `rwi.providers.ecmwf_aifs.AifsOpenDataSource` implements `GriddedSource` with `ecmwf-opendata` and `cfgrib`.
- A generic `GriddedProvider(BaseProvider)` adapts any `GriddedSource` to `WeatherProvider.fetch_forecast(point, window, variables)`. Aurora or GraphCast later means writing one new `GriddedSource`, not a new provider.
- **The bbox comes from the caller,** for example the active route's extent plus a margin. It is never hardcoded (hard rule 4).
- **Downloads and caching:**
  - one download per run;
  - the cropped run is cached in memory per provider, keyed by init time; with `allow_network=False` the newest cached run is served. A NetCDF cache under `data/cache/` is a later step;
  - point queries are bilinear in space and recorded as `processing="bilinear_grid_interp"`.
- **Provenance on every value:**
  - `provider="ecmwf"`, `dataset="aifs-single-open-data"`, `kind=FORECAST`;
  - `model_name="aifs"`, `model_version` taken from GRIB metadata;
  - `model_init_time_utc` and `valid_time_utc`;
  - `point` set to the interpolated point, and `requested_point`;
  - `spatial_resolution_km≈28`, `license="CC-BY-4.0"`.
- **Solar radiation:**
  - Surface solar radiation is accumulated in J/m². It is de-accumulated to an interval-mean W/m².
  - AIFS open data is published at 6-hourly steps; verify this against ECMWF's parameter list before coding. A 6-hour mean is useless for the car, so hourly GHI is built as follows:
    - Compute the interval clear-sky index *k = GHI_mean / GHI_cs_mean*, with the clear-sky term from pvlib (ADR 0003).
    - Interpolate *k* in time.
    - Multiply by the hourly pvlib clear-sky.
  - The value is recorded as `processing="deaccumulated;clearsky_index_time_interp"`.
  - GHI is exactly 0 wherever pvlib puts the sun below the horizon (hard rule 5).
- **DNI:** AIFS publishes no DNI. DNI is derived from GHI with pvlib's DIRINT decomposition at the hour midpoint. `kind` stays FORECAST (the value is still a forecast) and `processing` records `dirint_decomposition`, matching `rwi.solar.decompose_ghi`. It is exactly 0 below the horizon.
- **Rate limits and tests:**
  - Live fetches obey the ECMWF open-data rate limits.
  - Tests never hit the network; they parse a committed GRIB fixture (open decision C).

### 2. `rwi.ml` post-processes forecasts with quantile gradient boosting

**Why LightGBM, not PyTorch.**
- The problem is tabular, the data is small, inference runs on a CPU and needs to be explainable.
- LightGBM's native `quantile` objective fits this directly.
- PyTorch is deferred to nowcasting (§3).

**Training store.** `rwi.ml.dataset` builds the training rows from `SqliteArchive.pairs`, whose leakage guard already exists (ADR 0007).
- A row is one (forecast issue, valid time, site) triple. Features:
  - upstream forecast values and source id;
  - pvlib clear-sky GHI and DNI;
  - solar elevation;
  - lead time;
  - hour-angle sin/cos;
  - latitude and longitude;
  - distance along the route.
- The split is strictly by **issue time**. Every training row was issued before `training_cutoff_utc`, and every evaluation row after it. There is no random K-fold.

**Targets.**
- **Wind and cloud** train against BoM observations.
- **Irradiance** trains against whichever source exists, ranked as follows:
  1. car or ground pyranometer telemetry (`OBSERVATION`);
  2. BoM one-minute solar stations, if obtained (`OBSERVATION`);
  3. ERA5 surface solar radiation (`ANALYSIS`).
- A model trained on ERA5 learns to predict ERA5, not truth. Its manifest records `target_kind="analysis"`, and the UI says so.

**Irradiance target.**
- The model predicts the **clear-sky index** *k*, not raw W/m², with one LightGBM model per quantile (0.1, 0.5, 0.9).
- The output is *k × GHI_cs*. This makes "≥ 0" and "= 0 at night" structural, not learned.
- *k* is clipped to [0, k_max]. k_max is a named constant (cloud-enhancement allowance) with a science test.

**Constraints, enforced in code after prediction and tested as invariants.**
- Quantiles are sorted per value (no crossing).
- GHI and DNI are ≥ 0.
- GHI and DNI are exactly 0.0 when solar elevation ≤ 0.
- DNI ≤ DNI_cs × k_max.
- Cloud cover is within [0, 100] %.

**Output shape. No new contract type.**
- Each value is a `Measured` with:
  - `value` = P50;
  - `uncertainty = Uncertainty(lower=P10, upper=P90, interval_probability=0.8, method="quantile_regression:lightgbm")`.
- `Uncertainty` already exists in `rwi.core.provenance`, and nothing populates it today.
- Provenance fields:
  - `provider="rwi"`, `dataset="ml-postprocess"`, `kind=FORECAST`;
  - `model_name="<model id>"`, `model_version`;
  - `model_init_time_utc` = the upstream run's init time;
  - `processing` names the upstream source.

**No model, no ML output.**
- If no model artifact is loaded, or the input lies outside the trained feature range, the layer returns nothing.
- It never passes the raw forecast through under the ML label.

**Calibration.** Interval coverage is checked on the held-out period with `rwi.verification.calibration`. A model whose 80 % interval covers less than 70 % of held-out observations is flagged `UNVALIDATED`, not served as OK.

### 3. Nowcast is an interface, not a model

`rwi.ml.nowcast` defines a `Nowcaster` protocol and a patch extractor over `rwi.satellite`. It ships a persistence baseline, the existing advection nowcast behind the protocol, and no neural network. A U-Net or ConvLSTM goes in when there are labelled Himawari patches and a GPU budget. Until then it is a stub that raises, not a model that pretends.

### 4. Scoring

- CRPS goes into `rwi.verification.metrics` so that every source can use it:
  - for quantiles, through the pinball-loss approximation;
  - for ensembles, through the energy form.
- `rwi.ml.scoring` compares Open-Meteo, AIFS and the ML model on the same held-out pairs. It reports MAE, RMSE, CRPS and bias by lead-time bucket and route segment, using the existing bucket helpers.

### 5. Artifacts

- Saved to `data/models/<model_id>/<model_version>/`:
  - LightGBM **text** models, never pickle or joblib, so loading runs no code;
  - `manifest.json` containing model_version, training_cutoff_utc, target_kind, the feature schema hash, the git SHA, the data window and the held-out metrics.
- The loader refuses a manifest whose feature schema hash differs from the code's.

### 6. Strategy and API

- **Strategy:**
  - `rwi.strategy` gains a `generation_band: Literal["p10", "p50", "p90"]` input.
  - When bands exist, a P10 run replaces the current fixed `uncertainty_fraction` scaling with the model's own lower bound.
  - When bands do not exist, behaviour is unchanged.
- **API:**
  - Bands and metrics are served first under a new **experimental** `/api/ml/*` prefix: `/models`, `/bands`, `/scores`.
  - They are not added to the stable product surface. Promotion to `/api/forecast-quality` is a later, version-bumped change under the product-contract rail.

### 7. Dependencies

- A new optional extra `[ml]` in `pyproject.toml`: `ecmwf-opendata`, `xarray`, `cfgrib`, `eccodes`, `lightgbm`.
- `torch` is not included until §3 needs it.
- Core runtime and `make check` must keep passing **without** the extra:
  - `rwi.ml` and `ecmwf_aifs` import their heavy dependencies lazily;
  - their tests use `pytest.importorskip`.
- A separate CI job with `[ml]` installed runs them.

## Open decisions (answered 2026-11-26: A, B and C yes)

- **A. `pyproject.toml` `[ml]` extra.** Editing it and installing it is the owner's call (CLAUDE.md §4). The install is `uv pip install -e '.[ml]'`, run by the owner.
- **B. Add `training_cutoff_utc: datetime | None = None` to `rwi.core.provenance.Provenance`.**
  - It is additive and non-breaking.
  - It is a `core/` edit, and it moves the product schema: version bump plus `make product-schema`.
  - The alternative is to put the cutoff in `notes`, which is untyped and easy to lose. Recommended: add the field.
- **C. GRIB test fixture.**
  - The recommended option is one real AIFS run, cropped to a small corridor bbox and to the 5–6 parameters used.
    - It is downloaded once from data.ecmwf.int, under 1 MB after cropping, and committed with a provenance README.
    - The download needs explicit permission.
  - The alternative is a synthetic GRIB built from an eccodes sample template. It is labelled `SIMULATION` and is weaker at catching real-file parsing bugs.

## Consequences

- AIFS becomes a second, independent ECMWF path, with init-time provenance that Open-Meteo does not always expose. It costs one GRIB stack (eccodes) on every machine that runs it.
- **ML output exists only once training data exists.** Until telemetry, BoM solar or a deliberately ERA5-targeted model is in place, the honest state of #69 is "implementing, no trained model". The dashboard must show "no model" rather than an empty band.
- **Archiving AIFS must start as early as possible.** Every week not archived is a week of training data that cannot be recovered.
- The `rwi.ml` lane must be added to the ownership table in `docs/CONTRACTS.md` and CLAUDE.md §2.

## Alternatives considered

- **AIFS via Open-Meteo (`ecmwf_aifs025` model).**
  - For: zero new dependencies, and it slots into `openmeteo.py`.
  - Kept as a cross-check source, not the primary path, because:
    - run selection and init-time provenance are Open-Meteo's, not ECMWF's;
    - the interpolation is opaque;
    - adding Aurora or GraphCast would still need the gridded path.
- **PyTorch MLP or quantile network for post-processing.** More capacity than about 10⁴ rows can support, a heavier dependency, and no gain over GBM on tabular data at this size.
- **Predicting raw W/m² and clamping.**
  - Rejected: clamping hides a model that has learned non-physical values, and hard rule 5 wants exact zeros from geometry, not from a clamp.
- **A new `QuantileMeasured` contract type.** Rejected: `Measured.uncertainty` already carries a central interval with its probability. A new type would duplicate it and break every consumer.
- **Random train/test split.** Rejected: it leaks weather regimes across the split and inflates skill.
