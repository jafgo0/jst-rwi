# Capture worker (`rwi.capture`)

## Why this module exists

Forecast verification, bias correction, calibrated uncertainty and
evidence-based model selection are all functions of one thing: a record of what
was forecast *before the fact*, matched against what was then measured. That
record does not exist until something writes it down, every hour, for months.

This is that something. Without it, `rwi.verification`, `rwi.fusion` and the
calibration half of `rwi.ensemble` are machinery with no fuel, and
`docs/REQUIREMENTS.md` can never honestly move a competency past "tested".

**The practical consequence for the team: capture must start running long
before the race.** A verification history cannot be acquired retrospectively.
Every week it is not running is a week of evidence the platform will not have
in August 2027.

## What is captured

### Sites

Seven, in `sites.py`, spanning the route and its climate regimes: Darwin,
Katherine, Tennant Creek, Alice Springs, Coober Pedy, Port Augusta, Adelaide.

They are BWSC control stops (plus the start and finish) for three reasons: they
are on the route, the vehicle stops at each for 30 minutes, and several sit at
or near Bureau of Meteorology stations — which is what makes a *pair* possible
at all. A forecast at a point with no nearby station can never be scored.

Deliberately not more sites: each one costs a provider call per model per
cycle, the free Open-Meteo tier allows 10,000 calls a day, and a site with no
station produces forecasts that are archived and never scored.

The coordinates are the towns, not the control-stop entrances. The difference
is up to a few kilometres and is recorded in `docs/science/route.md`; for
verification against a station tens of kilometres away it is not the dominant
error.

### Variables

`temperature_2m`, `relative_humidity_2m`, `dew_point_2m`, `pressure_msl`,
`wind_speed_10m`, `wind_direction_10m`, `wind_gusts_10m`, `cloud_cover`,
`precipitation`, `ghi`, `dni`, `dhi`.

Chosen for what the race needs scored: irradiance (energy), wind (drag and
stability), cloud (the irradiance driver), temperature (array and battery
efficiency) and precipitation (safety). Pressure and humidity are cheap and
make the thermodynamic picture checkable.

Note the asymmetry that limits everything: **no configured provider measures
irradiance.** BoM gives temperature, wind, humidity, pressure, cloud and
visibility. So GHI, DNI and DHI forecasts are archived and currently cannot be
verified at all. Closing that gap needs a pyranometer on the vehicle or support
car, and it is the single highest-value instrumentation the team could add.

### Models

`ecmwf_ifs025`, `gfs_seamless`, `icon_seamless`, captured **separately**.

The provider's `best_match` is deliberately excluded. It is a blend whose
composition can change without notice, so a skill score computed from it would
not describe any model that exists, and it could not tell the team which model
to trust in central Australia — which is the question verification is for.

### The model-run-time problem (measured and then resolved, 2026-11-02)

A capture of 3,600 forecast values across `ecmwf_ifs025`, `gfs_seamless` and
`icon_seamless` produced **zero scoreable pairs**. The archive refuses to pair
a forecast whose initialisation time is unknown, and it is right to: lead time
is *valid time minus run time*, so without a run time the single most important
dimension of forecast skill is undefined. 2,352 of 3,600 values (65 %) were
dropped as `dropped_no_init_time`.

**The first explanation was wrong.** It was recorded as a hypothesis that
"seamless" models are stitched blends of several runs, so no single run time
exists. A direct probe of the live API refuted it:

| `models=` | Reports a model run time? |
|---|---|
| `ecmwf_ifs025` | yes |
| `ecmwf_ifs` | yes |
| `ecmwf_aifs025_single` | yes |
| `gfs_global` | **no** |
| `gfs_seamless` | **no** |
| `icon_global` | **no** |
| `icon_seamless` | **no** |

The single-run `gfs_global` and `icon_global` lack it as well. The actual rule
is simpler and has nothing to do with blending: **only ECMWF models expose a
run time through this provider.** Switching from the seamless to the global
variants would have changed nothing, which is exactly why the model list was
not changed until the probe had run.

**What this unlocked.** `ecmwf_aifs025_single` is ECMWF's AI forecast, and
`ecmwf_ifs` / `ecmwf_ifs025` are its physics-based ones. All three report run
times, so capturing them together makes an **AI-versus-physics benchmark on
identical lead times** possible. Section 5 of the master specification forbids
claiming that AI forecasting outperforms physical models without that evidence;
this is how the team will get it.

**The resulting model list** (`CAPTURE_MODELS`) serves two purposes:

* *verifiable*: `ecmwf_ifs025`, `ecmwf_ifs`, `ecmwf_aifs025_single`;
* *comparison only*: `gfs_global`, `icon_global`. They can never be scored, but
  `rwi.fusion` compares models at the same valid time and needs no lead time,
  and genuine disagreement between forecasting centres is the cheapest honest
  signal that a forecast needs a margin. The `_global` variants are preferred
  over `_seamless` because a seamless model is itself a blend, which is not a
  meaningful comparison member.

If GFS or ICON lead times are ever needed, the run time would have to come from
somewhere else — NOAA's own distribution publishes it — which is a provider
question, not an archive one.

## Design rules

1. **Partial failure never costs a cycle.** One model, one provider or one site
   failing is recorded and the rest proceeds. A provider outage is not an error
   in us, and partial history is worth far more than none. Demonstrated in
   practice on 2026-11-02, when Open-Meteo returned HTTP 503 for every model
   and the cycle still archived 208 BoM observation rows.
2. **Nothing is invented.** A failed fetch is a recorded failure, never a gap
   filled with a neighbouring value or a previous cycle's data.
3. **A provider that returns model output for an observation request is
   refused.** If `fetch_observations` comes back with anything other than
   `DataKind.OBSERVATION`, it is not archived and the mismatch is reported.
   Letting an analysis into the archive as a measurement would corrupt every
   score computed afterwards.
4. **Rows written is not the same as work done.** The archive refuses to
   duplicate a series it already holds (ADR 0007's immutability rule), so a
   healthy cycle can legitimately write zero rows. The worker tracks series
   *fetched* separately and says which case it is, because telling an operator
   "no observations captured" when the archive already has them would send them
   hunting for a fault that does not exist.
5. **Back off, then stop.** Consecutive total failures back off exponentially
   and, past `capture_max_failures`, the worker stops rather than hammering a
   provider that is clearly down. A capture gap is recoverable; a rate-limit
   ban is not.
6. **Off by default.** `RWI_CAPTURE_ENABLED` defaults to false. A process that
   silently makes provider calls on a metered connection in the outback must be
   switched on deliberately.

## Running it

```bash
.venv/bin/python -m rwi.capture --once                 # one cycle, all sites
.venv/bin/python -m rwi.capture --once --sites darwin alice-springs
.venv/bin/python -m rwi.capture                        # loop on the configured interval
```

It runs as its own process, separate from the API on purpose: capture must keep
running while the API is restarted, and a crash in one must not take the other
down. SQLite in WAL mode lets the API read while this process writes.

For continuous capture before the race, a systemd unit, a launchd job or a cron
entry calling `--once` hourly are all fine. Whichever is chosen, **someone must
check that it is still running**; silent death of the capture worker is the
most likely way the team arrives in August with no verification history.

## Expected growth

One cycle at one site with three models over a 48 h horizon writes roughly
3 × 48 × 12 ≈ 1,700 forecast values. Hourly at seven sites: about 290,000 rows
a day, roughly 8.7 M a month. The arithmetic and the retention policy are in
ADR 0007; the headline is a few GB by race time, which SQLite handles and which
the retention policy keeps bounded.

## Status

| Component | Status |
|---|---|
| Site selection and rationale | tested |
| `capture_once` partial-failure behaviour | tested |
| Observation-kind refusal | tested |
| `run_forever` backoff and stop | implemented, not validated (no long run yet) |
| CLI entry point | implemented, not validated |
| Forecast capture against a live provider | tested — 3,600 forecast values captured from 3 models at 2 sites on 2026-11-02, 0 failures |
| Model run times | tested — probed across 7 models; only ECMWF reports one, and the capture list now reflects that |
| AI-versus-physics benchmark (AIFS vs IFS) | not started — the data is now being captured; a score needs forecasts to reach their valid time |
| Retention enforcement (`enforce_retention`) | tested |
| Continuous operation over weeks | not started |

Nothing here is "validated against real data" in the sense that matters: the
worker has not yet run for long enough to produce a verification history. That
is the next milestone, and it is a matter of elapsed time, not of code.
