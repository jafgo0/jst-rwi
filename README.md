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
uv sync --frozen --python 3.12 --extra dev   # exactly what uv.lock pins
.venv/bin/pytest -q
.venv/bin/uvicorn rwi.api.app:app --reload --port 8000
```

`uv.lock` is committed; CI installs with `uv sync --frozen` and fails if it
is stale. Changed a dependency in `pyproject.toml`? Run `uv lock` and commit
the lock in the same change. Add `--extra ml` for the ADR 0008 ML stack.

Dashboard: `http://localhost:8000/` with the API running. Opening
`web/index.html` from disk (`file://`, origin `null`) is deliberately not
allowed by CORS.

## Running it in VS Code

Open the folder `jst-rwi` itself (not its parent), then:

1. **Pick the interpreter.** `Cmd+Shift+P` -> "Python: Select Interpreter" ->
   `./.venv/bin/python`. This is the step that matters: with the system Python
   selected instead, nothing imports and the editor reports errors that are not
   real. `.vscode/settings.json` already points at it, so usually VS Code finds
   it by itself.
2. **Install the recommended extensions** when prompted (Python, Pylance, Ruff,
   Mypy). `.vscode/extensions.json` lists them.
3. **Press F5** and choose **"API + dashboard (localhost:8000)"**, then open
   <http://localhost:8000>. The API serves the dashboard at the root, so there
   is no separate web server to start.

After every restart the platform has no position and the dashboard shows
"POSITION UNKNOWN" in each panel - it refuses to guess where the vehicle is.
Set one from **Operator controls** at the bottom of the Command Center, or run
the task "set a demo position (Alice Springs)" (`Cmd+Shift+P` -> "Tasks: Run
Task").

Other launch configurations: capture once (all sites or one), the whole test
suite, the file you are looking at, the scientific reference cases, and the
live network tests. Use the non-reloading API configuration when you need
breakpoints: with `--reload` the app runs in a child process and breakpoints
are unreliable.

`Cmd+Shift+P` -> "Tasks: Run Task" also offers `make check` (lint, types,
tests) and the end-to-end smoke check.

## Documentation

* `docs/CONTRACTS.md` — module interfaces and ownership
* `docs/REQUIREMENTS.md` — the 130-competency traceability matrix
* `docs/ARCHITECTURE.md` — architecture and data flow
* `docs/science/` — per-module methods, assumptions and validation status
* `docs/research/` — verified facts about regulations and data providers
