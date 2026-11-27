# ADR 0013 — BoM ACCESS-G via Open-Meteo rejected; stale-model guard

Status: **Accepted** 2026-11-27 (phase 3b of the race-weather plan; owner chose "rejection + guard").
Date: 2026-11-27
Requirements: #35 Data Quality Control, #55 Multi-Model Forecast Comparison, #59 Forecast Verification
Related: ADR 0008 (AIFS), `docs/science/providers.md` (dead `bom_access_global` row), `docs/science/fusion.md`

## Context

The plan for phase 3b was to add the Bureau of Meteorology's ACCESS-G (and its ensemble, ACCESS-GE) to the model comparison and the verification, through Open-Meteo. Open-Meteo still accepts both model names. We checked them against Open-Meteo's Metadata API on 2026-11-27:

| Directory | `last_run_initialisation_time` | `data_end_time` |
|---|---|---|
| `bom_access_global` | 2025-06-26 18:00Z | 2025-06-30 07:00Z |
| `bom_access_global_ensemble` | 2025-06-26 18:00Z | 2025-07-07 03:00Z |

Both have been frozen for about 17 months. The forecast API still answers HTTP 200 for them, but every value is `null` (fixture `openmeteo_forecast_all_null_bom_access.json`). The adapter parsed that as a series of MISSING values carrying a 2025 run time. That is honest about the values, but it makes a dead model look like a live one with gaps. Open-Meteo's docs page itself says "BOM ACCESS Global (until Jun 2025)".

There is no other free source of BoM NWP grids. The real-time grids are part of BoM's paid Registered User services (`docs/science/providers.md`, section on BoM; contact `webreg@bom.gov.au`).

## Decision

1. **ACCESS-G and ACCESS-GE are not added** to `CAPTURE_MODELS`, the model comparison or the verification. There is nothing to verify: no run since 2025-06-26.
2. **Guard for stale models** in `OpenMeteoProvider`:
   - After a data request, the adapter reads the model's Metadata API entry, as it already did for the run time.
   - If `data_end_time` is **earlier than the first requested hour**, it raises `ProviderUnavailableError`. The message says the model is not updated (stale) and gives the last run and the data end.
   - The refusal is not cached, so a model that comes back to life is used again on the next request.
   - The guard covers every model with a metadata directory, ECMWF included. `bom_access_global` and `bom_access_global_ensemble` are listed in `_META_DIR` so the guard sees them.
   - A model whose data ends *inside* the window is still served. Hours past the end stay MISSING, as before.
   - If the metadata cannot be read, nothing changes: the series is served, with `model_init_time_utc = None` and the metadata error recorded.
3. **Evidence:** the two metadata responses from 2026-11-27 are offline fixtures (`openmeteo_meta_bom_access_global*.json`), replayed by respx.

## Consequences

- The comparison stays at ECMWF IFS (0.25° and 9 km), AIFS, GFS and ICON. Only the ECMWF models report a run time and can be verified. There is no Australian model in the set.
- A model that Open-Meteo stops updating now fails loudly instead of quietly turning into nulls. Callers with a fallback chain move on to the next provider.
- Getting BoM NWP into the platform means a paid BoM registration. That is the owner's decision, not something code can fix. If it happens, it gets a new ADR and its own adapter.
- Re-check: if Open-Meteo resumes ACCESS-G, this ADR is superseded (ADRs are append-only), not edited.
