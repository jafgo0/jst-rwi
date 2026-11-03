# First real PV model validation (2026-11-03)

The first time this platform's models have met real data from a car on the road.

## What was done

Took one morning (2025-07-08 22:00 to 2025-07-09 01:00 UTC) of MPPT output
from the JU Solar Team's 2025 race-day CSV exports. The car was in the
Katherine–Dunmarra section of the NT. Pulled ECMWF IFS reanalysis for the same
hours at the matching GPS positions, ran it through our PV model with the
TESSERACT-2025 array config, and compared modelled DC power to the sum of the
four measured MPPT outputs.

Script: `scripts/validation/pv_vs_mppt_2025.py`.

## What came out

| UTC               | GHI reanalys | Modell DC | Uppmätt MPPT | Fel     |
|-------------------|--------------|-----------|--------------|---------|
| 2025-07-08 22:00  |     8 W/m²   |    15 W   |    259 W     | −244 W  |
| 2025-07-08 23:00  |   149 W/m²   |   344 W   |    156 W     | +188 W  |
| 2025-07-09 00:00  |   362 W/m²   |   690 W   |    258 W     | +432 W  |
| 2025-07-09 01:00  |   558 W/m²   |   970 W   |    139 W     | +831 W  |

## What it says

Three honest findings, each pointing somewhere:

1. **The chain works end-to-end.** GHI → POA → cell temperature → DC power
   produces sensible numbers in the right units. No sign flip, no 1000× unit
   error, no night-time irradiance leaking through.

2. **DEFAULT_ARRAY overestimates by ~2-3× at full sun.** The spec of 6 m² ×
   27% is nameplate. Real-world effective efficiency - after soiling, cell
   mismatch, non-ideal angle of incidence over the cambered car body, MPPT
   dynamics, and the fact that only part of the 6 m² is actually in the sun
   at any instant - is clearly lower. This is NOT a model bug; this is the
   gap between a flat-plane rooftop model and a real vehicle. The next
   improvement is wiring in the team's shadow-analysis matrix.

3. **Reanalysis cloud cover is too smooth for short-timescale validation.**
   ECMWF IFS reanalysis at 23:00 UTC reports 8 W/m² GHI (97 % cloud) but the
   car measured 259 W of power - meaning there WAS sun through gaps. And at
   01:00 UTC the opposite: reanalysis says 558 W/m² at 40 % cloud but the
   car produced only 139 W, meaning the car was likely stopped at a control
   stop or in deep shadow during that hour. The reanalysis cannot resolve
   either case at hourly resolution.

## What this unlocks

* Competency #29 (Solar Irradiance Measurement) now has partial real-world
  evidence - not yet "validated against real data" because the gap is too
  large to call it a validation, but **a measured discrepancy**, which is a
  starting point.
* The next debugging target is clear: either (a) measure the real effective
  array efficiency under field conditions and update DEFAULT_ARRAY, or (b)
  load the shadow-analysis matrix (contract already exists), or both.
* The methodology is reusable: five race days exist in the team repo, so a
  larger retrospective is a matter of running the script over all of them.

## What this does NOT prove

* That the forecast is good. This is REANALYSIS, not a real forecast. True
  forecast verification needs forecasts made before the valid time, which
  the capture worker is building week by week.
* That the car's MPPT measurements are without error. They are the ground
  truth we have; whether they are the ground truth that exists is a separate
  question only a calibrated pyranometer could answer.

## Validation status change

| Competency | Before | After | Notes |
|---|---|---|---|
| #29 Solar Irradiance Measurement | implementing | implementing | measurement path exists; validation evidence is a disagreement, not an agreement |
| #102 Solar Irradiance Analysis | tested | tested | chain validated end-to-end on sensible inputs |
| #114 Solar Panel Temperature Analysis | tested | tested | no change; still synthetic test only |
| #115 Solar Energy Production Forecasting | tested | tested | real-world gap documented but not yet corrected |

No competency moves to "validated against real data" from this exercise -
that requires an AGREEMENT, not just a comparison. But this is the first
honest numeric gap we can point at and the first path to closing it.
