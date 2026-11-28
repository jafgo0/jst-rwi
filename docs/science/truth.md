# Ground truth: BSRN Alice Springs and MERRA-2 (ADR 0017)

Offline comparisons that tell us how wrong the platform's irradiance inputs are at one
route point in August. Nothing here corrects anything yet.

## 1. Sources

| Source | Kind | What | Module |
|---|---|---|---|
| BSRN Alice Springs (`asp`) | OBSERVATION | 1-min GHI, DNI, DHI (LR0100), station-to-archive files via FTP | `rwi.providers.bsrn` |
| MERRA-2 M2T1NXRAD `SWGDN` | ANALYSIS | 1-h mean all-sky GHI at the 0.5° × 0.625° cell | `rwi.providers.merra2` |
| ERA5 via Open-Meteo | ANALYSIS | 1-h GHI at the 0.25° cell (as in ADR 0016) | `rwi.providers.open_meteo` |

BSRN files may not be redistributed; they stay under `RWI_TRUTH_DIR` (default
`data/truth/`, git-ignored). The station position comes from LR0004 in each file and
files from different positions are refused.

## 2. BSRN quality control

* **Physically possible limits** (Long & Shi 2008, Open Atmos. Sci. J. 2, 23–37,
  Table 1), with S0 = 1361 W/m² scaled by the Earth–Sun distance and μ0 the cosine of the
  solar zenith angle from pvlib SPA:
  GHI ∈ [−4, 1.5·S0·μ0^1.2 + 100], DHI ∈ [−4, 0.95·S0·μ0^1.2 + 50], DNI ∈ [−4, S0].
  A minute outside its limit is NaN for that component only.
* **Night:** with apparent elevation ≤ 0 a minute that passes the limits is set to exactly
  0.0 (rule 5). Thermal offsets of a few W/m² are not irradiance.
* **Hourly mean:** an hour needs `min_minutes` (default 60) valid minutes per component,
  otherwise it is MISSING. The label is the end of the hour; the value covers
  (t − 1 h, t]. This matches ERA5 via Open-Meteo and the MERRA-2 relabelling.

## 3. Clear-sky check

`rwi.validation.truth.clear_sky_check`:

1. Fill gaps in the minute index with NaN so windows stay contiguous.
2. Model GHI and DNI with the platform's Ineichen-Perez (`rwi.solar.clear_sky_series`) at
   each minute's midpoint, with pvlib's monthly Linke turbidity unless a value or a
   per-minute series is given (ADR 0014).
3. Detect clear minutes with `pvlib.clearsky.detect_clearsky` (Reno & Hansen 2016,
   Renewable Energy 90, 520–531): 10-minute windows, published default thresholds.
   A clear minute must also be lit and have a measured GHI.
4. Report model minus measurement for GHI and DNI on those minutes: bias, MAE, RMSE,
   relative bias and energy ratio.

Reading: a positive DNI bias on clear minutes means the model's atmosphere is too clean
(Linke turbidity too low), a negative one too turbid. Smoke-season aerosol in August is
the expected cause of a positive bias.

`detect_clearsky` scales the model to fit the measurement before testing shape, so a
uniform 5 % offset still counts as clear; that is what lets the check measure the
offset.

## 4. Hourly bias

`rwi.validation.truth.bias_statistics(candidate, reference)`:

* The reference must be `DataKind.OBSERVATION`; both series hourly with end-of-hour labels.
* Pairs at identical labels where both values are usable.
* Daylight only: apparent elevation > 0 at t − 30 min at the reference point (pvlib SPA).
  Night pairs would add many 0 = 0 matches and dilute the bias.
* Reports n, mean reference, mean candidate, bias, MAE, RMSE, relative bias, energy
  ratio and the geodesic distance between the grid cell and the station.

## 5. Representativeness

A 25–55 km grid-cell mean against a point pyranometer is not a like-for-like
comparison. Under broken cloud the hourly RMSE is dominated by sampling, not model
error; the mean bias over many Augusts is the robust number. One station says nothing
about the Darwin end of the route (wet-season remnants, coastal cloud). The provenance
of every result says so.

## 6. Running it

Credentials: `RWI_BSRN_FTP_USERNAME`, `RWI_BSRN_FTP_PASSWORD` (WRMC account),
`RWI_EARTHDATA_USERNAME`, `RWI_EARTHDATA_PASSWORD` (NASA Earthdata).

```bash
uv run python -I scripts/truth/fetch_bsrn.py --station asp --first-year 2015 --last-year 2025
uv run python -I scripts/truth/fetch_merra2.py --latitude -23.798 --longitude 133.888 --first-year 2015 --last-year 2025
uv run python -I scripts/truth/compare_truth.py --station asp --era5
```

Fetch scripts skip what already exists and exit 2 if anything failed. The report lands
in `<truth_dir>/report-asp.json`.

## 7. Validation of the code

* `tests/unit/test_providers_bsrn.py`: synthetic LR0100 files through pvlib's reader;
  end-of-hour labels, station point from the file, limit rejection, night zeros,
  file joins and gaps, and a science test pinning the Long & Shi limits.
* `tests/unit/test_providers_merra2.py`: midpoint-to-end relabelling, `SWGDNCLR` dropped,
  NaN and out-of-range flagged, malformed frames refused.
* `tests/unit/test_validation_truth.py`: the model against itself is clear and unbiased,
  a 5 % dimmer sky shows as an energy ratio of 1/0.95, cloudy minutes are not clear,
  gaps are not counted, bias pairs only usable daylight hours.

## 8. Not done (ADR 0017)

CERES SYN1deg (needs `pyhdf`), BoM Woomera (no verified source), ML bias correction
(needs archived forecast pairs), BSRN in `TRUTH_PRIORITY` (ADR 0011 decision), IMERG Early.
