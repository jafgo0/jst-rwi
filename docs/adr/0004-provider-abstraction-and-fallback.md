# ADR 0004: Provider abstraction with explicit capabilities and fallback; a forecast is never substituted for an observation

Status: Accepted
Date: 2026-11-01

## Context

Master specification section 5 requires a multi-provider architecture, preserved provenance for every
record, stale and conflict detection, and the rule "never silently substitute a forecast for a
measurement". The data landscape is uncertain (see `docs/research/data-sources.md`): Open-Meteo is easy
to use but its free tier is non-commercial and its model list changes; BoM observations are the
authoritative local source but their licence path is unclear; ECMWF open data are free but need GRIB
tooling; satellite data come later. The team cannot know today which sources will be allowed and
reliable in August 2027. Connectivity along the route is poor (Team Manager's Guide p.13), so any
provider will fail at times.

The most dangerous failure for a decision-support tool is not "no data" but data that looks like
something it is not: a model forecast shown as a current measurement, a four-hour-old value shown as
fresh, or a hand-fixed default shown as a reading.

## Decision

1. **One adapter interface.** Every source implements the `WeatherProvider` protocol in `contracts.py`:
   `capabilities`, `fetch_forecast`, `fetch_observations`, `health`. Adapters convert to canonical
   variable names and units at the boundary and attach full `Provenance`.
2. **Capabilities are declared and checked, not assumed.** `ProviderCapabilities` states whether the
   provider supports observations, forecasts, ensembles and archive, its variables, nominal resolution,
   update interval, licence, attribution and whether a key is needed. A call outside the declared
   capability raises `CapabilityNotSupportedError`.
3. **The kind of data is part of the type.** `WeatherSeries.kind` is `observation`, `forecast` (or
   other `DataKind`) for the whole series; series of different kinds are never merged. An observation
   request never falls back to a forecast: it fails, serves cached observations with their true age,
   or returns "unsupported". A forecast may serve as an input to a model but is always labelled as one.
4. **Fallback is explicit and ordered, within a kind.** `fetch_with_fallback(providers, point, window,
   variables, kind=...)` tries providers in a configured order for that kind, records which one answered
   and which ones failed, and returns the winner's series with its own provenance. Fusion of several
   sources is a separate, explicit later layer that keeps the originals.
5. **Freshness is enforced centrally.** `rwi.quality.check_series` flags values stale against
   `max_observation_age_s` and `max_forecast_age_s` (settings), out of range and rate-implausible
   against the variable registry; it flags and never deletes. Offline mode (`allow_network=false`) serves
   cache with age or raises.
6. **No fabricated numbers.** `Measured.value` may be `None` ("source had nothing"); it is never turned
   into zero. A solar value computed from geometry carries `kind=clear_sky_model`.
7. **Licence handling is data.** Attribution text and licence are in `ProviderCapabilities` and shown by
   the dashboard; a source whose licence is unresolved (BoM direct feeds) has no adapter until a human
   resolves it.

## Consequences

Positive
* New sources (BoM, ECMWF GRIB, satellite) are added without touching consumers.
* The dashboard can always say what a number is, where it came from and how old it is.
* A provider outage degrades to cached or reduced data with visible flags, not to silent substitution.

Negative
* More code than calling one API; every adapter must be tested against fixtures (`respx`) for unit
  conversion, provenance and failure behaviour.
* The "observation" side is thin at first: Open-Meteo is a model-based service, and its "current"
  or archive values are analysis/reanalysis, not instrument readings. Until a real observation source
  (BoM stations, team sensors) is licensed and implemented, the platform's honest answer to "what is
  measured right now" is "nothing", and the UI must say so. This is a deliberate cost.
* Fallback order is a policy decision that needs verification data (which source is better where) that does not
  exist yet; initial ordering is by availability and licence, not skill.
* Per-capability checks cannot detect a provider that lies about its own timestamps; only comparison
  with independent observations can (phase 2 verification).

## Alternatives considered

| Alternative | Why not |
|---|---|
| Single provider (Open-Meteo) hard-wired | Fastest start, but no resilience, licence dependence on one service and no path to BoM or ECMWF. |
| Generic "weather client" that quietly picks the best available data | Hides exactly the distinction the master specification forbids hiding. |
| Merge observation and forecast into one continuous series for charts | Convenient, but the join point invites misreading; the UI draws them as separate series instead. |
| Use a third-party aggregation library | Adds a dependency whose provenance and unit handling we cannot control. |
| Message bus between providers and consumers | Unneeded complexity for a single-process application (ARCHITECTURE section 3). |
