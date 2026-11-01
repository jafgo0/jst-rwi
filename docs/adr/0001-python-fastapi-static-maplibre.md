# ADR 0001: Python + FastAPI backend with a static MapLibre dashboard in phase 1

Status: Accepted (phase 1). Revisit at the start of Phase 4 (see Consequences).
Date: 2026-11-01

## Context

The platform is dominated by scientific computation (solar geometry, irradiance and PV models,
geodesy, time series, later optimisation and satellite processing) and by data integration. The
team's scarce resource is engineering time before August 2027, and the first scientifically
testable milestone (correct position, time, solar geometry, observation-versus-forecast
distinction on a real map) does not depend on a rich front end.

The master specification offers React + TypeScript + MapLibre as a candidate stack and says
explicitly that it is not mandatory. It also asks for a real geographic map (section 11) and
forbids a decorative substitute.

Constraints from the Regulations research: the operator may be offline for long stretches
(Team Manager's Guide p.13), so the front end must work from a locally served bundle with no
build-time or run-time CDN dependency in operation.

## Decision

1. Backend in **Python 3.12** with **FastAPI**, **Pydantic v2** and **uvicorn**. Scientific code
   uses NumPy, pandas, SciPy, pvlib and pyproj directly; there is no language boundary between
   the science and the service.
2. Phase-1 dashboard is a **static front end** (plain HTML, CSS and ES modules in `web/`) using
   **MapLibre GL JS** as the map renderer, served by FastAPI as static files and talking only to the
   documented `/api/*` endpoints in `docs/CONTRACTS.md`. No bundler and no Node toolchain are
   required to run or deploy it. MapLibre and any chart library are vendored into the repository
   (pinned versions) so the dashboard works offline.
3. The API is the contract. The front end holds no scientific logic: every number it shows is a field
   of an API payload with its provenance, and unit conversion for display is the only arithmetic.

## Consequences

Positive
* One language for science and service; type information flows from Pydantic models to OpenAPI.
* No build pipeline to break during the race; deployment is `uvicorn` plus a folder of files.
* The team can inspect and fix the whole stack with Python and basic JavaScript knowledge.
* The API-first boundary keeps a later React rewrite possible without touching the science.

Negative
* No component framework: state management, layout and testing of a larger UI are manual. Views F
  (Energy and Strategy) and H (Historical Analysis) of the master specification will strain a
  hand-written front end.
* No TypeScript types on the front end unless generated from OpenAPI by hand.
* Frontend tests (required by master spec section 15) need a browser-level tool (for example Playwright,
  not currently a dependency); until then front-end verification is manual and must be reported as such.
* Python is single-process by default; heavy work (satellite processing, optimisation) must be
  moved to worker processes later (see ARCHITECTURE section 10).

Trigger to revisit: the dashboard exceeds roughly five interactive views with shared state, or
two or more front-end defects have been caused by missing types, or a second front-end developer joins.
At that point migrate to Vite + TypeScript + React as a build-time artefact that still outputs static
files (the API and deployment shape do not change).

## Alternatives considered

| Alternative | Why not now |
|---|---|
| React + TypeScript + Vite from day one | Strong long-term, but adds Node tooling, a build step and a second dependency tree before any scientific value exists. Offline vendoring becomes more involved. Not rejected for later. |
| Server-rendered templates (Jinja/HTMX) | Fewer moving parts, but map and chart interactivity (timeline scrubbing, layer toggles) ends up as ad-hoc JavaScript anyway. |
| Streamlit / Dash / NiceGUI | Fast to start, but weak real-map control, hard to give a racing-engineer-grade layout, and the map is a plug-in rather than the centre. |
| Node/TypeScript backend | Throws away pvlib, NumPy and SciPy; unacceptable for the science. |
| Desktop app (Electron/Qt) | Heavier to distribute and test; a local web app already runs on any laptop. |
| Commercial map SDK (Google, Mapbox GL v2+) | Licence and offline restrictions; MapLibre is the open fork and fits offline tile packs. |
