# Race-week climatology (ADR 0016)

Module: `rwi.climatology` (`raceweek.py`, `build.py`), script `scripts/build_climatology.py`,
API `GET /api/climatology/race-week`.

## 1. What it answers

"What have 22–29 August looked like, during driving hours, on each part of the route?"
Every number is `DataKind.CLIMATOLOGY`. It is a long-term statistic, not a forecast for a
given day, and its source (ERA5) is reanalysis, not observation.

## 2. Data

* ERA5 hourly reanalysis via the Open-Meteo archive API (`models=era5`), about 0.25° grid
  (reported as 25 km).
* Variables: `cloud_cover` [percent], `ghi` [W/m2] (Open-Meteo `shortwave_radiation`),
  `wind_speed_10m` [m/s], `wind_direction_10m` [deg, direction the wind comes from].
* Time convention: radiation at valid time *t* is the mean over (*t* − 1 h, *t*]; cloud and
  wind are instantaneous at *t*. The provider must run in `preceding_hour_mean` mode.

## 3. Sampling

* Segments: control stop to control stop (2025 official stops), Darwin → Adelaide, 10 in all.
* Points: cell-centred every `spacing_km` along each segment, at least the midpoint. Spacing
  below about 28 km revisits the same ERA5 cells and adds calls, not information.
* Years: `first_year`..`last_year`. A year whose race week has not ended is refused.
* Window per year: event-time (ACST, UTC+9:30) 22 Aug 00:00 to 30 Aug 00:00. The archive is
  queried in whole UTC days covering it.

### Building and resuming

* One archive request per point per year. Each answer is saved straight away as a gzipped
  part under `<RWI_CLIMATOLOGY_DIR>/parts/` (git-ignored) and reused by the next run.
* A failed request is retried with exponential backoff; a point-year that still fails is
  listed and the build continues. Three failures in a row stop it early.
* The climatology file is only written when every point-year is present, so a gap never
  turns into a silently thinner sample. Rerun the same command to fill the gaps.
* Change of years or spacing reuses every part whose coordinates and year match.

## 4. Hour selection

Driving window: `SCHEDULE.daily_start`..`daily_stop` (08:00–17:00 event time).

| Quantity | Counted when | Per day (ACST, whole-hour UTC data) |
|---|---|---|
| GHI | the whole hour interval lies inside the window | 8 hours, ending 09:30 … 16:30 |
| Cloud, wind, tailwind | the valid time lies inside the window | 9 steps, 08:30 … 16:30 |
| Daily GHI energy | every whole-hour interval of the event-time day is present | 1 value |

Because ACST is a half-hour offset, an event-time day holds 23 whole UTC hour intervals. The
two half hours either side of local midnight are left out of daily energy; pvlib SPA puts the
sun more than 30° below the horizon then at Darwin and Adelaide (test
`test_dropped_slivers_are_night`), so nothing is lost.

## 5. Statistics

* Distributions: mean, min, max, P10/P25/P50/P75/P90 with numpy's default linear
  interpolation (Hyndman & Fan 1996, type 7).
* Tailwind: `speed · cos(dir_from − heading − 180°)`. Positive pushes the car. Heading is the
  route bearing at each sample point.
* Wind rose: 16 sectors of 22.5°, sector 0 centred on north (348.75°–11.25°), half-open
  intervals. Frequency per sector sums to 1. Calm (speed exactly 0) has no direction and is
  counted in `n_calm_excluded`.
* Daily energy: Σ hourly mean GHI × 1 h / 1000 [kWh/m2]; a day with any missing hour is
  dropped and counted in `n_missing`.
* `n_missing` counts selected steps with no usable value (MISSING or failed QC).

## 6. Assumptions and limits

* Hourly samples within a day, and neighbouring points, are strongly correlated. Treat
  `n_years` as the record length when judging how stable a P10 or P90 is.
* ERA5 cloud and surface radiation carry known regional biases. They are not corrected here;
  BSRN Alice Springs and BoM stations arrive in phase 3f (ADR 0017).
* The 2025 control stops are used; rebuild after updating the table if 2027 differs.
* The race week is pooled as a whole; there is no trend over the years and no ENSO or IOD
  stratification.

## 7. Validation

Unit tests (`tests/unit/test_climatology_*.py`, `tests/unit/test_api_climatology.py`):
segment boundaries, UTC race-week window, hour-selection counts on a synthetic series,
quantile type 7 against a hand-checked case, tailwind sign, wind-rose binning, daily-energy
gap handling, the build loop against a `respx`-mocked archive (one call per point-year,
resume with zero calls, retry backoff, a failure that does not stop the rest, corrupt and
foreign parts refetched), and every API error path.
No comparison against ground truth yet.
