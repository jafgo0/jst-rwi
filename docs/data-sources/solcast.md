# Solcast route forecast: open question for the organisers (2026-11-28)

Status: **not integrated**. Waiting on an answer from the BWSC organisers.

## What we know

* The project brief says a Solcast route forecast was offered to teams at earlier
  events. This is not verified in `docs/research/`; no rules text, URL or format is on
  file.
* The team's drive-system repo holds Solcast "estimated actuals" for 2025-08-23..31
  (temperature, GHI, wind; no MSLP or dewpoint), see
  `docs/validation/synoptic-calibration.md`. Estimated actuals are satellite-derived
  model output, not station data.
* No Solcast facts are claimed in `docs/science/providers.md`.

## Ask the organisers

1. Is a Solcast route forecast offered to teams for BWSC 2027 (22–29 Aug 2027)?
2. Free with entry, or a separate agreement with Solcast?
3. Access: API key per team, a shared feed, or files? Rate limits during the race?
4. Content: which variables (GHI, DNI, DHI, cloud, temperature, wind), horizon, time step,
   update cadence, probabilistic bands (P10/P90)?
5. Where along the route: fixed points, a polyline, or any coordinate?
6. Licence: may we store it, show it on our dashboard, and compare it with other
   providers in our own validation?
7. Is historical data from 2023/2025 available for back-testing?

## If the answer is yes

Add it as a **comparison provider**, not a primary source, until it is verified against
BSRN and ERA5 like the others (ADR 0017):

* `src/rwi/providers/solcast.py` with `respx`-mocked tests. Forecasts are
  `DataKind.FORECAST`; estimated actuals are `DataKind.ANALYSIS`, never `OBSERVATION`
  (rule 1).
* Full provenance per value, including Solcast's own interval-label convention, checked
  against a real response before pairing it with end-of-hour labels.
* Credentials in `Settings` as `SecretStr`, never committed.
* Scores in the forecast-quality pipeline next to ECMWF IFS/AIFS.
* A research note in `docs/research/` with the organisers' answer and the licence terms.

## If the answer is no

Nothing to do. Buying Solcast access ourselves is a separate decision with a cost.
