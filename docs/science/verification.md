# Forecast verification (`rwi.verification`)

Owner: verification. Code: `src/rwi/verification/` (`metrics.py`, `calibration.py`, `report.py`).
Tests: `tests/unit/test_verification_*.py`. Contract: `docs/CONTRACTS.md` section 3b.

**Status of the evidence: there is none yet.** This module is the machinery that will produce
evidence once `rwi.archive` has accumulated forecast/observation pairs. Every number it can produce
today comes from hand-built unit-test pairs. No function here is "validated against real data", and no
model may be called better than another on the strength of this module until a report with
`has_evidence == True` exists for real archived data.

Conventions: times are aware UTC; units are the canonical units of `rwi.core.variables`; the error of
a pair is always `e = forecast - observation` (positive means the forecast was too high).

---

## 1. Metrics

For one group of `n` pairs (one variable, one provider, one model, one lead-time bucket) with errors
`e_i`, forecasts `f_i` and observations `o_i`. All sums use `math.fsum` and the pairs are put in a
canonical order first, so the result does not depend on the order the archive returned them in.

| Metric | Formula | Unit | Notes |
|---|---|---|---|
| `sample_count` | `n` | pairs | Always reported. |
| `mean_absolute_error` | `MAE = (1/n) * sum |e_i|` | variable unit | Robust, easy to read. |
| `root_mean_square_error` | `RMSE = sqrt((1/n) * sum e_i^2)` | variable unit | Penalises large misses; `RMSE >= MAE`. |
| `bias` | `(1/n) * sum e_i`, i.e. mean forecast minus mean observation | variable unit | Positive: model too high. Computed as the mean of errors (identical in exact arithmetic to `mean(f) - mean(o)`, with less rounding). |
| `mean_forecast`, `mean_observed` | `mean(f_i)`, `mean(o_i)` | variable unit | Vector mean for circular variables (section 3). |
| `persistence_mae` | section 4 | variable unit | `None` when it cannot be built honestly. |
| `skill_vs_persistence` | `1 - MAE / persistence_MAE` (section 5) | dimensionless | `None` when undefined. |
| `period` | first to last valid time among the scored pairs | UTC | End is the last valid time itself, not one step beyond. |

Exact-value check used in the tests: errors `[+2, -2, +4, -4]` give `MAE = 3.0`,
`RMSE = sqrt(10) = 3.1622776601683795`, `bias = 0.0`, asserted with `==`.

Pairs with NaN/inf values are ignored (and counted in `verify` notes). Pairs of other variables are
ignored. The requested `unit` must equal the registry unit or `score_pairs` raises.

## 2. Lead-time buckets

Lead time is valid time minus the model **initialisation** time (what skill degrades with), not the
time since we downloaded the forecast. Buckets are half-open `[lower, upper)` in seconds, so a pair
exactly on a boundary falls in the **upper** bucket (3 h is in 3-6 h, not 0-3 h). A lead time at or
beyond the last upper bound (120 h) is not scored and `verify` says how many were dropped.

| Bucket | Why |
|---|---|
| 0-3 h | Nowcast range. Dominated by initial-state error and spin-up; the regime where persistence is hardest to beat. |
| 3-6 h | Still short enough that the latest observed state carries much of the information. |
| 6-12 h | Same-day planning, where the first full model cycle after a morning run applies. |
| 12-24 h | The "tomorrow" forecast; diurnal-cycle timing errors (cloud onset, sea breeze) dominate. |
| 24-48 h | Day-ahead to two-day-ahead; synoptic-scale errors begin to dominate. |
| 48-120 h | Medium range. Wide, because pair counts are small and error grows slowly relative to bucket width. |

These are a **proposal** based on the usual shape of NWP error growth (fast initially, slower later),
widening geometrically so each bucket holds a useful number of pairs. They are not tuned on data; if
real archive coverage is thin, pass coarser `lead_time_buckets_s`. Custom buckets must satisfy
`0 <= lower < upper` and must not overlap.

## 3. Circular variables

A variable is treated as circular when the registry says its unit is `deg` and it is a direction
(today only `wind_direction_10m`; `is_circular_variable`). For these:

* error: `e = wrap(f - o)` with `wrap(x) = ((x + 180) mod 360) - 180`, then `-180` mapped to `+180`,
  giving the interval `(-180, 180]`;
* MAE, RMSE and bias use these wrapped errors (bias is the mean signed wrapped error);
* `mean_forecast`, `mean_observed` use the vector method:
  `mean = atan2(mean(sin a_i), mean(cos a_i))` in degrees, mod 360, in `[0, 360)`. The arithmetic
  mean is never used;
* if the mean resultant vector length is below 1e-9 (e.g. directions 0 and 180) the mean direction does
  not exist and the field is `None`.

**Worked example.** Forecast 355 deg, observed 5 deg. Linear subtraction gives `355 - 5 = 350`, which
would put this near-perfect forecast among the worst in the archive. The wrapped difference is
`((350 + 180) mod 360) - 180 = 170 - 180 = -10`: the forecast is 10 deg anticlockwise of the
observation. `MAE = 10`, `bias = -10`. The reverse (forecast 5, observed 355) gives `+10`. Mean of
350 and 10 deg: arithmetic gives 180 (due south, the opposite of the truth); the vector mean is 0
(north). These are asserted in `test_verification_metrics.py` (`@pytest.mark.science`).

Wind **speed** is an ordinary linear variable; scoring a direction error while the wind is near calm is
not meaningful and is not filtered here (a known limitation, see section 9).

## 4. Persistence baseline

`persistence_baseline(pairs, *, observation_history=None, tolerance_s=1800) -> float | None`

Persistence is the trivial forecast "the value stays what it was when the forecast was issued". For
each pair the persistence forecast is the **observation at the forecast's initialisation time**,
`valid_time - lead_time_s`. The baseline is the MAE of that forecast against the same pair's observed
value (circular differences for circular variables).

**Data it needs.** The contract's `ForecastObservationPair` carries no previous observation, so the
reference is taken from one of two sources, in this order:

1. `observation_history`: an explicit `(time, value)` series for one station, if the caller supplies it;
2. otherwise the observations carried by the pairs themselves: each pair contributes `observed_value`
   at `valid_time + time_offset_s`, keyed by station (observation point rounded to 0.001 deg), pooled
   across all providers and models of the same variable (observations do not depend on the model).
   Duplicate observations at one station and time are averaged (vector mean for circular).

Rules that keep the baseline honest:

* the reference must be within `tolerance_s` (default 1800 s) of the init time, nearest wins;
* the reference must be strictly **earlier** than the pair's own observation (a lead of 0 would
  otherwise forecast a value with itself);
* pairs without a reference are excluded, and the **model's MAE used in the skill score is recomputed
  on exactly the same matched pairs**, so the comparison is like for like (the stored
  `mean_absolute_error` still covers all pairs in the group);
* the baseline is `None` unless at least 2 pairs **and** at least half of the group have a reference.
  A baseline from a handful of lucky matches would flatter or damn the model arbitrarily.

With sparse archives (observations only at the valid times of the stored forecasts) references often
do not exist for long leads, so `persistence_mae` and the skill will be `None` for those buckets. That
is the intended behaviour: the score reports "unknown", not a number.

## 5. Skill score and sign convention

`skill_vs_persistence = 1 - MAE_model / MAE_persistence` (both on the matched pairs).

* **positive**: better than persistence (`+1` is a perfect forecast, `+0.5` halves the error);
* **zero**: no better than persistence;
* **negative**: worse than persistence (`-1` means twice the persistence error).

A model that cannot beat persistence has not earned its place; `verify` writes a note for every
bucket with `skill <= 0`. If `MAE_persistence == 0` (observations never changed over the matched
pairs) the ratio is undefined and the skill is `None`, never infinity.

## 6. `is_meaningful` and the sample-count threshold

`VerificationScore.is_meaningful` is `sample_count >= 30`. Thirty is a **convention**, borrowed from
the folk rule that the central limit theorem "kicks in" around 30; it is not a theorem. Weather errors
are autocorrelated (consecutive hours of one forecast run share their error), so 30 pairs may carry
the information of far fewer independent samples, and 30 does nothing about heavy tails. It is a
floor below which a score is certainly not evidence, not a certificate above it. The score also counts
all pairs in the bucket even where persistence matched only some of them.

## 7. Bias correction: what it may and may not do

`bias_correction(scores, *, variable, provider, model_name, lead_time_s) -> float | None` returns
`-bias` of the one matching score (exact variable, provider and model name; `lead_time_s` inside the
half-open bucket) **only if** that score `is_meaningful`; otherwise `None`. A model 1.5 degC too warm
(bias `+1.5`) gets `-1.5`. If several meaningful scores cover the lead time (custom overlapping runs)
the one with the most samples is used.

`apply_bias_correction(series, scores) -> WeatherSeries` returns a new series. It may:

* correct only `DataKind.FORECAST` series, only usable values, and only variables that are not
  discrete state codes (`is_day`, `weather_code`);
* use the lead time `valid_time - model_init_time` (series level, else the value's provenance);
* correct each value additively, then clip to the registry range, noting the clip;
* wrap circular variables into `[0, 360)`, noting the wrap.

It records `bias_corrected(-0.8 degC, n=214, ecmwf_ifs025, 6-12h)` (plus any
`clipped_to_registry_range(...)` or `wrapped_to_0_360`) in `provenance.processing`, appended to what
was already there, and the raw value in `provenance.notes`. A value already carrying the marker is not
corrected again.

It may **not**:

* correct anything without a meaningful matching score, or with an unknown lead time (value passes
  through untouched);
* create irradiance or precipitation from nothing: a radiation or precipitation value already at its
  lower bound (night, dry) is left alone, because an additive mean bias, computed over day and night,
  would otherwise add light at midnight;
* produce a value outside the registry range (negative irradiance or wind speed, humidity above 100 %);
* correct observations, or pretend to know why the bias exists.

Known limits: the correction is a single additive number per bucket. It does not model dependence on
time of day, weather regime or value size (a multiplicative bias in irradiance is common and is
**not** handled), it was computed at the archive's stations and may not transfer to the route, and a
correction derived from the same pairs it is then evaluated on proves nothing. Evaluate any correction
on a later, separate period.

## 8. Leakage rules this module relies on the archive to enforce

`rwi.verification` takes pairs as given and cannot detect these by itself (apart from the one check
`ForecastObservationPair` makes in its validator):

1. a forecast is scored only if it was **recorded before its valid time** (validator-enforced, and
   the archive must not produce pairs that would violate it);
2. a recorded forecast is **immutable**; a later run for the same valid time is a new row, so
   verification never scores a forecast that was revised after seeing the truth;
3. `lead_time_s` is measured from model **initialisation**, not from retrieval time;
4. each (provider, model, init time, valid time, station) appears **once**; duplicates would inflate
   `sample_count` and therefore `is_meaningful`;
5. pairs are matched within the archive's separation and time-offset tolerances; looser tolerances mix
   representativeness error into the forecast error;
6. observations are quality-controlled (`rwi.quality`) before pairing, and are never themselves
   model output (analyses and reanalyses are not truth);
7. bias corrections must be fitted on pairs from a period before the period they are evaluated on.

The persistence baseline uses only observations at or before the forecast init time, so it does not
leak either; `score_pairs` and `verify` never look at any value later than a pair's own valid time
except that pair's own observation, which is the thing being predicted.

## 9. Known limitations

* Deterministic scores only; no probabilistic scores (CRPS, reliability) and no confidence intervals.
* Precipitation and irradiance are zero-inflated; MAE/bias over all hours understates daytime or
  wet-hour errors. Stratifying by day/night or by event is future work.
* Wind-direction error is scored even when the wind is near calm and direction is poorly defined.
* Sample counts are not de-correlated; see section 6.
* Persistence needs observations at init times; with sparse archives it is often `None`.

## 10. Validation status per function

Allowed values: not started / researching / implementing / implemented, not validated / tested /
validated against real data / operationally ready. "tested" means unit-tested against hand-computed
values only. There is no verification history yet, so nothing is validated against real data.

| Function | Status |
|---|---|
| `score_pairs` | tested |
| `persistence_baseline` | tested |
| `skill_score` | tested |
| `circular_difference`, `circular_mean`, `is_circular_variable` | tested |
| `verify` | tested |
| `bias_correction` | tested |
| `apply_bias_correction` | tested |
| Default lead-time buckets (section 2) | implemented, not validated |
| `is_meaningful` threshold of 30 (section 6) | implemented, not validated |

## 11. CRPS and pinball loss

Added for ADR 0008 (probabilistic ML post-processing). Gneiting & Raftery (2007),
JASA 102, 359–378.

- `pinball_loss(y, q, τ)` = τ·(y−q) if y ≥ q, else (1−τ)·(q−y).
- `crps_quantiles(y, q, τ)` = (2/K)·Σ pinball over K quantile levels. With only
  P10/P50/P90 it is a coarse approximation of the integral CRPS. All systems in a
  comparison are therefore scored on the **same three levels**. A point forecast
  scored as three equal quantiles has CRPS = |error|, so its CRPS equals its MAE.
- `crps_ensemble(y, x)` is the energy form: mean|x−y| − ½·mean|xᵢ−xⱼ|. With one
  member it equals |error|.

| Function | Status |
|---|---|
| `pinball_loss` | tested |
| `crps_quantiles` | tested; science test: N(0,1) at y=0 → 0.2337 |
| `crps_ensemble` | tested against brute force and the same science reference |
