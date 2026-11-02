# Ensembles and uncertainty (`rwi.ensemble`)

Module status as a whole: **tested** at unit level on synthetic ensembles built in
the tests. **Nothing here is validated against real data**: no ensemble forecast
has been compared with an observation yet, so none of the spread, frequency or
interval figures has a measured reliability. Source of all provider facts:
`src/rwi/providers/openmeteo.py` and `docs/science/providers.md`.

Why it exists: ensemble spread is the only uncertainty available today that comes
from model runs that were actually made, not from an error bar invented
afterwards. For solar racing "GHI 700" versus "GHI 400 to 850" changes the
strategy.

## 1. Which ensemble the provider actually gives

`OpenMeteoProvider.fetch_ensemble(point, window, variables, *, models=None) -> EnsembleForecast`
(`/v1/ensemble` on `ensemble-api.open-meteo.com`) returns one `WeatherSeries` per
member, the control first (`dataset="ensemble/control"`, then `"ensemble/memberNN"`).
It is the only ensemble fetcher in the platform; this module does not fetch.

| | `ecmwf_ifs025` (provider default) | `gfs025` |
|---|---|---|
| Open-Meteo metadata dir | `ecmwf_ifs025_ensemble` | `ncep_gefs025` |
| Members | **51** (control + 50) | 31 (control + 30) |
| Grid | 0.25 deg (25 km; `_SPATIAL_RESOLUTION_KM`) | 0.25 deg (25 km) |
| Native time step | 3-hourly, hourly output is **interpolated by Open-Meteo** | 3-hourly, same |
| Runs | every 6 h | every 6 h |
| Horizon | 15 d; adapter limit 360 h | 10 d; adapter limit 240 h |
| History | about 3 days only (`_ENSEMBLE_PAST_LIMIT`) | same |

Verification of these numbers: the 51 members of `ecmwf_ifs025` are confirmed by
counting `temperature_2m`, `temperature_2m_member01` ... `_member50` in the
captured fixture `tests/fixtures/openmeteo_ensemble_darwin_ifs025.json`. The
`gfs025` figures (31 members, 10 d) come from `docs/science/providers.md` and the
provider module; there is no `gfs025` fixture, so they are documented, not
re-measured by me. ICON-EPS (40 members) is listed in `providers.md` but the
adapter has no resolution/horizon entry for it. Variable availability differs by
model (e.g. `ecmwf_ifs025` returns null for `precipitation_probability`,
`uv_index`, `visibility`): a variable that is null for every member simply yields
no summaries.

Consequences used in this module:

* Because the hourly values between native 3-hourly steps are interpolated, the
  member spread at an off-step hour is not independent information. Spread at
  hourly resolution should not be read as hourly-scale predictability.
* The ensemble is 25 km, the deterministic forecasts it may be attached to can be
  9 km. The ensemble spread describes the coarse grid cell, not a point.
* The free-tier ensemble host has a separate, volume-weighted daily quota
  (`providers.md` sec. 1.1): fetch once, summarise many times.

## 2. Summary statistics (`summarise`)

Per valid time (exact timestamp match) using only members that have a usable
value there. `member_count` is that number, may be below the ensemble size, and
the provenance `notes` then say `member_count=12 of 30 ... 18 were missing`.
A time with no member produces no summary. Missing values are never filled.

**Percentile method: linear interpolation** (numpy `method="linear"`,
Hyndman-Fan type 7): the p-th percentile is at fractional rank `p/100 * (n-1)` in
the sorted members. With n = 31 members p10 is exactly the 4th smallest member and
p90 the 28th smallest; with n = 5 the 10th percentile of [1..5] is 1.4. With so
few members the tail percentiles are coarse and can never lie outside
[min, max]; p5/p95 would be interpolations between two order statistics. The
method is also written into every summary's `provenance.processing`. Percentile
`"50"` is set equal to the `median` exactly.

**Standard deviation: sample (ddof=1, divide by n-1).** The members are a small
sample from the underlying forecast distribution; we want an estimate of that
distribution's spread rather than the spread of the 31 numbers themselves. For
[1, 2, 3, 4, 5] this is sqrt(2.5) = 1.5811388..., not the population value
sqrt(2) = 1.4142136 (the tests assert both which is and which is not used). With
one member the value is `0.0` and the provenance notes and `spread_skill_note`
say that **one member carries no spread information; 0.0 means "unknown", not
"certain"**.

`as_uncertainty()` (in the contract) turns a summary into the p10-p90 interval,
`interval_probability = 0.8`, plus the standard deviation. Remember that 0.8 is
the *nominal* mass of the central 80 % of members, not a verified coverage.

## 3. Circular variables (wind direction)

`wind_direction_10m` is an angle on a circle. The variable registry carries no
"circular" flag (and is not owned by this module), so `rwi.ensemble._common.CIRCULAR_VARIABLES`
lists them; add any new angular variable there.

Worked example, members [350, 355, 0, 5, 10] degrees:

* arithmetic mean = (350 + 355 + 0 + 5 + 10) / 5 = **144 deg**: wrong, it is
  pointing south-east for a wind that is blowing from due north;
* vector mean: mean sin = 0, mean cos = 0.99240...; `atan2(0, 0.9924)` = **0 deg**
  (mapped into [0, 360); values within 1e-9 deg of 360 are snapped to 0);
* resultant length R = (1 + 2 cos 5 deg + 2 cos 10 deg) / 5 = 0.99240;
* circular standard deviation = sqrt(-2 ln R) = 0.12352 rad = **7.08 deg**
  (Mardia & Jupp). A linear standard deviation of these numbers would be about
  160 deg and would say the wind is unpredictable when the members agree within
  10 degrees;
* circular median = the member minimising the sum of angular distances (here 0);
* `minimum`/`maximum` = clockwise start and end of the smallest arc containing
  all members: **350 and 10**. They are not an ordering, `maximum < minimum` means
  the arc crosses north. This is documented in the provenance notes;
* `percentiles` is **empty** and the notes say why: linear percentiles across the
  wrap are wrong and no cut point is canonical. Consequently `as_uncertainty()`
  has `lower = upper = interval_probability = None` and carries only the
  circular standard deviation. We chose an empty dict over numbers that look
  fine and are not.

Limits: when members fan out around the circle (R < 0.1) the mean direction is
essentially undefined and the notes say so; R is floored at 1e-6 so the standard
deviation stays finite. The circular standard deviation has no small-sample
correction. Threshold queries ("above 180 deg") are rejected for circular
variables; `probability_in_sector` (circular distance to a centre) exists
instead.

## 4. Member frequency is not a calibrated probability

`probability_above`, `probability_below`, `probability_in_sector`,
`member_frequency_throughout` and `irradiance_stays_above` return **the fraction
of members** meeting a condition. Convention: strictly greater (`above`) or
strictly less (`below`); a member equal to the threshold is in neither. The
result is the exact ratio k/n, never rounded. No members is an error
(`EnsembleDataError`), never `0.0`.

Why this is not a probability of the event:

1. **Resolution.** With N members the values are k/N: 3.2 percentage points for
   31 members, 2.0 for 51. "0" means no member, not impossible; "1" means every
   member, not certain.
2. **Sampling noise.** The frequency from a finite ensemble has standard error of
   about sqrt(p(1-p)/N), about 9 percentage points at p = 0.5 for 31 members.
3. **Reliability is unknown.** Operational ensembles are usually under-dispersive
   (too little spread near the surface) and carry model bias, so "30 % of
   members" can correspond to a quite different real frequency. For irradiance
   the 25 km grid cannot represent individual cloud elements.
4. **Interpolated hours** (sec. 1) make the members more alike than independent
   hourly forecasts would be.

What calibration would need: a **verification history** - archived ensemble
forecasts paired with later observations at the same place over enough
cases and lead times to build a reliability diagram / a mapping from member
fraction to observed frequency (plus a bias correction). The archive and
`rwi.verification` (`score_pairs`, `verify`) are being built; there is no such
history yet. Until then the numbers must be shown with `probability_note(forecast,
variable)`, which states that this is a member frequency, how many members stand
behind it, the resolution in percentage points, and that the ensemble is
unverified. `MemberFrequency.note` does the same for window queries, and also
reports members excluded for gaps.

The racing helper `irradiance_stays_above(forecast, threshold_w_m2=500,
start_utc, end_utc)` is "fraction of members whose GHI is above 500 W/m2 at
every valid time in the window". Only members with a value at every valid time are
counted; the counts are returned. It is validated against the variable registry
(radiation class, unit W/m2) so `uv_index` is rejected.

## 5. Spread-skill caveat (`attach_uncertainty`, `spread_skill_note`)

Ensemble spread measures *disagreement between members*. It is a measure of
forecast uncertainty only to the extent that the ensemble is statistically
reliable (spread matches typical forecast error). That has **not** been checked
for this platform's locations, seasons or variables. A small spread can still
accompany a large error (shared model bias, under-dispersion), and a large spread
can be harmless. The attached `Uncertainty.method` names the ensemble model and
member count so the origin is visible; its meaning is "ensemble spread", not
"expected error".

`attach_uncertainty(series, summaries)` returns a new `WeatherSeries`; it only
sets `Measured.uncertainty`. A test compares the IEEE bit pattern of every value,
and that unit, quality and provenance are equal and that the input object is
unchanged. Matching is by variable and nearest valid time within `tolerance_s`
(default 1800 s), and the summary's point must be within `max_distance_m` (default
30 km) of the series point. No match, or a missing value: `uncertainty=None`
(not a default). Existing uncertainty is kept unless `overwrite=True`. Only
`FORECAST` series are accepted. Attaching IFS-ensemble spread to a series from
another model is allowed but is a statement about the ensemble, not that model.

## 6. Validation status

| Function | Status |
|---|---|
| `summarise` (linear variables: mean, median, min, max, sample std, linear percentiles) | tested |
| `summarise` (circular variables: vector mean, circular std, circular median, arc; no percentiles) | tested |
| `probability_above`, `probability_below` | tested |
| `probability_in_sector` | tested |
| `member_frequency_throughout`, `irradiance_stays_above` | tested |
| `probability_note`, `MemberFrequency.note` | tested |
| `attach_uncertainty` | tested |
| `spread_skill_note` | tested |
| Reliability of ensemble spread as forecast uncertainty (any function) | not started (needs verification history) |
| Calibration of member frequencies into probabilities | not started (needs verification history) |

"tested" means correctness of the arithmetic and conventions on constructed
inputs. It does not mean the ensemble is skilful.
