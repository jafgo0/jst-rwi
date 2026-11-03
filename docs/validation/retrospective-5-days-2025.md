# Five-day retrospective: PV model vs 2025 race data (2026-11-03)

Follow-up to `first-pv-validation.md`. Expanded from one 4-hour window to all
five race days, with a critical fix: the vehicle's heading is now used to
convert world sun azimuth to vehicle-frame azimuth before indexing the shadow
matrix. The matrix is defined in the vehicle frame; the previous validation
indexed it as if the car always faced north.

## Totals across all days

| Configuration            | Hours | Modelled | Measured | MAE    | Bias   | Ratio   |
|--------------------------|-------|----------|----------|--------|--------|---------|
| Naked DEFAULT_ARRAY      | 13    |   518 W  |   526 W  | 524 W  | −7 W   | 0.99×   |
| With shadow matrix       | 13    |   241 W  |   526 W  | 457 W  | −284 W | 0.46×   |

## What this reverses from the previous report

The first report (one 4-hour window on day 1) said the matrix closed a 2.49×
gap to 0.98×. That is still true for those four hours. But across 13 hours of
real driving the matrix now UNDERESTIMATES by 54 % on average, and naked
DEFAULT_ARRAY lands at 99 %.

Three possible explanations, in order of likelihood:

1. **The matrix was generated for the 2027 array (338 cells, 5 MPPT groups)
   but we are validating against 2025 data (fewer cells, 4 groups)**. The
   file we loaded has 4 groups so our contract accepts it, but the physical
   geometry may be the 2027 car.

2. **Our vehicle-frame azimuth convention may be wrong by a sign or a
   rotation**. `sun_az_vehicle = (sun_az_world - heading) % 360` is one
   plausible convention; the matrix might expect the opposite rotation or
   an offset.

3. **Hour-level aggregation is too coarse** - the car switches between
   driving, stopping and recharging within an hour; our model treats each
   hour as uniform irradiation on a uniformly-oriented panel.

## What stays true

* The arithmetic chain (GHI → POA → T_cell → DC power) is correct. It
  produces sensible numbers at the right scale.
* MAE is dominated by hour-to-hour variance, not by systematic drift. 524 W
  MAE on a 526 W mean means roughly ±100 % on any one hour, which reflects
  how much a cambered car's power varies with heading and shadow.
* The validation framework now exists as a reusable library
  (`rwi.validation`). Future reruns are one function call.

## What the next step is

Not more code. **A conversation with the team**: which version of the car
and panel does `efficiency_matrix.mat` describe? What azimuth convention
does their own simulator use? Without those answers, any further validation
is guessing which end of the stick to grab.

## Status changes

| Row | Before | After | Why |
|-----|--------|-------|-----|
| #29 Solar Irradiance Measurement | implementing | implementing | No change; mismatch widened |
| #115 Solar Energy Production Forecasting | tested | tested | Previous bump reversed - 13-hour result does not support it |
| #114 Solar Panel Temperature Analysis | tested | tested | No change |

No competency moves to "validated against real data". The honest status is
"we have a framework and we have real data, and we have a disagreement we
cannot yet explain".
