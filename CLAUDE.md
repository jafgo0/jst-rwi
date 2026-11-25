# JST-RWI — Instructions for Claude

Race Weather Intelligence for JU Solar Team, BWSC 2027 (Darwin → Adelaide,
22–29 Aug 2027). Python 3.12, FastAPI, pvlib. Greenfield — no legacy
assumptions.

The authoritative documents are in `docs/`. This file points you at them and
encodes the invariants that must never be violated regardless of what a prompt
asks for.

---

## 1. Hard rules (correctness invariants)

Breaking one is a bug even if tests pass. Stop and ask before any change that
would relax one.

1. **No synthetic value is presented as an observation.** Clear-sky model
   output is never labelled as measured irradiance. Modelled fields carry
   `DataKind.CLEAR_SKY_MODEL` (or equivalent); only real readings get
   `DataKind.OBSERVATION`.
2. **Every number carries provenance:** provider, model, valid time,
   position, unit, quality. A new numeric field without provenance is
   incomplete — do not merge it.
3. **UTC internally.** Local time is derived from the active position via
   `timezonefinder`. Never hardcode Australia/Darwin, Europe/Stockholm, or
   any other zone.
4. **No hardcoded coordinates.** Position comes from the active positioning
   mode. `POSITION_UNKNOWN` is a valid, expected state — do not paper over
   it with a default or a last-known fallback that is not explicitly asked
   for.
5. **Sun below horizon ⇒ direct ground irradiance is exactly 0.0.** Not a
   small positive number from a clamped model. See `src/rwi/solar/`.
6. **Solar geometry comes from pvlib (NREL SPA).** No hand-rolled
   approximations. ADR 0003.
7. **The platform recommends; humans decide.** Never add code that issues
   vehicle control commands, autonomous overrides, or unattended actuation.

---

## 2. Module ownership — do not cross lanes

`docs/CONTRACTS.md` is binding. The short version:

| You are working on… | Edit only under… |
|---|---|
| Solar geometry / irradiance | `src/rwi/solar/**`, `tests/unit/test_solar_*.py`, `docs/science/solar.md` |
| Weather providers / QC | `src/rwi/providers/**`, `src/rwi/quality/**`, matching tests, `docs/science/providers.md` |
| Route / projection / ETA | `src/rwi/route/**`, matching tests, `data/routes/**`, `docs/science/route.md` |
| PV energy model | `src/rwi/energy/**`, matching tests, `docs/science/pv.md` |
| Vehicle dynamics / strategy | `src/rwi/vehicle/**`, `src/rwi/strategy/**`, matching tests |
| Core / contracts / API / alerts | `src/rwi/core/**`, `src/rwi/contracts.py`, `src/rwi/config.py`, `src/rwi/api/**`, `src/rwi/alerts/**` |
| Dashboard | `web/**` |
| Product contract (strategy repo surface) | `docs/PRODUCT_CONTRACT.md`, `docs/product/openapi.v0.json`, `src/rwi/api/product.py`, `src/rwi/client/**`, `scripts/export_product_schema.py` |

**Product-contract rail:** editing a route under `/api/meteorologist`,
`/api/route`, `/api/solar`, `/api/forecast-quality`, `/api/state`,
`/api/weather`, `/api/alerts` or `/api/energy` means the STABLE surface
moved. In the same PR: bump `version` in `pyproject.toml`, run
`make product-schema`, commit `docs/product/openapi.v0.json`. The pin
test (`tests/unit/test_product_schema_pin.py`) fails loudly otherwise.

If a change needs a new shared type, **do not edit `src/rwi/contracts.py`
yourself** — report that a contract change is needed and wait. Contracts are
the single coordination surface between parallel modules; duplicating a field
with a different unit is how this project will break.

---

## 3. Commands (use the Makefile; CI runs the same targets)

```bash
make check         # ruff + mypy + non-network tests. Run before claiming done.
make test          # pytest, excludes `network` and `science` markers
make test-science  # scientific reference cases (slow, but deterministic)
make test-network  # ONLY with RWI_RUN_NETWORK_TESTS=1; hits live providers
make api           # uvicorn on :8000 with reload
make lint          # ruff check
make typecheck     # mypy src/rwi
```

Python is `.venv/bin/python`. Never use a bare `pip` or `pip install`; the
venv is managed with `uv`. **Do not run `uv pip install` yourself** — if a
dependency is missing, say so and stop.

---

## 4. What Claude must not do on its own

These require an explicit ask from the user, in the same conversation:

- `git commit`, `git push`, `git checkout <branch>`, `git reset`, `git clean`,
  `git rebase`.
- `uv pip install`, `pip install`, editing `pyproject.toml`'s dependency list.
- Editing `src/rwi/contracts.py`, `src/rwi/core/**`, or any `docs/adr/*.md`
  (ADRs are append-only — supersede, don't rewrite).
- Writing or modifying anything under `data/routes/` — that directory is an
  audited provenance record of how the Stuart Highway GeoJSON was produced.
- Running `make test-network` or any script that calls a live provider.
- Deleting `data/archive/`, `data/cache/`, or `data/replay/` without being
  asked (operational data, not source).

If any of these is the natural next step, explain why and wait.

---

## 5. Testing conventions

- Use `respx` to mock HTTP providers. Do **not** mock `pvlib` — it's
  deterministic and its correctness is the point of the test.
- A test that needs the internet gets `@pytest.mark.network` and is skipped
  by default.
- A test that pins a physical answer (e.g. SPA zenith at a reference
  site/time, PV output against a datasheet example) gets
  `@pytest.mark.science` and cites the reference in a comment.
- New behaviour gets a test in the same PR. New numeric behaviour gets a
  science-marked test.

---

## 6. Style

- Ruff + mypy, line length 100, target `py312`. `make lint` and
  `make typecheck` must pass before you say "done".
- Prefer built-in generics (`list[int]`, `X | None`) over `typing.List` / 
  `Optional`. No `from __future__ import annotations` on 3.12.
- Pydantic v2 models for anything crossing a module boundary.
- Imports: standard lib, third-party, `rwi.*` — blank line between groups.
- Public functions in `rwi/*` get type hints and a one-line docstring saying
  what they return and under what provenance.

---

## 7. Where to look first

- `docs/CONTRACTS.md` — module interfaces and the ownership table.
- `docs/REQUIREMENTS.md` — 130-competency traceability matrix. New features
  should map to a requirement id in the commit message or PR body.
- `docs/ARCHITECTURE.md` — data flow (providers → QC → fusion → energy →
  strategy → API → dashboard).
- `docs/science/<area>.md` — per-module methods, assumptions, validation.
- `docs/adr/` — architecture decisions (append-only).
- `docs/research/` — verified facts about regulations and providers. Trust
  these over web search for anything about BWSC rules or provider APIs.

---

## 8. When you're unsure

Read `docs/CONTRACTS.md` and the relevant `docs/science/<area>.md` before
guessing. If the code and `docs/REQUIREMENTS.md` disagree, say so — don't
silently change one to match the other. Ask once; don't loop.

---

## 9. Working alongside other Claude sessions

Several Claude sessions (from different accounts) work in this repo and
coordinate only through GitHub issues and pull requests. Follow
`docs/SAMARBETE.sv.md`: one issue per task, claim it with a comment, one
branch per task, `Closes #<n>` in the PR, the other session reviews, a human
merges. Before starting work, check open issues and PRs so you don't
duplicate or collide with another session's lane.
