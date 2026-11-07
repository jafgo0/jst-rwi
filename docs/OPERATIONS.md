# Operations

How to run JST-RWI, and the one job that must not be forgotten.

## The capture job is the time-critical one

Forecast verification, bias correction, calibrated uncertainty and
evidence-based model choice are all functions of a history of forecasts
recorded *before* their valid time, matched against what was then measured.

**That history cannot be acquired retrospectively.** Every week the capture job
is not running is a week of evidence the team will not have in August 2027. It
is the only part of this platform where delay costs something that cannot be
recovered by working harder later.

### Install it (macOS)

```bash
./scripts/install-capture-service.sh --install
```

This writes a LaunchAgent to `~/Library/LaunchAgents/` and loads it. It runs
once immediately so the install is verifiable, then hourly.

| Command | What it does |
|---|---|
| `./scripts/install-capture-service.sh --status` | is it loaded, did it exit cleanly |
| `./scripts/install-capture-service.sh --run-now` | trigger a cycle immediately |
| `./scripts/install-capture-service.sh --log` | last 40 lines of the log |
| `./scripts/install-capture-service.sh --uninstall` | stop and remove it; the archive is untouched |

Log: `~/Library/Logs/jst-rwi-capture.log`.

On Linux a systemd timer calling `scripts/capture.sh` hourly does the same job;
on any platform, `cron` running `scripts/capture.sh` is enough.

### What it costs

Per cycle: 7 sites × 5 models forecasts, plus observations from the sites that
have a station. About 42 provider calls an hour, roughly 1,000 a day, against
Open-Meteo's free-tier limit of 10,000 a day. Measured cycle time: under 2 s.

Growth is about 290,000 rows a day, a few GB by race time. `--prune` runs each
cycle and only deletes once the archive exceeds `RWI_ARCHIVE_MAX_BYTES`
(ADR 0007).

### Check on it

The job is scheduled rather than daemonised on purpose: a scheduler that
restarts the process hourly is much harder to kill silently than a daemon that
dies at 03:00 and is noticed in March. But **somebody still has to look**.

```bash
curl -s localhost:8000/api/forecast/archive | python3 -m json.tool | head -20
```

`coverage.value_count` should grow every hour. The History view in the
dashboard shows the same thing, including `coverage.pairs`, which is where the
first scored forecasts will appear.

## Which sites can actually be verified

A forecast can only be scored where something measured the weather. Confirmed
by a real capture on 2026-11-02:

| Site | BoM observations | Can produce verification pairs |
|---|---|---|
| Darwin | yes | yes |
| Katherine | **no** — nearest station Daly Waters, 232 km | **no** |
| Tennant Creek | yes | yes |
| Alice Springs | yes | yes |
| Coober Pedy | yes | yes |
| Port Augusta | yes | yes |
| Adelaide | yes | yes |

Katherine is captured anyway: its forecasts still feed model comparison and the
route view, and it is a control stop the vehicle must visit.

## Satellite imagery: what it costs to run

Measured on 2026-11-02 for the three C03 tiles the race route needs
(T059, T069, T078):

| | |
|---|---|
| Per 10-minute slot, sun up | 11.8-15.5 MB |
| Per slot, at night | ~8.7 MB |
| Average rate | ~0.2 Mbit/s |
| Race day (08:00-17:00 ACST, 54 slots) | **~0.8 GB** |
| Only the tile the car is in | ~5 MB/slot, **~0.26 GB/day** |

**Verdict: feasible from a support car if the data plan allows about 1 GB a
day.** Following only the car's own tile cuts it to a quarter, at the cost of
losing sight of weather approaching from outside that tile.

Not verified: file sizes in August (cloudier scenes compress worse, so expect
more), and the real link speed on the Stuart Highway. The Team Manager's Guide
warns that cellular coverage is poor over large stretches, so assume the
imagery is unavailable for hours at a time and that the platform must degrade
to the numerical forecast — which it does.

## What still cannot be verified at all

**Irradiance.** No configured provider measures GHI, DNI or DHI, so the
variables that decide the energy strategy are the ones the platform cannot
check itself against. A pyranometer on the vehicle or a support car is the
single highest-value instrument the team could add, and it needs to be
logging before the race, not during it.

**Lead times for GFS and ICON.** Only ECMWF models publish a model run time
through this provider, so only ECMWF forecasts can be scored
(`docs/science/capture.md`). GFS and ICON still contribute to model comparison.

## Running the API and dashboard

```bash
make api                      # http://localhost:8000, serves the dashboard too
./scripts/smoke.sh            # end-to-end check of the honesty rules
make check                    # lint, types, tests
```

The API is read-mostly and safe to restart at any time; the capture job is a
separate process for exactly that reason.

### Run it as a single process

`StateStore` lives in process memory (`src/rwi/core/state.py`). A fix posted to
worker A is not visible to worker B. Run `uvicorn` with **one worker** on the
race-day laptop; that is the only supported mode today.

```bash
uvicorn rwi.api.app:app --host 127.0.0.1 --port 8000       # OK
uvicorn rwi.api.app:app --workers 4                          # DO NOT: splits state 4 ways
gunicorn -w 4 rwi.api.app:app                                # DO NOT: same reason
```

If someone wants to run multiple API processes behind a load balancer, the
state store has to move out of process first (Redis, SQLite with WAL, or a
dedicated writer) and `get_state_store()` has to become an adapter. That is
work, not a config flip.

### Trusted-source telemetry secret

`POST /api/state/position` with `mode=live_gps` is refused unless the
deployment sets `RWI_TELEMETRY_SHARED_SECRET` AND the caller presents the
same value in the `X-Telemetry-Secret` header. Without the env var set, the
HTTP path for live_gps is closed; manual and route_sim modes stay open so
operator entry and simulation still work. This closes the trivial path where
any page reachable on the private network could lie about being the car.

* Rotate the secret per race; store it in a password manager, not in the
  repo.
* The real telemetry pipeline (`rwi.telemetry.TelemetryCoordinator`) applies
  monotonicity, freshness and implied-speed gates that this single-shot
  endpoint cannot; prefer the pipeline for anything beyond operator-driven
  pokes.

### Faster data: what it would take

Live dashboard latency today is bounded by two independent clocks:

1. **State poll** — the UI polls `/api/state` every 5 s (`pollStateMs` in
   `web/config.js`). Lowering it to 1 s on a LAN is safe; the backend reads
   from an in-memory store.
2. **Provider fetch** — Open-Meteo forecasts update at the model cadence
   (6 h for ECMWF-IFS, 1 h for GFS); nothing the client does gets a new
   value between model runs. The capture job already pre-warms the archive
   for the 7 configured route sites; extending the site list and enabling
   `capture_enabled=true` is the real "faster data" lever.

A new value cannot arrive before the upstream model publishes it. What the
platform controls is how quickly a published value reaches the dashboard:
pre-warm the cache (`capture_enabled=true`), shorten
`cache_ttl_forecast_s` only where quota permits, and reduce `pollStateMs` on
a trusted LAN. A WebSocket push path would help only once the capture
cadence is already down to minutes; today it is hours.

## Before the race

* Start capture now, and verify weekly that `value_count` is still growing.
* Decide the irradiance measurement question.
* Replace the placeholder vehicle and array parameters with measurements
  (`docs/science/vehicle.md`, `docs/science/pv.md` list what to measure).
* Settle the BoM licence question if anything will be shown outside the team
  (`docs/science/providers.md`).
* Back up `data/archive/rwi.sqlite3`. It is the only thing here that cannot be
  rebuilt from source.
