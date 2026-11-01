# JST-RWI — Race Weather Intelligence

Meteorological and race decision-support platform for **JU Solar Team**,
Bridgestone World Solar Challenge 2027 (22–29 August 2027, Darwin → Adelaide).

Greenfield project. No code or architectural assumption is carried over from any
earlier weather application.

## What this is

A system that connects **weather intelligence** to **vehicle energy and race
strategy**: real observations and forecasts along the actual route, physically
modelled solar irradiance and PV output, vehicle energy balance, and scenario
comparison for human strategists.

## What this is not

Not a consumer weather app, not a demo. Rules that hold everywhere in the code:

* no synthetic value is ever presented as an observation;
* every number carries provider, model, valid time, position, unit, quality;
* clear-sky model output is never labelled as measured irradiance;
* with the Sun below the horizon, ground-level direct irradiance is exactly zero;
* no hardcoded coordinates — position comes from the active positioning mode;
* UTC internally, local time derived from the active position's own timezone;
* the platform recommends; humans decide. No vehicle control commands.

## Layout

```
src/rwi/core/        shared primitives: time, geodesy, units, wind, provenance, position state
src/rwi/contracts.py every cross-module type, in one place
src/rwi/solar/       solar geometry and clear-sky irradiance (pvlib / NREL SPA)
src/rwi/providers/   weather source adapters (Open-Meteo, …) + fallback
src/rwi/quality/     quality control and freshness gating
src/rwi/route/       route geometry, map matching, ETA trajectory
src/rwi/energy/      POA transposition, cell temperature, PV power and energy
src/rwi/vehicle/     longitudinal dynamics and consumption
src/rwi/strategy/    scenario evaluation and recommendations
src/rwi/api/         FastAPI application
web/                 MapLibre operations dashboard
docs/                requirements matrix, architecture, ADRs, science notes
```

## Quick start

```bash
uv venv --python 3.12 .venv
VIRTUAL_ENV=.venv uv pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/uvicorn rwi.api.app:app --reload --port 8000
```

Dashboard: open `web/index.html` (or `http://localhost:8000/` once the API
serves it) with the API running.

## Documentation

* `docs/CONTRACTS.md` — module interfaces and ownership
* `docs/REQUIREMENTS.md` — the 130-competency traceability matrix
* `docs/ARCHITECTURE.md` — architecture and data flow
* `docs/science/` — per-module methods, assumptions and validation status
* `docs/research/` — verified facts about regulations and data providers
