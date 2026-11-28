# ADR 0017 — Ground-truth sources: BSRN Alice Springs and MERRA-2

Status: **Accepted** 2026-11-28 (phase 3f of the race-weather plan).
Date: 2026-11-28
Requirements: #16 Climatology, #19 Verification
Related: ADR 0003 (pvlib), ADR 0011 (ML targets), ADR 0014 (dynamic turbidity),
ADR 0016 (climatology), `docs/science/truth.md`

## Context

* The climatology (ADR 0016) and every forecast verification so far compare models with
  models. Nothing tells us how far ERA5's August GHI is from what a pyranometer at the
  route sees, or whether the platform's clear-sky model (Ineichen-Perez with pvlib's
  monthly Linke turbidity) is right for the dry season.
* BSRN Alice Springs (station `asp`, about 1 km off the Stuart Highway) measures GHI, DNI
  and DHI separately every minute with secondary-standard instruments. It is the best
  irradiance truth on the route. Access is by FTP with a free account from the World
  Radiation Monitoring Center.
* The plan also names BoM Woomera, MERRA-2, CERES SYN1deg, SatCORPS and IMERG Early, and
  ML bias correction.

## Decision

1. **BSRN** (`rwi.providers.bsrn`): read station-to-archive files with
   `pvlib.iotools.read_bsrn` (LR0100), apply the BSRN "physically possible" limits
   (Long & Shi 2008), set night minutes that pass them to exactly 0.0 (rule 5), and form
   hourly means labelled at the end of the hour as `DataKind.OBSERVATION`. An hour needs
   all 60 minutes by default. The station position comes from the file's LR0004, never a
   constant (rule 4). FTP credentials live in `Settings` as `SecretStr`.
2. **MERRA-2** (`rwi.providers.merra2`): the `pvlib.iotools.get_merra2` output for
   M2T1NXRAD `SWGDN` becomes an hourly `DataKind.ANALYSIS` GHI series at the grid cell.
   pvlib stamps hourly means at HH:30; the label is moved to the end of the hour. The
   model's own clear-sky flux (`SWGDNCLR`) is dropped so it is never read as GHI.
3. **Comparison** (`rwi.validation.truth`): `clear_sky_check` finds clear minutes in the
   BSRN data with `pvlib.clearsky.detect_clearsky` (Reno & Hansen 2016, published
   defaults) and reports model minus measurement for GHI and DNI on them;
   `bias_statistics` pairs an hourly candidate (ERA5, MERRA-2) with the BSRN hours at
   identical labels in daylight and reports bias, MAE, RMSE and energy ratio. Both are
   `DataKind.DERIVED`, provider `rwi`, and name their inputs.
4. **Scripts the user runs** (network): `scripts/truth/fetch_bsrn.py`,
   `scripts/truth/fetch_merra2.py`, `scripts/truth/compare_truth.py`. Data lands under
   `RWI_TRUTH_DIR` (default `data/truth/`, git-ignored: BSRN data may not be
   redistributed). Tests use synthetic files only.

### Deferred, with the reason

* **CERES SYN1deg native files** need `pyhdf`, which is not a dependency. NASA POWER's
  hourly `ALLSKY_SFC_SW_DWN` is CERES-based and reachable over REST, but its hour-label
  convention is not documented clearly enough to pair with end-of-hour labels without
  guessing. Revisit once someone confirms the convention or approves `pyhdf`.
* **BoM Woomera 1-minute solar** has no scriptable public endpoint we have verified and
  no sample file in the repo. Adding a reader needs one real file first.
* **ML bias correction** of AIFS/IFS needs archived forecast–truth pairs from the capture
  service; a reanalysis-vs-BSRN offset is not a forecast error model. BSRN is also not in
  `rwi.ml.targets.TRUTH_PRIORITY`; adding it is an ADR 0011 decision.
* **SatCORPS** stays a retrospective check (ADR 0016 context). **IMERG Early** is optional
  and not needed for irradiance.

## Consequences

* The dry-season clear-sky model and ERA5's GHI bias can be measured at one route point
  over many Augusts. One point is not the route: the result says nothing about the
  Darwin end, and a grid-cell mean against a point carries representativeness error,
  which the provenance says.
* No correction is applied anywhere yet. If a bias is confirmed, correcting the
  climatology or forecasts is a new decision with its own ADR.

## Alternatives considered

* **Fetching BSRN inside the API**: rejected; it is a slow FTP archive with a personal
  account, and the comparison is an offline study.
* **Keeping `SWGDNCLR` as a separate variable**: rejected; the platform has no
  "model clear-sky flux" variable and a GHI-like number without one is a rule-1 risk.
