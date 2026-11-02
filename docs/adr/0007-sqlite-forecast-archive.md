# ADR 0007 — SQLite for the forecast/observation archive

Status: Accepted for phase 2. Re-evaluate at the triggers in ADR 0006 that have not yet fired.
Date: 2026-11-02

## Context

ADR 0006 deferred any database and listed five triggers that would force the
decision. **Trigger 1 has now fired, exactly as written:**

> Phase 2 forecast verification needs thousands of forecast/observation pairs
> queried by location, variable and lead time (MAE, RMSE, bias by lead time),
> which is awkward over files.

Everything that phase 2 is supposed to deliver depends on this one capability.
Forecast verification, bias correction, calibrated uncertainty and
evidence-based model selection are all functions of *a history of forecasts
that were recorded before their valid time, matched to what was then measured*.
Today the platform has none, which is why `docs/REQUIREMENTS.md` cannot honestly
mark a single competency "validated against real data" and why the dashboard's
verification panel reads "no verification history yet".

ADR 0006 also required that the replacement be chosen "by documented
evaluation, not by default". This is that evaluation.

### What the workload actually is

Capture is append-mostly and read-rarely-but-analytically:

* one capture run writes one forecast series per site: roughly 48 valid times ×
  10 variables ≈ 480 rows;
* hourly capture at 8 sites along the route ≈ 92,000 rows/day,
  ≈ 2.8 M rows/month, ≈ 25 M rows by August 2027, on the order of 2–4 GB;
* observations are far smaller: half-hourly, ~10 variables, a handful of
  stations ≈ 5,000 rows/day;
* reads are analytical: "every pair for GHI at 6–12 h lead, this month, from
  this model", which is a grouped scan over an indexed range, run on demand and
  not in a hot loop;
* writers: the capture worker, plus the API process if a capture is triggered by
  hand. One machine. ADR 0006's trigger 3 (several processes or machines
  writing) has **not** fired.

### Options

| Option | Fits the workload | Cost |
|---|---|---|
| Keep JSON/Parquet files | Appending is fine; "group by lead-time bucket across months" means reading everything | The awkwardness trigger 1 names; no indexes, no transactions |
| **SQLite (stdlib `sqlite3`)** | Indexed range scans, transactions, WAL for read-while-write, one file | One file to back up; single writer; limited concurrency |
| DuckDB / Parquet | Excellent analytics | Weak for continuous small appends with a concurrent reader; another dependency |
| PostgreSQL + PostGIS | Everything, eventually | A service to install, back up and keep alive on a laptop in the outback — precisely the objection ADR 0006 raised, for triggers that have not fired |

## Decision

Use **SQLite through the Python standard library** for the forecast and
observation archive.

* One file, `data/archive/rwi.sqlite3`, path from `RWI_ARCHIVE_PATH`, outside
  version control.
* WAL journal mode, so the API can read while the capture worker writes.
* Schema versioned in-database with `PRAGMA user_version`; migrations are
  forward-only and recorded in `docs/science/archive.md`.
* Provenance is stored in full with every record. A score that cannot name the
  model run it came from is not evidence.
* A recorded forecast is **immutable**: a later model run for the same valid
  time is a new row, never an overwrite. Overwriting would quietly improve our
  measured skill by replacing old forecasts with better-informed ones — the
  most dangerous form of data leakage available to us.
* Retention: raw rows are kept while the archive stays under a configured size;
  beyond it, rows older than a configured age are down-sampled to daily
  aggregates per variable, model and lead-time bucket. Deletion is logged.

No database *service* is introduced. The outback-laptop objection in ADR 0006
stands and is respected: SQLite is a file, with no daemon, no port and no
separate backup procedure beyond copying it.

## Consequences

* Phase 2 verification, bias correction and calibrated uncertainty become
  possible at all; nothing else unblocks them.
* One writer at a time. If a second operator station or a satellite worker ever
  needs to write (ADR 0006, trigger 3), this decision is revisited rather than
  stretched.
* The archive will grow to a few GB by race time, which approaches trigger 5.
  The retention policy above exists so that it does not cross it silently; if
  it does, PostgreSQL with TimescaleDB is the next step, and the SQL in the
  archive module is kept plain enough to port.
* A corrupt archive must never take the live platform down: the archive is
  opened lazily and every failure degrades to "verification unavailable", never
  to a failed weather request.
* Backing it up is the team's responsibility and belongs in the operations
  documentation before the event.

## Alternatives considered

Rejected as above. The decisive points were that trigger 3 has not fired, so
the concurrency a server buys is not needed yet; and that the capture pattern
is continuous small appends, which is DuckDB's weakest case and SQLite's
strongest.
