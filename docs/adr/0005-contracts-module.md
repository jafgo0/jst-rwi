# ADR 0005: One cross-module contracts module as the typed interface layer

Status: Accepted
Date: 2026-11-01

## Context

Seven modules (`solar`, `providers`, `quality`, `route`, `energy`, `vehicle`, `strategy`) plus `api`,
`alerts` and the web dashboard are being written in parallel by different people and agents. The
usual failure of this situation is interface drift: two modules each define "an irradiance value"
with different units, one field is optional in one place and required in another, a speed is in km/h
on one side and m/s on the other. The master specification asks for typed interfaces, explicit units
and documented interface contracts (section 13), and for every value to carry provenance.

## Decision

1. Every type that crosses a module boundary is defined in **`src/rwi/contracts.py`** (shapes:
   `SolarPosition`, `IrradianceComponents`, `WeatherSeries`, `ProviderCapabilities`, `Route`, `Trajectory`,
   `PowerEstimate`, `VehicleConfig`, `ScenarioResult`, `Recommendation`, `Alert`, and so on). Primitives
   they build on (`GeoPoint`, `Provenance`, `Measured`, `TimeWindow`, `PositionFix`, `ActiveState`,
   the variable registry, wind and unit helpers) live in **`src/rwi/core/`**.
2. Modules own **behaviour**; `core` and `contracts` own **shapes**. Feature modules may import from
   `core` and `contracts`; they may not redefine, widen or duplicate those types.
3. Contract types are **immutable Pydantic v2 models** with validators that enforce physical invariants
   at construction (for example: irradiance components must be zero when the Sun is below the
   horizon; an observation must carry an observation time; a forecast a valid time; a `Measured`
   must use the registry's canonical unit; routes must have non-decreasing cumulative distance).
4. Changing a contract is done **once, by the integrator**, not by the module that needs it. A module
   that needs a field reports it. Ownership and signatures are listed in `docs/CONTRACTS.md`.
5. Units are canonical and fixed (`rwi.core.variables`): SI-based, irradiance W/m2, power W, energy
   Wh, pressure Pa, wind speed m/s with direction "from" clockwise from true north, times aware UTC. Conversion
   happens only at the boundary (provider in, UI out).
6. The HTTP API serialises these same models, so the dashboard and the Python code share one vocabulary.

## Consequences

Positive
* Parallel work is possible: each module can be tested against the contract alone.
* Invariants (night-time zero, unit checks, provenance) are enforced in one place and cannot be bypassed by
  a module that forgets them.
* OpenAPI schema and documentation come largely for free.

Negative
* `contracts.py` is a coordination bottleneck and a merge-conflict hotspot; one owner serialises changes.
* A big shared file couples all modules to it: a breaking change touches everyone. Mitigation: additive
  changes preferred, tests in `tests/unit/test_contracts.py`, and a documented change procedure.
* Validators at construction cost some CPU in hot loops (long trajectories); measure before optimising
  (ARCHITECTURE section 10).
* Pydantic models are Python-specific; any non-Python consumer (for example a C extension) will use the JSON schema.
* Known issues already found: the `ArrayConfig` comment mentions GaAs although the 2027 Regulations prohibit GaAs
  for solar conversion (clause 2.4.4); an informational comment, but the integrator should correct it.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Each module defines its own types and the API converts | Maximum local freedom, maximum drift. Conversion code becomes the bug farm. |
| Python `Protocol` per boundary, duck typing | No runtime validation; unit and provenance rules would not be enforced. |
| `dataclasses` or `TypedDict` | Lacks validators and JSON schema; weaker for the API. |
| Generated types from an IDL (protobuf, OpenAPI first) | Heavier toolchain for a single-process Python application; reconsider if a second language appears. |
| Splitting contracts into one file per module | More files, but cross-references between modules (for example `PowerEstimate` using `POAIrradiance`) would create import cycles. Reconsider if the file grows beyond maintainability. |
