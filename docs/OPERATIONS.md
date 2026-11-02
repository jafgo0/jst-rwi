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

## Before the race

* Start capture now, and verify weekly that `value_count` is still growing.
* Decide the irradiance measurement question.
* Replace the placeholder vehicle and array parameters with measurements
  (`docs/science/vehicle.md`, `docs/science/pv.md` list what to measure).
* Settle the BoM licence question if anything will be shown outside the team
  (`docs/science/providers.md`).
* Back up `data/archive/rwi.sqlite3`. It is the only thing here that cannot be
  rebuilt from source.
