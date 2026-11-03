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

## What this does not say

* It does not measure forecast skill. Open-Meteo preserves historical
  forecast runs only ~90 days back; for 2025-08 both the archive and the
  historical-forecast endpoints return the same reanalysis values, so
  there is no way to recover what the model predicted ahead of time.
  Measuring forecast skill requires either our own captures from that
  week (we have none) or a provider that preserves older forecasts (ECMWF
  Open Data, but with coarser resolution).
* It does not compare against ground truth. NASA POWER is itself a
  gridded satellite product, not a measurement at a station. A BoM
  observatory 1-minute GHI stream (Alice Springs 015540) would be
  stronger evidence. BoM Climate Data Online does not expose a stable
  scriptable URL, so this script does not fetch it.

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
