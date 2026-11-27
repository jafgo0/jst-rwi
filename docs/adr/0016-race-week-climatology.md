# ADR 0016 — Race-week climatology per control-stop segment (ERA5)

Status: **Accepted** 2026-11-27 (phase 3e of the race-weather plan).
Date: 2026-11-27
Requirements: #16 Climatology
Related: ADR 0003 (pvlib), ADR 0015 (camp charging, which falls back to clear-sky
beyond the forecast horizon), `docs/science/climatology.md`

## Context

* Forecasts reach about 16 days ahead. Planning months before the race (pack sizing,
  strategy priors, which segments to expect headwind on) needs a different tool: what
  22–29 August has looked like over many years, per part of the route.
* The plan names ERA5, MERRA-2 and BSRN. ERA5 is the only one of those that is gridded,
  hourly, has wind and cloud and GHI together, and is already reachable through the
  Open-Meteo archive API that `OpenMeteoProvider.fetch_analysis` speaks. MERRA-2 and BSRN
  arrive with the truth sources in phase 3f (ADR 0017).
* ERA5 is reanalysis: model output constrained by observations. It is not an observation
  (rule 1), and its cloud and surface radiation are known to be biased in places.

## Decision

1. **Module** `rwi.climatology`, built offline by `scripts/build_climatology.py` and served
   read-only by `GET /api/climatology/race-week?route_id=&distance_km=`.
   * The API never fetches. It serves the JSON file the script wrote, or 404 if it does not
     exist, or 503 if it fails validation or belongs to another route.
   * INTERNAL tier: `/api/climatology` is not a product-contract prefix, so there is no
     version bump and no schema export.
2. **Segments** run control stop to control stop, using the 2025 official stops
   (`OFFICIAL_2025_CONTROL_STOPS`): Darwin → Katherine … Port Augusta → Adelaide, 10 in all.
   Each segment is sampled at cell-centred points every `--spacing-km` (at least the
   midpoint), with the route heading at each point.
3. **Window.** Event-time dates 22–29 August (`EVENT_TIMEZONE`) of every year from
   `--first-year` to `--last-year`. One archive request per point per year with
   `models=era5`. Any provider error aborts the build; a partial climatology is never
   written. A year whose race week has not ended yet is refused.
4. **Hour selection** follows the Open-Meteo time convention:
   * GHI is a preceding-hour mean, so an hour counts only when its whole interval lies
     inside the 08:00–17:00 driving window (`SCHEDULE`).
   * Cloud cover and wind are instantaneous, so a step counts when its valid time lies
     inside the window.
   * Instantaneous radiation mode is refused: the daily energy would be wrong.
5. **Statistics per segment**, pooled over points, years and selected hours:
   * cloud cover, GHI, 10 m wind speed, and the tailwind component along the route heading
     (`speed · cos(direction_from − heading − 180°)`, positive pushes the car): mean, min,
     max and P10/P25/P50/P75/P90 (numpy linear, Hyndman & Fan type 7);
   * daily GHI energy per point per event-time day, summed over the whole-hour intervals
     of that day; a day with any missing hour is dropped and counted in `n_missing`;
   * a 16-sector wind rose centred on north, frequency and mean speed per sector; calm
     (speed exactly 0) has no direction and is counted separately.
6. **Provenance.** Every distribution carries `DataKind.CLIMATOLOGY`, provider
   `open-meteo`, dataset `archive`, model `era5`, the segment midpoint, ERA5's 25 km grid
   and a `processing` string naming the years and the pooling. `n_years` is reported next to
   `n_samples` because hourly samples are strongly autocorrelated.

## Consequences

* Pre-race planning gets distributions with honest labels, without anyone treating a
  climatological P50 as a forecast.
* The output file lives in `data/climatology/` (config `RWI_CLIMATOLOGY_DIR`). Building it
  calls a live provider, so it is run by a human, not by CI or tests.
* Segments follow the 2025 stops. If the 2027 stops differ, the file must be rebuilt after
  the stop table is updated.
* ERA5 biases pass straight through. Nothing here corrects them against ground truth; that
  needs BSRN and BoM stations (ADR 0017).
* Climatology is not wired into the camp forecast or the energy model yet. The strategy tool
  can read it from the API.

## Alternatives considered

* **Fetch on request.** Rejected: 10 segments × points × years is hundreds of archive
  calls; a request would take minutes and hammer the free API.
* **Fixed-length segments (e.g. 100 km).** Rejected: control stops are where the strategy
  actually makes decisions, and they bound the daily legs.
* **Monthly means from a published climatology.** Rejected: August means mix the dry-season
  build-up and hide the spread; the race-week tail (P10/P90) is the point.
* **Pool every hour of the day.** Rejected: night hours dilute cloud and wind statistics
  with conditions the car never drives in.

## Status

Accepted. Tested with `respx`-mocked ERA5 archive responses and pvlib references. The real
file has not been built yet; that is a live-provider run.

## Amendment 2026-11-27 — resumable, retrying build

Decision item 3 said any provider error aborts the build. With 35 years at 50 km spacing
that is about 2,000 archive calls, and one 503 near the end threw all of them away. It
now reads:

* **Checkpoints.** Every point-year series is written as gzipped JSON (about 11 KB) to
  `<parts-dir>/<route_id>/era5/<year>/<lat>_<lon>.json.gz` as soon as it arrives, by
  write-then-rename. A rerun loads a part instead of fetching it. A part that cannot be
  read, is not `era5`, lacks a variable, does not cover the race week or belongs to
  another point is fetched again. Parts are keyed by coordinates, not spacing.
* **Retries.** A point-year is tried `--attempts` times (default 5), waiting
  `--backoff-s · 2^k` between tries (default 30 s, so 30/60/120/240 s), on top of the
  provider's own HTTP retries. `CapabilityNotSupportedError` is a configuration bug and is
  never retried.
* **Failures.** A point-year that still fails is recorded and the build goes on with the
  rest. `--max-consecutive-failures` (default 3) failures in a row stop the run early, since
  the provider is then down or the quota is spent.
* **Still no partial climatology.** If any point-year is missing at the end, the build
  raises `IncompleteClimatologyError` listing them, the script exits with status 2 and no
  climatology file is written. Rerunning the same command fetches only what is missing.

