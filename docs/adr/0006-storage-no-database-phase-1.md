# ADR 0006: Storage - no database in phase 1 (file cache only)

Status: Accepted for phases 1-2. Re-evaluate against the triggers below.
Date: 2026-11-01

## Context

The master specification lists SQL and databases as a competency (item 65) and gives
PostgreSQL/PostGIS with time-series capabilities as a candidate. The honest question is what we must
persist in phase 1:

* provider responses (for caching and offline use);
* the active position/time state (single process, in memory, history capped at 5000 fixes);
* route files (small GeoJSON/GPX in `data/routes/`);
* configuration (vehicle and array parameters) which must stay out of git;
* later: forecast-versus-observation pairs for verification, telemetry history, race logs.

In phase 1 there is no telemetry, no verification history, one process and a handful of users. A database
adds an extra service to install, back up and keep alive on a laptop in the outback, a schema to migrate
and a second failure mode, while the data volumes are small.

## Decision

Phase 1 (and phase 2 until a trigger fires) uses **no database**:

* Provider responses and derived series are cached as files under `data/cache/` (directory from
  `RWI_CACHE_DIR`), keyed by provider, dataset, location grid cell, variables and model run, with TTLs
  `cache_ttl_forecast_s` and `cache_ttl_observation_s` and the retrieval time stored in the file so age is
  always computable. Writes are atomic (write to a temp name, then rename) so a crash cannot leave a half-written
  entry that looks valid; unreadable entries are discarded, never trusted.
* Routes: versioned files in `data/routes/` with provenance in the file or a sidecar.
* Configuration: environment variables and files outside version control (`.env`, configuration YAML/JSON to be
  placed outside git); no vehicle data in the repository.
* Active state: in memory (`StateStore`); on shutdown, last-known-good position and settings are written to a small
  JSON file so a restart does not lose context.
* Logs: structured log files on disk with rotation.

Triggers to adopt a database (any one is sufficient, record the decision in a new ADR):

1. Phase 2 forecast verification needs thousands of forecast/observation pairs queried by location,
   variable and lead time (MAE, RMSE, bias by lead time), which is awkward over files.
2. Phase 3 telemetry: continuous vehicle data at 1 Hz or more that must be queried, replayed and joined
   with weather and position.
3. More than one process or machine must write (for example a separate worker for satellite processing
   or a second operator station).
4. Spatial queries (nearest-segment, corridor intersections) become a bottleneck that the in-memory route
   code cannot meet.
5. Cache or history size exceeds what files handle reliably (for example above a few GB or tens of thousands
   of entries).

Likely choice then: **PostgreSQL with PostGIS** for relational and spatial data, with TimescaleDB or plain
partitioned tables for time series; or **SQLite/DuckDB/Parquet files** if single-machine analytics is enough. Choose
by documented evaluation, not by default.

## Consequences

What we gain
* Nothing to install or operate beyond Python; trivial offline recovery; easy to inspect and delete.
* Fewer failure modes in the field.

What we lose meanwhile
* **No historical query capability**: forecast verification, "which model performed best here",
  bias statistics and historical route analysis (competencies 37, 57, 59, 75, 76, 97, 128, 129)
  cannot be computed from stored history until storage exists. Phase 2 cannot honestly start verification
  without it, so the trigger is likely to fire in phase 2.
* No transactions, no concurrent-writer safety, no indexes; file-cache growth must be managed by a
  clean-up job.
* No durable telemetry store; replay (positioning mode "historical replay") needs recorded files in a
  defined format, which must be designed with the telemetry protocol.
* Cache keys based on rounded coordinates can alias nearby points; the key design must be tested.
* Data in plain files are only as protected as the file system; access control for sensitive telemetry
  must be handled at the API and operating-system level (ARCHITECTURE section 9).

## Alternatives considered

| Alternative | Why not now |
|---|---|
| PostgreSQL/PostGIS from day one | Correct for later; premature before data exist; adds an operations burden. |
| SQLite for cache and history | Attractive middle path (single file, transactions, no server) and the first candidate when a trigger fires; not needed to meet phase-1 goals. |
| Redis / in-memory cache | Extra service; loses state on restart, which is the opposite of the offline requirement. |
| Parquet/DuckDB for verification | Good fit for analytics; adopt with the verification module. |
| Time-series database (InfluxDB, Timescale) | Only justified by telemetry volume, which does not exist yet. |
