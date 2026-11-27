# ADR 0015 — Camp-site charging forecast per candidate overnight stop

Status: **Accepted** 2026-11-27 (phase 3d of the race-weather plan).
Date: 2026-11-27
Requirements: #5 Atmospheric Humidity, #126 Control Stop Analysis
Related: ADR 0003 (pvlib SPA), ADR 0013 (stale-model guard), `docs/science/pv.md` sec. 1.7,
`docs/research/bwsc-2027-regulations.md` (clauses 3.17.2, 3.21, 3.27.9, 3.28)

## Context

* The car stops for the night at about 17:00 event time. It may charge from the stationary
  array until sunset or until the 19:00 charging lockout starts, whichever comes first. It
  may charge again from sunrise or from the 06:30 lockout end, whichever is later, until the
  08:00 start.
* These two windows are short and the sun is low in both. This makes the outcome sensitive
  to three things the existing energy model does not look at:
  * DNI at low sun;
  * cloud tens of kilometres along the line of sight toward the western and eastern horizons,
    not overhead;
  * gusts while the array is raised.
* Dew on the array at dawn delays the morning window.
* The strategy tool, which is a separate repository, chooses where to stop. It needs a
  per-candidate outlook to make that choice. RWI must not make the choice itself.

## Decision

1. **Module** `rwi.energy.camp`, exposed only through `GET /api/camp/forecast`.
   * The route takes `route_id`, `date` (event-time date) and 1–6 `distance_km` values. It
     evaluates each candidate. It never ranks, scores or picks one.
   * The route is INTERNAL tier: `/api/camp` is not a product-contract prefix, so there is
     no version bump and no schema export.
2. **Windows**
   * Built from `rwi.regulations.SCHEDULE`, using daily stop, daily start and the charging
     lockout.
   * Sunset and sunrise come from pvlib SPA (rule 6), at geometric elevation −0.8333°.
   * Each window reports which bound limited each end: `daily_stop`, `daily_start`, `sunset`,
     `sunrise` or `charging_lockout`. If a window has no positive length, its times are
     `None` and the label says why, for example `sunset_before_daily_stop`.
3. **Energy per window**
   * Clear-sky energy: Ineichen-Perez GHI, DNI and DHI, sampled every 60 s and integrated
     with the trapezoid rule. It is labelled `CLEAR_SKY_MODEL`. Below the horizon the value
     is exactly 0.0 (rule 5).
   * Forecast energy: the Open-Meteo preceding-hour means, labelled `FORECAST`. Each
     provider hour contributes `mean x 1 h x E_cs(hour ∩ window) / E_cs(hour)`, so the
     hour's energy is spread over time with the clear-sky shape.
     * If the provider serves instantaneous radiation instead, forecast energy is not
       computed (`None`). This avoids treating a point value as an hourly mean.
   * Gusts: the peak 10 m gust over the provider hours that overlap the window.
4. **Horizon cloud**
   * Total, low, mid and high cloud cover is read at configured offsets along the sunset
     azimuth (west) and the next sunrise azimuth (east).
   * Each value is taken at the provider step nearest the sun event, within half a step.
     Each carries its own `FORECAST` provenance.
5. **Dew at dawn**
   * For each forecast hour from sunset to sunrise, array temperature comes from
     `pvlib.temperature.faiman_rad` with these settings:
     * POA = 0;
     * a horizontal array, with emissivity 0.88 and sky view 1.0 (pvlib defaults);
     * downwelling longwave from Brutsaert clear-sky emissivity with the Crawford & Duchon
       cloud term;
     * vapour pressure from Magnus, with Alduchov & Eskridge coefficients, evaluated at the
       dew point.
   * Risk is flagged when `T_array − T_dew ≤ margin`. `risk_at_sunrise` is the value for the
     step nearest sunrise.
6. **Thresholds are required config, with no defaults:**
   * `RWI_CAMP_GUST_WARNING_M_S`
   * `RWI_CAMP_DEW_MARGIN_C`
   * `RWI_CAMP_HORIZON_OFFSETS_KM` (a JSON list; all values must be positive)

   When a setting is unset, its flag or horizon list is absent and the response says so in
   `notes`. The team has not set a gust limit for the raised array, and dew margin and
   horizon distance are not calibrated. Shipping a guessed number would look like a
   validated limit.
7. **Degradation**
   * If the provider fails, or the date is past Open-Meteo's 384 h horizon
     (`CapabilityNotSupportedError`), the candidate falls back to clear-sky only.
   * In that case `forecast_available` is false and `message` says why. A provider error
     never becomes a 500.

## Consequences

* The strategy tool gets a comparable outlook per candidate stop, with provenance on every
  number. The decision about where to stop stays with the strategy tool and its humans
  (rule 7).
* No contract change: the response models live in `rwi.energy.camp` and the router.
* The dew model is an indicator, not a validated frost or dew forecast:
  * the real array is tilted toward the sun when raised, and its sky view is less than 1;
  * after sunrise, solar heating is ignored (POA 0);
  * Crawford & Duchon derived the cloud fraction from a solar deficit, but here it is
    NWP cloud cover.
* Forecast energy is bounded by the forecast's own skill at low sun. Nothing here corrects
  it; that is ML post-processing (ADR 0008).
* Forecasts reach only about 16 days ahead. Earlier planning gets clear-sky only, until
  race-week climatology is added (phase 3e).

## Alternatives considered

* **Rank candidates in RWI.** Rejected: choosing the stop belongs to the strategy tool, and
  ranking would need its cost model.
* **Use the overhead cloud cover only.** Rejected: at 5–15° elevation, the line of sight
  crosses the cloud layer tens of kilometres away.
* **Default thresholds** (for example a 10 m/s gust). Rejected: a default would look like a
  team-approved limit.
* **Dew from relative humidity alone** (`RH > x %`). Rejected: radiative cooling of the
  array is the mechanism, and it depends on cloud and wind, not on RH alone.

## Status

Accepted. Tested with `respx`-mocked Open-Meteo and pvlib references. Not validated against
an observed camp night.
