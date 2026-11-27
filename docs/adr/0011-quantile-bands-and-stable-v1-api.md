# ADR 0011 — Quantile bands, wind as u/v, on-board truth and a stable `/api/v1`

Status: **Accepted** 2026-11-27. The owner approved three things: (a) `QuantileBand` in `rwi.contracts`; (b) registering `wind_u_10m` and `wind_v_10m` in `rwi.core.variables`; (c) the conservative box projection for head/cross-wind bands. BSRN is deferred to its own ADR.
Date: 2026-11-27
Requirements: #46 Wind Forecasting, #54 Probabilistic Forecasting, #59 Forecast Verification, #76 Statistical Calibration
Related: ADR 0005 (contracts), ADR 0007 (archive), ADR 0008 (ML post-processing), ADR 0010 (on-board sensors)

## Context

ADR 0008 built quantile post-processing. Its output reaches consumers only through `/api/ml/bands`, which is EXPERIMENTAL. That endpoint returns a `WeatherSeries` whose `Measured.uncertainty` holds P10/P90. Four gaps stand in the way of a stable product:

1. **No band type.** The band exists only as an `Uncertainty(lower, upper)` on a P50 value inside a router-specific envelope. A strategy consumer cannot depend on that shape.
2. **Wind is post-processed as a speed, not a vector.** Head-wind and cross-wind depend on the route heading, so they cannot be derived from a speed band at all. They need the wind vector.
3. **No measured truth.** ADR 0010 brought ML-01 GHI and Airmar observations into the archive, but nothing trains or verifies against them.
4. **No versioned stable endpoint.** There is no route-aware band endpoint and no verification endpoint that the strategy repository can pin.

## Decision

### 1. `QuantileBand` (contract)

`rwi.contracts.QuantileBand` holds one variable at one place and valid time.

- **Fields:** `variable`, `unit`, `valid_time_utc`, `point`, `p10`, `p50`, `p90`, `method`, `interval_probability`, `quality`, `ml_derived`, `provenance`.
- **Validators:**
  - `p10 <= p50 <= p90`.
  - `unit` matches the registry.
  - The provenance kind is never `OBSERVATION`.
  - `provenance.valid_time_utc` equals the band's valid time.
  - When `ml_derived` is true, `provenance.model_version` and `provenance.training_cutoff_utc` are required. A band that cannot say which model made it, or up to when that model saw data, cannot be verified without leakage.
- `Provenance` itself does not change.
- The derived route-relative quantities are `headwind_10m` and `crosswind_10m`. They are not forecastable variables, so they are not registered in `rwi.core.variables`. `QuantileBand` lists them with their unit in `ROUTE_RELATIVE_BAND_UNITS`.
  - Headwind is positive when the wind opposes travel.
  - Crosswind is positive when the wind comes from the left.
  - Both follow `rwi.core.wind`.

### 2. Wind as u/v

- `wind_u_10m` and `wind_v_10m` (m/s, range ±120) are registered in a new `DERIVED_VARIABLES` registry, not in `CANONICAL_VARIABLES`: that set is what every forecast provider must serve (`rwi.providers.openmeteo` asserts it), and no provider serves u/v. They use the meteorological convention: u is positive toward the east, v positive toward the north.
- No provider serves them directly. `rwi.ml.targets` derives them from `wind_speed_10m` and `wind_direction_10m` with `rwi.core.wind.met_to_uv`, for forecasts and observations alike.
  - A forecast pair and an observation pair are joined only when the forecast run, the valid time and the matched observation are all the same.
  - Speed 0 gives (0, 0).
- At inference, the post-processor adds the u/v steps to the upstream series before applying the u/v models.
- `temperature_2m` becomes an ML variable as well, so that the route endpoint can return a temperature band.

### 3. Truth hierarchy

Each variable lists its truth sources in priority order. For each forecast (run, valid time), the first source with a matched observation wins, and the row records which one it used.

| Variable | Truth, in order |
|---|---|
| `ghi` | `jst-onboard` / ML-01 → BoM |
| `temperature_2m` | `jst-onboard` / Airmar (only at screen height, per ADR 0010) → BoM |
| `wind_u_10m`, `wind_v_10m`, `wind_speed_10m` | BoM |
| `dni`, `cloud_cover` | BoM |

- The archive's pair query gains an optional observation-provider filter.
- A model's `target_kind` records the mix it was trained on:
  - `telemetry` when every row is on-board;
  - `bom_observation` when every row is BoM;
  - `telemetry+bom_observation` when it is mixed.
- **Airmar wind is not 10 m truth.** It is measured at sensor height, typically 1–2 m on a support car, and ADR 0010 keeps it under `wind_*_sensor`. Using it as 10 m truth would bias the model low, so it is excluded until a height correction has its own ADR.
- **BSRN** (Alice Springs, ASP) is a planned secondary irradiance truth. It is a new external source with its own licence and access terms, so it gets its own ADR and is not part of this one.

### 4. Head/cross-wind bands: conservative box projection

The u and v models are separate marginal quantile models with no joint distribution. Projecting the P10 and P90 corners would claim an 80 % interval that the data do not support.

- **P50** is the projection of (u₅₀, v₅₀) onto the route frame at that point's heading.
- **P10/P90** are the minimum and maximum of the projection over the four corners of the box [u₁₀, u₉₀] × [v₁₀, v₉₀]. Projection is linear, so the extremes lie on corners.
- If each marginal interval had its nominal 80 % coverage, the box would contain the truth with probability ≥ 1 − 0.2 − 0.2 = 0.6 (Bonferroni). The projected interval contains the box image, so it inherits that bound.
- The band is labelled `method="uv_box_projection"` with `interval_probability=None`, because 0.6 is a lower bound, not a nominal probability.
- `/api/v1/verification` reports the coverage actually achieved.
- For a combined band, `model_version` is `u:<ver>+v:<ver>` and `training_cutoff_utc` is the later of the two cutoffs, the one that matters for leakage.

### 5. Stable `/api/v1`

- `/api/v1` is a new STABLE prefix in `rwi.api.product.TIER_BY_PREFIX`. `/api/ml/*` stays EXPERIMENTAL.
- **`GET /api/v1/forecast/route`** samples the predicted trajectory, using the same sampling and horizon as `/api/route/{id}/weather`.
  - Each point returns bands for `ghi`, `dni`, `cloud_cover`, `temperature_2m`, `headwind_10m` and `crosswind_10m`.
  - Every band carries its provenance and `model_version`.
  - When a band is not available, the point says why, per variable: `no_model`, `not_applicable`, `no_upstream` or `outside_training_range`. Nothing is invented, and a raw upstream value is never relabelled as a band.
- **`GET /api/v1/verification`** scores each saved model on archive pairs issued after its training cutoff. The pairs come from the same truth hierarchy.
  - Per variable it reports the truth mix, `n`, MAE, the pinball losses, and the P10–P90 coverage.
  - For head/cross wind it reports the coverage achieved by the box projection along the route headings the request specifies.
- This change moves the stable surface, so the package version goes to 0.4.0 and the product schema is re-pinned.

## Consequences

- A strategy consumer can pin `/api/v1` and `QuantileBand` instead of parsing `Measured.uncertainty`.
- Head/cross-wind bands are wider than a true joint 80 % interval would be. That is deliberate: under-coverage on head-wind would directly overstate the energy budget. A joint model (copula or bivariate quantiles) can tighten them later through a new ADR.
- Until ML-01 has race-relevant data, `ghi` models train on BoM where it exists. Verification shows the mix, so a BoM-trained score is never presented as an on-board one.
- Adding variables to `ML_VARIABLES` does not create models. Each one needs `python -m rwi.ml retrain`, and until then the endpoint returns `no_model`.

## Alternatives considered

- **Bands only in `api/routers/ml.py`.** Rejected: the band is then not a contract, and every consumer re-derives it differently.
- **Post-process head/cross wind directly.** Rejected: both depend on the route heading, which is not a property of the grid point. A model would have to be trained per heading, or learn the heading as a feature from too few rows.
- **Corner projection with `interval_probability=0.8`.** Rejected: it overstates confidence. No joint distribution supports 80 %.
- **Use Airmar wind as truth now.** Rejected: it is sensor-height wind, see §3.
- **Add BSRN now.** Deferred: it is a new external source and needs its own ADR.

## Status

Accepted. Implemented in the Phase 2 commit, together with the 0.4.0 product schema.
