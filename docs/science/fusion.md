# Multi-model comparison (`rwi.fusion`)

Owner: fusion. Code: `src/rwi/fusion/`. Tests: `tests/unit/test_fusion_*.py`.

The point of this module is one sentence: **when two numerical weather models disagree about
tomorrow, that disagreement is the cheapest available signal that a forecast should not be acted on
without a margin.** It measures disagreement. It does not say which model is right (that needs
verification evidence, which does not exist yet), and it does not blend models into a new forecast.

## 1. Which models the provider actually offers

`OpenMeteoProvider.fetch_forecast(..., models=...)` passes **one** model name per request to
`api.open-meteo.com/v1/forecast`; the adapter only checks the name against `^[a-z0-9_]+$`, so the
list below is what was *probed or documented*, not a closed list enforced in code.
Source of every figure: `docs/science/providers.md` sec. 1, 1.2 and 4 (live calls and the Open-Meteo
docs and `meta.json` files, 2026-11-01). Nothing in this table was re-verified for this document.

| `models=` value | Resolution | Native time step | Run cadence | Status / caveats |
|---|---|---|---|---|
| `best_match` (adapter default) | per variable and hour, the finest of ICON (2-11 km), GFS (3-25 km), IFS (9-25 km) - **no single resolution** | hourly output | per source: ICON 3 h, GFS 1 h, IFS/AIFS 6 h | A *blend*; `model_init_time_utc` is always `None`; which model supplied a value is not reported. **Not a model in its own right, so not a meaningful comparison member against its own ingredients.** |
| `ecmwf_ifs` | 9 km | hourly | 6 h (`meta.json`) | `uv_index` returns null |
| `ecmwf_ifs025` | 25 km | **3-hourly, interpolated to hourly by Open-Meteo** | 6 h (`update_interval_seconds=21600`) | `uv_index`, `visibility` null |
| `ecmwf_aifs025_single` | 25 km | **6-hourly** natively | 6 h | AI model; `wind_gusts_10m`, `precipitation_probability`, `uv_index`, `visibility` null |
| `icon_global` | not recorded as verified for the single model | not recorded | not recorded (the `best_match` row quotes ICON at 3 h) | name accepted and probed (Darwin, 48 h); `uv_index`, `visibility` null |
| `gfs_global` | not recorded as verified for the single model | not recorded | not recorded (the `best_match` row quotes GFS at 1 h) | name accepted and probed; `soil_temperature_0cm` null |
| `bom_access_global` | 15 km (docs table) | - | **dead**: last run 2025-06-26, every value null on 2026-11-01 | Do not use. |

Consequences for comparison:

* Series from different models will usually all be **hourly** (the adapter requests hourly output),
  but `ecmwf_ifs025` and `ecmwf_aifs025_single` are *interpolated by the provider* between their
  native 3 h / 6 h steps. Hourly "agreement" between an interpolated and a native series is partly
  an artefact of the interpolation. `compare_models` records this in the notes for the models whose
  native step is verified (`KNOWN_NATIVE_STEP_S`: the three ECMWF models above) and makes no claim
  for the others.
* Grid cells differ (9 km vs 25 km), so part of any disagreement is spatial representativeness,
  not model physics. The notes say so when the returned grid points differ.
* No fixture or code path here has seen `icon_global` or `gfs_global` data; their resolution and
  cadence are deliberately left unstated until someone verifies them.

## 2. Alignment rule

Models publish on different time steps. The rule (implemented in `rwi.fusion.compare._align`, stated
in the module docstring and repeated in every `ModelComparison.notes`):

1. Only **usable** values count (`WeatherSeries.series`): a missing, stale, out-of-range value is
   not a value.
2. Candidate comparison times are the distinct valid times of all models. In time order, the
   earliest not-yet-assigned time is a *reference time*. Each model contributes its value closest to
   the reference time within `+tolerance_s` (default **300 s**). The point is stamped with the reference
   time. 300 s is a convention: Open-Meteo stamps on the hour, so it only absorbs sub-hourly feeds
   and clock noise and is far below any model step.
3. A model with no value in the window is **absent** from that point. Nothing is interpolated,
   resampled, forward-filled or substituted. (Because nothing is interpolated, there is nothing to
   record about interpolation except that none happened; the notes say so.)
4. A disagreement exists only where **two or more** models are present. Hourly and 3-hourly models
   are therefore compared at the 3-hourly instants they share; at the hours between, only one
   model exists and no comparison is produced.
5. The notes record: points compared, how many had all models, which models were absent how often,
   any non-zero timestamp offset used (largest offset), windows holding more than one value, models
   with no data, provider-interpolated native steps, differing grid points and differing/unknown
   model runs.

**One model is not agreement.** A single usable model produces an empty `disagreements` list and a
note saying it is not comparable and that the absence of a spread must not be read as certainty.
Two models that give identical values produce a real spread of `0.0` (a comparison that found
agreement). The two cases are different objects and are tested separately.

Other rules enforced on the inputs: all series must be the same `DataKind` and that kind must be
`FORECAST` or `ANALYSIS` (observations and clear-sky bounds cannot "disagree" as models); series must
describe the same point (within 50 km, which covers two neighbouring 25 km grid cells); an unknown
variable raises `UnknownVariableError`.

**Statistics.** Per comparison point: `spread` = max - min; `standard_deviation` = *population*
standard deviation (ddof = 0; the models present are the whole population, not a sample of one);
`mean`; `median`; `is_significant = spread > threshold` (strictly greater). Values are
ordered by model name, so dict order of the input never changes output. **Wind direction is
circular**: the spread is the smallest arc containing every direction (350 and 10 degrees are 20
apart), the mean is the vector mean, the standard deviation is the root mean square of angular
deviations from it; if the directions cancel exactly the mean is undefined and a provenance note says
the reported value is a placeholder.

## 3. Significance thresholds

> **These are engineering judgements, not physics.** No law says a 2 degC spread is "significant".
> Each value is the point at which a race strategist should stop trusting one model's number and
> hold a margin, chosen from what the quantity does to the car. They have **not** been tuned against
> verification data (there is none) and must be revisited when `rwi.verification` produces real
> spread/error statistics. They live in `rwi.fusion.thresholds` (`DEFAULT_SIGNIFICANCE_THRESHOLDS`,
> `THRESHOLD_BASIS`), are read-only, and are overridable per call with
> `significance_thresholds={variable: value}`. A variable with neither a default nor an override raises
> `ValueError`: a threshold is never invented.

Arithmetic quoted below uses stated, illustrative assumptions (1000 W/m2 reference irradiance,
25 m/s cruise, -0.3 %/K cell coefficient), none of which is a measured property of the team's car.

| Variable | Default spread | Race-relevant justification |
|---|---|---|
| `ghi` | 150 W/m2 | Array output is close to linear in irradiance. Against a 1000 W/m2 clear-sky midday reference, 150 W/m2 is about 15 % of midday array output - several times the "few percent" that would change an hourly energy estimate enough to matter. Set well above the tens of W/m2 by which healthy models routinely differ, so a flag means "hold a margin", not "any difference". |
| `dni` | 200 W/m2 | More volatile than GHI (thin cloud removes it faster); about 20 % of the reference. A near-flat array receives only the cosine-projected part, so it matters less directly than GHI. |
| `cloud_cover` | 30 percent points | Dominant driver of irradiance but strongly non-linear (little loss below roughly half cover, a great deal above roughly 70 %; recalled from the empirical cloud-to-irradiance literature, not re-derived here). 30 points at the cloudy end separates "sunny day" from "overcast day"; at the clear end it changes almost nothing, so the threshold is a blunt screen. Models also define total cloud differently. |
| `temperature_2m` | 2 degC | The *direct* effect is small: -0.3 %/K gives ~0.6 % cell power for 2 K, and ~0.7 % air density (drag). Below the "few percent" level. Kept as a diagnostic: in a dry outback airmass a 2 K disagreement usually means the models differ on cloud, a front or a wind change, which do matter. |
| `wind_speed_10m` | 3 m/s | Drag scales with airspeed squared. At 25 m/s, a 3 m/s along-track difference changes airspeed by 12 % and drag by ~25 %. Only part of a 10 m wind spread lies along the track and the car sees wind near 1 m height, so 3 m/s is lenient; a tighter override is defensible for headwind-sensitive legs. |
| `wind_gusts_10m` | 5 m/s | Gusts matter for handling and crosswind stability more than for mean energy. Models parameterise gusts and differ more on them than on mean wind. |
| `wind_direction_10m` | 45 deg (circular) | Along-track component goes with the cosine of the wind-heading angle; 45 degrees can turn a crosswind into a quartering headwind. Meaningless in light wind and not conditioned on speed in this phase: read together with `wind_speed_10m`. |
| `precipitation` | 1 mm per provider step (hourly from the adapter) | Rain removes direct irradiance and wets the road; models differ mostly on whether and where it rains. 1 mm separates "dry" from "measurable rain" in at least one model. Does not transfer to other step lengths. |

Each value carries its status string `"engineering judgement - not derived from verification data"`,
which also appears in every `ModelDisagreement.provenance.notes`.

## 4. Model selection: why "no preference" is the correct answer today

`preferred_model(scores, *, variable, lead_time_s) -> (name | None, reason)` returns a name **only**
when a score for exactly that variable and lead-time bucket (half-open `[lower, upper)`)

1. `is_meaningful` (currently 30 or more samples, from `VerificationScore`), **and**
2. beats persistence: `skill_vs_persistence > 0`.

Otherwise `(None, reason)` with a reason that says what is missing (no scores at all; no score for
this variable; no bucket covering this lead time, listing the buckets that exist; sample counts too
low, listing them per model; skill versus persistence per model if none is positive or if the
baseline is unavailable). There is no fallback to the first model, alphabetical order, a default
model, or the best MAE on a few samples - a model with nine lucky samples never outranks one with
three hundred verified ones, and an exact tie on skill and MAE gives `None` because the order of the
names is not evidence.

Why this is correct today: the archive and verification pipeline have no history, so *no model has
earned preference*, and the master specification forbids calling a model better without benchmark
evidence. Persistence is the bar because a model that cannot beat "nothing changes" has not earned
its place.

When several models qualify, the highest `skill_vs_persistence` wins (it is relative to each model's
own sample, so it is comparable across models verified over different periods, where raw MAE is not).
There is no significance test between models available from the scores, and the reason text says
so. If only one model has earned evidence the reason states that this is not proof that it is better
than the others, only that the others have shown nothing yet. A preference applies to one variable and
one lead-time bucket. Model identity is `VerificationScore.model_name` (or `provider` when absent).

`compare_models` fills `ModelComparison.preferred_model` / `preference_basis` only when the caller
passes `verification_scores` and `lead_time_s`; otherwise `preferred_model` is `None` and the basis
says why. A preferred name that is not among the compared models is discarded with an explanation.

## 5. Weights (for later; not applied)

`model_weights(scores, *, variable, lead_time_s, candidate_models=None) -> ModelWeights` gives
`w_i = (1/MAE_i) / sum_j (1/MAE_j)` over the models whose matching score is meaningful **and** beats
persistence (so a model that cannot beat persistence carries no weight). Candidates with no earned
evidence get weight `0.0` when others have evidence; a zero-MAE model takes the weight (shared among
such models) rather than dividing by zero. The weights sum to exactly 1.0. With no earned evidence the
result is **equal** weights, `basis="equal"`, and a note that equal weights mean "no evidence", not
"equally good", and are not a recommendation to blend.

Inverse MAE is chosen as the simplest defensible scheme: monotone in error, no tuning parameters,
easy to explain. Known limits: it ignores correlation between models' errors (models sharing a parent
system are less independent than their weights imply) and may compare MAEs from different
verification periods.

**What they are for:** showing the UI how the verification evidence is distributed, and giving a
later, separately validated phase a starting point. **No blended forecast is produced in this phase.**
Blending models whose skill is unknown is exactly the kind of unjustified sophistication the
specification warns against: it would put a new number, with no provenance of skill, in front of the
strategy layer, hiding the very disagreement this module exists to expose.

## 6. Validation status

Vocabulary: not started / researching / implementing / implemented, not validated / tested / validated against real data / operationally ready.

* `compare_models`: **tested** (synthetic series only)
* `disagreements`: **tested** (synthetic series only)
* Alignment rule (`rwi.fusion.compare._align`): **tested**
* Default significance thresholds (`rwi.fusion.thresholds`): **implemented, not validated** (judgements; no verification data to tune against)
* `preferred_model`: **tested** (evidence rule; no real verification history exists)
* `model_weights`: **tested**
* Real-data behaviour (two live Open-Meteo models over the route): **not started**

Nothing here has been run on real multi-model data; "tested" means unit tests on hand-built
`WeatherSeries` and `VerificationScore` objects.
