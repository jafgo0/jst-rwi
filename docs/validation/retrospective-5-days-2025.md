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

## Review audit of the PV chain (2026-11-08)

What has actually been checked, against the code and the 2025 race data,
and what remains open.

| Check | Status | Where |
|---|---|---|
| Timestamp matching: MPPT and reanalysis use the same UTC and the same local-time offset | **done** | `scripts/validation/pv_vs_mppt_2025.py` converts MPPT UTC to hour-bucket UTC; reanalysis hours are UTC. No local-time drift found. |
| Hour-mean vs instantaneous comparison | **documented as a limit** | The MPPT CSV is per-sample; reanalysis GHI is hourly mean. The script averages MPPT per hour before comparing. A 526 W mean on 13 hours cannot resolve sub-hour variability; "hourly aggregation is too coarse" is listed above as candidate explanation #3. |
| Heading-aware vehicle-frame azimuth | **done** | The current run uses `sun_az_vehicle = (sun_az_world - vehicle_heading) % 360` before indexing the shadow matrix. The previous run did not; that is why this retrospective reversed the first report. |
| Shadow-matrix coordinate convention (sign, axis order) | **OPEN** | The matrix index convention is not documented alongside `efficiency_matrix.mat`. Candidate explanation #2 above. |
| Vehicle/panel version coupling of the matrix | **OPEN** | The matrix file loaded has 4 MPPT groups so the Pydantic contract accepts it, but the physical geometry may belong to the 2027 car. Candidate explanation #1. |
| Night-time irradiance leak | **done** | GHI is 8 W/m² at 22:00 UTC on day 1; DC power is 15 W — no night leak into daytime. |
| Unit sanity (W vs kW, m/s vs km/h) | **done** | Order-of-magnitude on day-1 noon is 500–700 W modelled / 500 W measured; no 1000× bug. |
| Clock skew between telemetry and reanalysis | **done** (within the hourly bin) | Hour-bin alignment hides sub-hour clock skew; see hour-mean limit above. |

### Precise questions for the team

Write-once, non-urgent; the answer to any one of these unblocks the next
step of validation. Numbered so the team can reply by number.

1. **Matrix version.** Does `efficiency_matrix.mat` describe (a) the
   2025 race car (Tesseract-2025, 4 MPPT groups, X cells per group), or
   (b) the 2027 car (planned, N MPPT groups, ... cells)? If (b),
   validating against 2025 race data is comparing a 2027 model against
   a 2025 car and no further code change will close the gap.
2. **Index convention of the matrix.** For a given cell index, is axis 0
   the sun's **elevation** or its **zenith** angle? Is axis 1 the sun's
   **azimuth** or its **hour angle**? Are the azimuth zeros at **north
   clockwise**, **south clockwise**, or **east counter-clockwise**?
   What range (0..360, -180..180, 0..90)? Reference: the matrix file
   itself has no header text our loader reads.
3. **Vehicle frame orientation.** Is the vehicle's local +x axis the
   **direction of travel** (nose), or is the matrix defined relative to
   the **panel normal** which may differ from nose by a mounting offset?
4. **MPPT-to-array mapping.** The MPPT CSV has four channels. Which
   physical group on the panel does each channel feed? If the file we
   load has the groups ordered differently from the CSV, hourly totals
   still match but per-group sensitivity to shadow does not.
5. **Panel reference used by the team's own simulator.** If the team's
   own tool reports a figure for day 1 at 00:00 UTC, we can compare
   their number against ours and localise the disagreement to either
   model inputs (GHI, heading, position) or model code.

## Status changes

| Row | Before | After | Why |
|-----|--------|-------|-----|
| #29 Solar Irradiance Measurement | implementing | implementing | No change; mismatch widened |
| #115 Solar Energy Production Forecasting | tested | tested | Previous bump reversed - 13-hour result does not support it |
| #114 Solar Panel Temperature Analysis | tested | tested | No change |

No competency moves to "validated against real data". The honest status is
"we have a framework and we have real data, and we have a disagreement we
cannot yet explain".
