# ADR 0014 — CAMS aerosol, dynamic Linke turbidity and NASA FIRMS hotspots

Status: **Accepted** 2026-11-27 (phase 3c of the race-weather plan).
Date: 2026-11-27
Requirements: #8 Solar Radiation, #16 Climatology, #19 Atmospheric Chemistry, #28 Visibility Analysis, #35 Data Quality Control
Related: ADR 0003 (pvlib SPA), `docs/science/solar.md` sec. 1.7, `docs/science/providers.md` sec. 2.3-2.4

## Context

* The clear-sky model (Ineichen-Perez) used only the monthly SoDa Linke-turbidity
  climatology. Requirement #19 listed AOD as "not sourced". On the BWSC route in late
  August, bushfire smoke in the Top End and dust in the centre are the aerosol events
  that make real clear-sky DNI fall below the climatological value. The climatology
  cannot see them.
* The platform had no fire or smoke signal of any kind. Smoke over the Stuart Highway is
  a visibility hazard as well as an irradiance one.
* Both gaps can be filled from keyless or free sources:
  * the Open-Meteo Air Quality API serves the ECMWF CAMS global aerosol forecast;
  * NASA FIRMS serves near-real-time VIIRS/MODIS hotspots, with a free personal MAP_KEY.

## Decision

1. **CAMS aerosol provider** (`rwi.providers.cams.CamsAerosolProvider`).
   * Source: Open-Meteo Air Quality, `domains=cams_global` (0.4 deg).
   * Variables, hourly: `aerosol_optical_depth` (AOD550, dimensionless) and `dust`
     (ug/m3 at the surface).
   * Output kind: `DataKind.FORECAST`.
   * Values outside physical bounds are dropped as `OUT_OF_RANGE`. The bounds are
     AOD > 10 and dust > 1e5 ug/m3.
   * The provider is not in the `WeatherSeries` registry, because it serves aerosol only.
2. **Dynamic Linke turbidity** (`rwi.solar.turbidity.dynamic_linke_turbidity`), per hour:
   1. AOD550 -> AOD700 with the Angstrom law (`pvlib.atmosphere.angstrom_aod_at_lambda`).
   2. Precipitable water from T2m/RH2m (`gueymard94_pw`).
   3. `pvlib.atmosphere.kasten96_lt` at absolute air mass 2.
   * Air mass 2 is the TL(AM2) convention of the SoDa climatology that Ineichen-Perez expects.
   * **The Angstrom exponent is required config** (`RWI_AOD_ANGSTROM_EXPONENT`), with no
     default:
     * CAMS via Open-Meteo gives no exponent, and the right value depends on the aerosol type.
     * pvlib's built-in default is deliberately not used.
     * When it is unset, the climatology is used and the provenance says so.
   * An hour keeps the climatology when any of these holds:
     * an input is missing;
     * pw >= 5 cm (outside the published fit);
     * TL is non-finite or < 1.
   * The note counts those hours by reason.
   * `clear_sky_series` accepts a per-time `linke_turbidity` array plus a note. Its output
     is still `DataKind.CLEAR_SKY_MODEL`.
3. **NASA FIRMS hotspots** (`rwi.providers.firms.FirmsProvider`):
   * Source: the area CSV API for the route bbox, with a margin of `firms_route_buffer_km`.
   * Required config, none with a default:
     * the MAP_KEY (`RWI_FIRMS_MAP_KEY`, a `SecretStr`);
     * the source (`RWI_FIRMS_SOURCE`);
     * the day range (`RWI_FIRMS_DAY_RANGE`, 1-5);
     * the buffer.
   * Without them, `/api/map/fires` returns `available: false` and lists what is missing.
     No request is made.
   * The key appears only in the live request URL. It is masked as `***` in provenance,
     in cache keys and in errors, and the tests check this.
   * Output kind: `DataKind.OBSERVATION`, because a hotspot is a satellite detection.
     The provenance notes say the detections are near-real-time and unvalidated.
     Unparseable rows are rejected and counted.
4. **Fire alert** (`rwi.alerts.fires.fire_alerts`):
   * Hotspots are kept when all of these hold:
     * they are within `fire_alert_radius_km` of the route;
     * their confidence is >= `fire_alert_min_confidence`;
     * their FRP is >= `fire_alert_min_frp_mw`.
   * Kept hotspots are clustered along the route. Two hotspots join one cluster when they
     are at most the radius apart in distance along the route.
   * The rule emits one `WATCH` alert per cluster. Its id is `fire-{route}-{km}`.
   * The thresholds have no defaults. When they are unset, the map layer is still served
     but there are no alerts.
   * **Category is `VISIBILITY`**, because the contract has no FIRE category. Smoke on the
     road is the race hazard a hotspot points to. A dedicated category would need a
     `contracts.py` change, and that was not made here.
5. API (INTERNAL tier, so no product-schema bump):
   * `GET /api/map/fires?route_id=` returns a GeoJSON of hotspots near the route plus the
     alerts;
   * `GET /api/map/aerosol?lat&lon&hours` returns CAMS AOD/dust and the TL per hour
     (dynamic or climatology).

## Consequences

* When aerosol forecasts are high, clear-sky DNI/GHI now follow the CAMS forecast instead of
  the monthly mean. This happens only when the team has chosen an Angstrom exponent.
* Smoke near the road shows up on the map and as a WATCH alert. This happens only when the
  team has registered a MAP_KEY and set the thresholds.
* Two recorded responses are offline fixtures (see `tests/fixtures/README.md`), so tests
  never call CAMS or FIRMS.

## Limits (not hidden)

* A hotspot is a thermal anomaly seen from orbit, minutes to hours old. It is not a fire
  front, a perimeter, smoke extent or a road closure. The alert asks the team to check; it
  does not claim the road is blocked.
* A single configured Angstrom exponent is wrong for mixed smoke/dust air. The resulting TL
  has not been validated against measured DNI. BSRN Alice Springs is the planned check
  (phase 3f).
* CAMS AOD is a 0.4 deg model forecast, not a measurement. It is labelled FORECAST
  throughout.
* gueymard94_pw from surface T/RH misses elevated moisture layers.
