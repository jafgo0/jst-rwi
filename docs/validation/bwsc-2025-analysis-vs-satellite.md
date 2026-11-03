# ECMWF reanalysis vs NASA POWER along the BWSC-2025 race route (2026-11-03)

A meteorological validation of what our live forecast pipeline reaches for
in `fetch_analysis`, measured against an independent satellite-derived GHI
product over BWSC-2025 week.

Not a PV validation. Not a strategy validation. This asks one question:
how well does ECMWF-IFS reanalysis match reality on the Darwin-Adelaide
corridor in late August?

## What was done

For each of six route points, pulled hourly GHI over 2025-08-24 to
2025-08-30 (BWSC-2025 race week) from two independent sources:

* ECMWF-IFS reanalysis via Open-Meteo's `archive-api` (our live pipeline's
  `fetch_analysis`).
* NASA POWER (CERES SYN1deg) via `power.larc.nasa.gov`. Satellite-derived,
  independent of ECMWF.

Then built `ForecastObservationPair` objects with ECMWF as forecast and
NASA as observation, and ran `rwi.verification.score_pairs`.

## What came out

| Point          | n     | ECMWF mean | NASA mean | bias   | MAE    | RMSE   |
|----------------|-------|-----------:|----------:|-------:|-------:|-------:|
| Darwin         |  168  |     253.2  |    241.8  | +11.4  |  71.3  | 112.0  |
| Katherine      |  168  |     261.3  |    255.0  |  +6.3  |  75.8  | 117.7  |
| Tennant Creek  |  168  |     253.7  |    252.5  |  +1.1  |  77.6  | 120.0  |
| Alice Springs  |  168  |     249.9  |    256.5  |  -6.6  |  70.7  | 109.5  |
| Coober Pedy    |  168  |     174.4  |    188.0  | -13.6  |  55.7  |  90.1  |
| Adelaide       |  168  |     118.4  |    135.8  | -17.4  |  55.3  |  93.9  |
| **Pooled**     | **1008** | **218.5** | **221.6** | **-3.1** | **67.7** | **107.8** |

All numbers W/m^2.

## What it means

1. **Pooled bias is tiny (-3 W/m^2 on a 220 W/m^2 mean)**. ECMWF-IFS is
   well calibrated against the satellite product for the race corridor in
   late winter conditions.
2. **Hour-level noise is large: MAE is 30 % of the mean**. Any single hour
   can disagree by half of the mean irradiance. That is the baseline our
   forecasts will never beat.
3. **A north-south trend exists**: ECMWF reads +11 W/m^2 higher than NASA
   in the tropics (Darwin), -17 W/m^2 lower in Adelaide. The sign of the
   bias flips around Tennant Creek. Consistent with a cloud-fraction bias
   that is small on bright tropical days and larger when stratocumulus
   dominates the southern half.
4. **RMSE 108 W/m^2 is the lower bound on our GHI forecast error over
   this region**. Any live ECMWF-IFS forecast will inherit at least this
   much hour-to-hour uncertainty from the model's analysis step.

## Triangulation against a BoM-anchored product (added 2026-11-03)

NASA POWER is itself gridded (CERES). A single independent reference
could carry a correlated bias. SILO (Queensland Government's data drill)
is a scriptable gridded product interpolated from BoM stations with a
satellite fallback; its radiation grid is BoM-anchored over most of
Australia. Pulling daily GHI from SILO at the same six points lets us
triangulate.

| Point          | ECMWF | NASA | SILO (BoM) | ECMWF - SILO | NASA - SILO |
|----------------|------:|-----:|-----------:|-------------:|------------:|
| Darwin         | 253.2 | 241.8|      267.7 |        -14.5 |       -25.9 |
| Katherine      | 261.3 | 255.0|      260.6 |         +0.7 |        -5.6 |
| Tennant Creek  | 253.7 | 252.5|      251.5 |         +2.2 |        +1.0 |
| Alice Springs  | 249.9 | 256.5|      233.1 |        +16.7 |       +23.3 |
| Coober Pedy    | 174.4 | 188.0|      193.1 |        -18.7 |        -5.1 |
| Adelaide       | 118.4 | 135.8|      126.5 |         -8.0 |        +9.3 |

What triangulation shows:

* The three products agree closely at Katherine and Tennant Creek
  (all within +/- 6 W/m^2 of each other). The middle of the route
  is a benign case.
* At **Darwin** ECMWF and NASA POWER *both* read LOW compared to
  BoM-anchored SILO (-15 and -26 W/m^2). This is a model under-estimate
  of tropical GHI that the NASA-POWER-only check could not have seen;
  POWER and ECMWF happen to share the same direction of error here.
* At **Alice Springs** ECMWF and NASA POWER *both* read HIGH compared
  to BoM-anchored SILO (+17, +23 W/m^2). Flips the sign of the ECMWF
  bias relative to what POWER-alone suggested, and raises the plausible
  high-end error.
* Nowhere does any single pair of products contradict all other pairs,
  so this is noise and model skill, not one bad grid.

So the pooled -3 W/m^2 ECMWF-vs-POWER agreement is not evidence that
ECMWF is well calibrated against reality; it is evidence that two
gridded products agree in the mean. Compared to BoM-anchored SILO the
absolute bias at any one point reaches 15-25 W/m^2.

## What this does not say

* It does not measure forecast skill. Open-Meteo preserves historical
  forecast runs only ~90 days back; for 2025-08 both the archive and the
  historical-forecast endpoints return the same reanalysis values, so
  there is no way to recover what the model predicted ahead of time.
* It does not compare against raw station measurements. SILO is
  BoM-anchored but is still interpolated, not an observatory reading.
  BoM observatory 1-minute GHI (Alice Springs 015540 among others) is
  the strongest truth source but BoM Climate Data Online does not
  expose a stable scriptable URL; a one-off manual CSV download is the
  honest next step.

## What is now in the repo

`scripts/validation/bwsc_2025_analysis_vs_satellite.py` runs the whole
pipeline and prints the table above. It is a one-off exploration tool;
no part of it is on an import path from production code.

## Honest next steps

1. **BoM observatory 1-minute GHI** - download Alice Springs 015540 for
   race week once (one-off manual CSV from Climate Data Online), then
   compare both ECMWF reanalysis and NASA POWER against the measured
   station. This triangulates the error.
2. **Keep capturing live forecasts** - once our forecast archive has
   covered a full season, we can measure real forecast skill at real
   lead times against our own ingested analyses. The infrastructure
   exists; it just needs time.
