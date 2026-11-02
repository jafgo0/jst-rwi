# Forecast archive (`rwi.archive`)

Owner: archive. Code: `src/rwi/archive/`. Tests: `tests/unit/test_archive_*.py`. Decision record:
`docs/adr/0007-sqlite-forecast-archive.md`. Public API: `docs/CONTRACTS.md` section 3b.

Purpose: keep every forecast the platform ever relied on, exactly as it was when it was retrieved, next to
the measurements that later tested it. Without that history the platform cannot say how good a forecast is,
so no competency can be marked validated, no bias corrected and no model preferred on evidence.

Everything here that is a measurement of the code itself was measured on the development machine on
2026-11-02 with SQLite 3.53.1 (Python 3.12 stdlib `sqlite3`). **No real archived data exists yet**: every
test uses synthetic series. Statements about scientific adequacy (tolerances, retention) are design
choices, not validated results.

---

## 1. Schema (user_version 1)

All instants are `INTEGER` epoch seconds, UTC, floored to whole seconds. They are converted to aware UTC
`datetime` only at the boundary (`to_epoch_s` / `from_epoch_s` in `store.py`); callers never see integers.
Flooring is conservative for the leakage rule: it can only turn "retrieved 0.4 s before" into "same second"
(rejected), never the reverse. Coordinates are WGS84 degrees, elevation metres above mean sea level or
`NULL` (unknown, which is not 0 m).

### `series` - provenance, stored once per model run or station

| Column | Type | Unit | Meaning |
|---|---|---|---|
| `id` | INTEGER PK | - | surrogate key |
| `identity_key` | TEXT UNIQUE | - | SHA-256 of the identity (section 3); makes recording idempotent |
| `provider` | TEXT | - | e.g. `open-meteo`, `bom` |
| `dataset` | TEXT | - | endpoint/product, e.g. `forecast` |
| `kind` | TEXT | - | `DataKind` value: `forecast`, `observation`, ... |
| `model_name`, `model_version` | TEXT NULL | - | NWP model and version, if known |
| `model_init_time_utc` | INTEGER NULL | epoch s | model run initialisation ("cycle") time; lead time = valid - this |
| `retrieved_at_utc` | INTEGER | epoch s | when the FIRST fetch of this series was made (informational; the guard uses `value.retrieved_at_utc`) |
| `latitude_deg`, `longitude_deg` | REAL | degrees | grid point or station actually used |
| `elevation_m` | REAL NULL | m | its elevation |
| `requested_latitude_deg`, `requested_longitude_deg`, `requested_elevation_m` | REAL NULL | deg, deg, m | what was asked for, if different |
| `spatial_resolution_km` | REAL NULL | km | model grid spacing |
| `temporal_resolution_s` | REAL NULL | s | step of the series |
| `license`, `source_url` | TEXT NULL | - | attribution and where it came from |
| `created_at_utc` | INTEGER | epoch s | when the archive wrote the row |

### `value` - the numbers (`WITHOUT ROWID`, primary key `(series_id, variable, valid_time_utc)`)

| Column | Type | Unit | Meaning |
|---|---|---|---|
| `series_id` | INTEGER FK | - | owning series |
| `variable` | TEXT | - | canonical name from `rwi.core.variables` |
| `valid_time_utc` | INTEGER | epoch s | instant the value describes (measurement time for observations) |
| `value` | REAL NULL | canonical unit of the variable (W/m2, m/s, degC, Pa, percent, mm, ...) | `NULL` = source had no number or it was not finite |
| `unit` | TEXT | - | unit string, stored so a row is self-describing |
| `quality` | TEXT | - | `QualityFlag` value; only `ok`, `interpolated`, `unvalidated` are ever paired |
| `retrieved_at_utc` | INTEGER | epoch s | when THIS cell was fetched; the leakage guard reads it |

`UPDATE` on `series` and `value` is refused by triggers (`value_is_immutable`, `series_is_immutable`):
immutability is enforced by the database, not only by convention. Uncertainty objects are not stored (no
consumer yet; adding them is a forward migration).

### `daily_aggregate` - what retention keeps (`WITHOUT ROWID`)

Primary key `(day_utc, variable, provider, model_name, lead_lower_s)`; `day_utc` epoch s (UTC midnight);
`model_name` is `''` when unknown; `lead_lower_s`/`lead_upper_s` seconds, half-open bucket (`NULL` upper =
unbounded); `pair_count` count; `sum_error`, `sum_abs_error`, `sum_sq_error` in the variable's unit
(error = forecast minus observation; the squared sum in unit^2). From these, bias = sum_error / n, MAE =
sum_abs_error / n and RMSE = sqrt(sum_sq_error / n) are exactly recoverable. Buckets (hours):
0-3, 3-6, 6-12, 12-24, 24-48, 48+ (`LEAD_BUCKETS_S`).

### Indexes and the queries they serve

| Index | Columns | Serves |
|---|---|---|
| `value` primary key | `(series_id, variable, valid_time_utc)` | idempotency checks and "all cells of this run" |
| `idx_value_variable_time` | `(variable, valid_time_utc, series_id)` | **the pair query**: variable equality + valid-time range, on both the forecast side and the observation side; `EXPLAIN QUERY PLAN` shows `SEARCH v USING INDEX idx_value_variable_time (variable=? AND valid_time_utc>? AND valid_time_utc<?)` |
| `series.id` (rowid) | - | join from each value to its provenance |
| `idx_series_kind_model` | `(kind, provider, model_name, model_init_time_utc)` | listing/selecting runs of one model; `coverage()` grouping |
| `series.identity_key` UNIQUE | - | idempotent `record_series` |

The pair SQL says `INDEXED BY idx_value_variable_time`, so a schema change that loses the index makes the
query fail loudly instead of silently becoming a full scan. A test asserts the plan text (no `SCAN` of a
table, index used for both queries). Provider, model and lead-time filters are residual filters on the
joined `series` row; the time range is what makes the scan small, so **callers should always pass a
`period` at scale**.

## 2. The pair query

`pairs(variable, period, provider, model_name, max_separation_m, max_time_offset_s)` (plus an optional
`lead_time_range_s=(lo, hi)` half-open bucket filter, and `pairs_with_report`, which also returns a
`PairsReport`):

1. Forecast side: one range scan for `kind = 'forecast'` values of the variable inside `period` (half-open
   on valid time), usable quality, `value.retrieved_at_utc < valid_time_utc`, known model init time, and
   `valid_time >= init`. The values that fail the time rule are counted, not silently dropped.
2. Observation side: one range scan for `kind = 'observation'` values in `period` widened by the time
   tolerance.
3. Each forecast cell is matched to **one** observation inside both tolerances: smallest |time offset|, then
   smallest separation, then lowest series id. Separation is the WGS84 geodesic distance
   (`rwi.core.geo.geodesic_distance_m`), computed once per distinct grid-point/station pair and cached.
4. One pair per (model run, valid time): three runs forecasting one instant give three pairs with three lead
   times. Runs are never collapsed and never crossed with each other.

`time_offset_s` = observation time minus forecast valid time; `lead_time_s` = valid time minus **model
initialisation** time (not minus download time).

### The leakage rule

A forecast is a statement made before the event. A value fetched at or after its valid time is information
about the past (it may be an analysis, or a re-run that has seen the observations) and scoring it would
flatter the model. The archive applies the rule in SQL (`value.retrieved_at_utc < valid_time_utc`, strict),
`ForecastObservationPair` re-checks it, and the number rejected is returned in
`PairsReport.dropped_recorded_after_valid`, kept in `archive.last_pairs_report`, logged at WARNING and shown
in `coverage()["pairs"]`. Forecasts with no model init time (`dropped_no_init_time`) or init after valid
(`dropped_negative_lead`) are also excluded and counted: lead time would otherwise be a guess.

The guard uses the retrieval time **of each cell**, not of the series. This matters because a model run
fetched again later is the same run (section 3): the later fetch adds cells, and a cell added after its own
valid time must not inherit the earlier fetch's timestamp.

## 3. Immutability, idempotency and why overwriting is data leakage

A recorded value is never updated. If the archive replaced the stored forecast for 14:00 with a newer run's
forecast for 14:00 as the day went on, the archive would end up holding, for every valid time, the forecast
made with the most hindsight - and every score computed from it would measure a skill the platform never
had when it had to decide. This is the most dangerous leakage available to the team, which is why it is
forbidden by trigger as well as by code.

**Identity used by `record_series`** (`series.identity_key`, UNIQUE): the provenance of the *run or station*,
not of the download.

* forecast with known `model_init_time_utc`: `(provider, dataset, kind, model_name, model_version,
  model_init_time_utc, grid latitude, longitude, elevation)`. Latitude/longitude are rounded to 1e-6 deg,
  elevation to 0.1 m. **Retrieval time is not part of it**: hourly capture of a 6-hourly model returns the
  same run six times and must not count one forecast six times in every score (it would also multiply the
  archive's size).
* forecast without an init time: the same plus `retrieved_at_utc` (runs cannot be told apart).
* observation and any other kind: `(provider, dataset, kind, model_name, model_version, point)` - one growing
  series per station/source.

A different init time is a different run and always a new series. Within an existing identity each (variable,
valid time) cell is written once: identical cells are skipped (re-recording writes 0 rows), new cells are
added with their own retrieval time, and a cell that exists with a different value is **kept unchanged**,
counted in `archive.last_record_report.conflicting` and logged. A series that contradicts itself raises
`ArchiveConflictError` before anything is written.

## 4. Matching tolerances

Defaults: `max_separation_m = 50_000`, `max_time_offset_s = 1800` (the protocol defaults). They are a
generous starting point, **not validated choices**:

* Time: BoM station observations are half-hourly (`docs/science/providers.md`), forecasts are hourly. The
  nearest reading is therefore never more than 15 minutes from a half-hourly grid instant; 30 minutes only
  allows for a missed reading. An instantaneous observation is compared to an hourly forecast value that may
  represent an instant or an interval - that representativeness error is not removed by matching.
* Space: the global models are 25 km (IFS 0.25 deg) to ~13 km grids, so a grid centre can be ~15-20 km from a
  station that lies inside its cell. 50 km admits some station-versus-grid mismatches (coast, hills, urban
  heat). Verification should report scores for tighter tolerances (e.g. 25 km) and flag a sensitivity.
  Elevation difference between grid point and station is stored but not used to filter.

Every pair carries its own `separation_m` and `time_offset_s`, so any tolerance can be re-applied later.
Capture sites are towns, not control-stop entrances; the actual distance to the station is recorded per
pair, never assumed.

## 5. Retention policy (ADR 0007)

`prune(older_than_utc, dry_run=False, only_above_bytes=None) -> int` returns the number of raw `value` rows
deleted (or that would be). Policy:

* Raw rows are kept while the archive file stays under `Settings.archive_max_bytes`. The caller (capture
  worker or operator) decides *when*; `only_above_bytes` makes the call a no-op below the threshold.
* Rows older than the cut-off (valid time; recommended `Settings.archive_retention_days`) are first folded
  into `daily_aggregate` (pairs at default tolerances, per day, variable, provider, model and lead bucket)
  and then deleted, in one transaction. Aggregates add up across successive prunes.
* Observation rows are kept an extra 30 minutes (the default time tolerance) so forecasts valid just after
  the cut-off can still be matched.
* Forecast values with no matching observation are deleted without an aggregate (there is nothing to
  score); leaked forecasts likewise. Series left without values are removed.
* Every non-dry deletion is logged at INFO with counts; a dry run logs what it would do and changes nothing.
* Not preserved by retention: individual values, per-pair separations, uncertainty. Skill by lead bucket is.

**Never prune before the event.** Race week (August 2027) is the data the archive exists to collect; the
recommendation is to run no automatic prune after 2027-06-01, and to back up first (section 7).

## 6. Expected growth to August 2027

Basis (ADR 0007): 480 rows per series (48 valid times x 10 variables), 8 sites, hourly capture.
480 x 8 x 24 = **92,160 rows/day**. Capture from about 2026-11-15 to race start 2027-08-22 is 280 days
(15 + 31 + 31 + 28 + 31 + 30 + 31 + 30 + 31 + 22): 92,160 x 280 = **25.8 M rows**, matching the ADR.

The capture worker as configured (`rwi.capture`) is different: 7 sites, 3 models, 12 variables, 48 h, hourly.

* If every hourly fetch wrote new rows: 7 x 3 x 48 x 12 = 12,096 rows/hour = 290,304/day = **81.3 M rows** in
  280 days.
* With the identity above (one stored forecast per model run): each run is fetched for ~54 h of valid times
  (48 h window sliding for the 6 h until the next run) = 648 cells per site/model. With 4 runs/day/model:
  7 x 3 x 4 x 648 = 54,432 rows/day = **15.2 M rows** in 280 days. If a model issues 8 runs/day (ICON blends)
  the worst case is ~30 M. Observations: ~7 stations x 8 variables x 48 readings/day = ~2.7 k rows/day
  = ~0.75 M rows.
* Size: measured **78 bytes per `value` row** including both indexes (288,000 rows -> 22.5 MB, constant-valued
  synthetic data; real floats are also 8 bytes, so expect the same order, plus 5-15 % WAL/free-page slack).
  15.2 M rows = ~1.2 GB; 25.8 M = ~2.0 GB; the un-deduplicated 81.3 M would be ~6.3 GB and would exceed the
  default `archive_max_bytes` (4 GB), which is why section 3 deliberately does not key identity on
  retrieval time.
* Query cost (measured, development laptop): 28,800 pairs from 288,000 rows in 0.26 s; 50,000 pairs in about
  1 s including constructing validated pydantic objects. The cost is linear in the pairs returned, so at
  scale always pass `period` (and ideally `provider`/`model_name`). `coverage()` scans everything: 1.1 s at
  288 k rows, expect ~1-2 minutes at 25 M; use `include_pair_counts=False` for a quick look.

## 7. Backup guidance for the team

* The archive is one file, `data/archive/rwi.sqlite3` (`RWI_ARCHIVE_PATH`), plus `-wal` and `-shm` files
  while it is open. **Do not `cp` it while the capture worker runs**: a copy of the main file without the
  WAL can be missing recent commits or be inconsistent.
* Safe online backup (any time, even while writing):
  `sqlite3 data/archive/rwi.sqlite3 ".backup 'backup/rwi-YYYYMMDD.sqlite3'"`, or from Python
  `src.backup(dst)` with the stdlib `sqlite3` connection API, or `VACUUM INTO 'file'`. Then check it:
  `sqlite3 backup/... "PRAGMA integrity_check"` must print `ok`.
* Cadence: daily until race week, then every 6 hours; keep the last 7 dailies and one per week. Copy backups
  to a second device that is not the laptop (the outback laptop is the single point of failure) and bring a
  copy on a USB drive.
* Before any `prune`, before any software upgrade that adds a migration, and before race week: take a backup.
* Restore: stop the capture worker, replace the file (delete stale `-wal`/`-shm` files), start again.
  `coverage()` shows what came back.
* A corrupt archive does not stop the platform: archive calls raise `ArchiveUnavailableError`, which callers
  turn into "verification unavailable". Move the broken file aside, restore the last backup, and keep the
  broken one for inspection.
* The archive is not under version control. The licences of what it holds (Open-Meteo CC BY 4.0, BoM data
  for the team's own use only, see `docs/science/providers.md`) forbid publishing it as is.

## 8. Operation and failure behaviour

* Opening is lazy: constructing `SqliteArchive` or calling `open_archive(settings)` touches nothing. The
  first use opens the file (`open_archive` creates its directory then; a bare `SqliteArchive` does not).
* WAL journal, `synchronous=NORMAL`, 5 s busy timeout, one connection guarded by a lock, one writer at a time
  (ADR 0007's stated limit).
* Every `sqlite3` failure becomes `ArchiveUnavailableError` (corrupt file, missing/unwritable directory or
  file, locked too long, schema newer than the software) or `ArchiveError`; messages name the path. A write
  to an archive opened `read_only=True` raises `ArchiveReadOnlyError`. Callers wrap archive use in
  `except ArchiveError`.
* Migrations are forward-only: `MIGRATIONS[n]` upgrades `user_version` n-1 to n inside one transaction; a
  database from a newer version is refused, never downgraded. Add new migrations; never edit old ones.

Migration log:

| Version | Date | Change |
|---|---|---|
| 1 | 2026-11-02 | initial schema: `series`, `value`, `daily_aggregate`, indexes, immutability triggers |

## 9. Validation status

Vocabulary: not started / researching / implementing / implemented, not validated / tested /
validated against real data / operationally ready.

| Component | Status | Evidence / gap |
|---|---|---|
| Schema, indexes, WAL, `user_version` | tested | unit tests incl. `EXPLAIN QUERY PLAN`; never run on a real multi-GB file |
| `record_series` (immutability, idempotency) | tested | synthetic series only; behaviour with real provider re-fetches (init-time metadata quirks) not observed |
| Leakage guard in `pairs` | tested | synthetic late-recorded forecasts; guard is unconditional in SQL and in the pair model |
| Forecast/observation matching | tested | synthetic geometry; tolerances not validated against real stations |
| `coverage` | tested | small archives; timing at 25 M rows extrapolated, not measured |
| `prune` / daily aggregates | tested | dry run, folding and deletion on synthetic data; never run on real data; nothing yet reads `daily_aggregate` |
| Typed failure handling (corrupt, missing dir, read-only) | tested | on Linux/macOS file permissions; Windows untested |
| Migrations | implemented, not validated | only the 0 -> 1 step exists and is exercised |
| Growth and size estimates | implemented, not validated | arithmetic plus one synthetic size measurement; to be replaced by the real file size after the first weeks of capture |
| Backup procedure | researching | commands documented, never rehearsed by the team |
