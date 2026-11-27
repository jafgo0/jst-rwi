# ADR 0012 — Ensemble cold-front passage time per control stop

Status: **Accepted** 2026-11-27 (phase 3a of the race-weather plan; owner approved one ADR per sub-step).
Date: 2026-11-27
Requirements: #14 Weather Fronts, #53 Forecast Uncertainty Analysis, #54 Probabilistic Forecasting, #56 Ensemble Forecasting
Related: ADR 0008 (AIFS), ADR 0011 (stable `/api/v1`), `docs/validation/synoptic-calibration.md`

## Context

`rwi.alerts.synoptic.detect_front_passages` finds a surface cold-front signature in one deterministic series. It looks for four things: a 24 h-detrended MSLP trough, a NW→SW backing wind shift, 24 h cooling and a dewpoint drop. A front needs at least two of them, and one must be kinematic.

Fronts matter at the South Australian end (Coober Pedy → Port Augusta). There, a passage brings a wind shift, gusts and a cloud band. A single model run gives one passage time and no idea how sure it is. The strategy team needs a probability of a front and a P10/P50/P90 passage time per stop.

## Decision

### 1. Method (`rwi.alerts.fronts`)

- **Ensemble.** Run the existing detector, unchanged and with its default thresholds, on every member of the Open-Meteo ECMWF IFS 0.25° ensemble (`ecmwf_ifs025`, control first) at each stop.
- **Which front counts.** A member's front is its earliest detected passage in `[now, now + hours]`. Passages before `now` (inside the look-back) are ignored.
- **Probability.** `probability = members_with_front / members_evaluated`. A member with no steps is not evaluated. A member without a front stays in the denominator.
- **Passage quantiles.**
  - P10/P50/P90 are linear-interpolation quantiles (numpy default) of the passage epochs of members *with* a front. They are conditional on a front occurring, and the payload says so.
  - They are computed only when at least `MIN_MEMBERS_FOR_QUANTILES = 3` members have a front. That is the smallest sample for which P10 < P50 < P90 can differ at all. It is a statistical floor, not a physical threshold.
  - Below the floor the quantiles are `null` and a reason is given.
- **AIFS cross-check.** ECMWF AIFS single (`ecmwf_aifs025_single`) runs through the same detector and is reported as a separate deterministic passage. It is **not** mixed into the ensemble quantiles, because it is a different system with no member exchangeability.
- **Window.** It starts at `now − 30 h`, which covers the 24 h detrend plus the ±3 h detector half-window. It ends at `now + hours + 3 h`. The ensemble keeps only about 3 days of history, so 30 h is safe.

### 2. Stops

The defaults are Coober Pedy, Glendambo and Port Augusta, taken from the named waypoints of the BWSC route. The `stops` query parameter chooses others by name. An unknown name returns 422.

### 3. Alert rule

The rule is `front_alerts(outlook, watch=, warning=)`: severity WATCH or WARNING from the front probability, with category `WIND`. There is no `FRONT` category in `AlertCategory`, and adding one is a contract change that needs the owner's approval.

- **Thresholds are config with no default:** `RWI_FRONT_ALERT_WATCH_PROBABILITY` and `RWI_FRONT_ALERT_WARNING_PROBABILITY`. Unset means no alerts, and the response says `alerts_configured: false`. A probability threshold here is an operational choice, not something to guess.
- **Content.**
  - Evidence: P10/P50/P90, the member fraction and the model.
  - Valid window: P10–P90.
  - The recommended action only asks that the team be briefed. The platform recommends; humans decide.

### 4. Endpoint

`GET /api/v1/fronts?stops&hours` is STABLE and pinned in `docs/product/openapi.v0.json`.

- `hours` defaults to 72 and is limited to 1–168.
- Results are cached for 10 min per (stops, hours).
- A failed fetch for one stop or one model is recorded on that stop. It does not fail the whole response.

## Consequences

- **Not calibrated.** The detector is uncalibrated. Against ERA5 and ISD it fires about 67 times a year per station and locks to the diurnal cycle (see #14 in REQUIREMENTS.md). The ensemble probability is therefore a *model-front* probability, not a verified frequency of observed fronts. Every response carries this caveat.
- **Timing resolution.** Ensemble values are interpolated hourly from native 3–6 h steps, so passage times carry at least that much timing uncertainty.
- **Where it applies.** The rules are built for mid-latitude cold fronts. In the tropical north, a "front" from them is not meaningful, which is one reason the default stops are all in SA.
- **Cost.** It adds two Open-Meteo requests per stop per 10 min.
