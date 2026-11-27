# JST-RWI dashboard (web/)

Operations dashboard for the JU Solar Team race weather platform. Plain HTML,
ES modules and CSS: **no build step, no npm**. Libraries are vendored under
`vendor/` at exact pinned versions and loaded with SRI hashes, so the dashboard
loads with no internet connection (master prompt §14):

| Library | Version | Local path | Upstream source |
|---|---|---|---|
| MapLibre GL JS (JS + CSS) | 4.7.1 | `vendor/maplibre-gl-4.7.1/` | unpkg.com/maplibre-gl@4.7.1/dist/ |
| uPlot (JS + CSS) | 1.6.31 | `vendor/uplot-1.6.31/` | cdn.jsdelivr.net/npm/uplot@1.6.31/dist/ |

Offline, only the **basemap tiles** are missing: the map falls back to a plain
background with the route and overlays drawn on top, and shows a
"BASEMAP UNAVAILABLE" banner. Geography is not drawn in that state. To keep
geography offline, set `map.styleUrl` in `config.js` to a style served from this
machine (for example a PMTiles extract).

## Running it

1. Start the API from the project root: `.venv/bin/uvicorn rwi.api.app:app --port 8000`
2. Open **http://localhost:8000/**. The API serves `web/` at `/`, so the dashboard
   is same-origin with the API: no CORS setup, and `apiBase` stays empty.

Other ways:

* Any static server from inside `web/` (`python -m http.server 8080`), then
  `http://localhost:8080/?api=http://localhost:8000`. The API's CORS allow-list is
  `localhost:8000` and `127.0.0.1:8000`, so another origin needs a backend
  change or a proxy.
* `file://` does **not** work in current browsers (they block ES modules from
  file URLs). `index.html` detects this and says so on screen after 2.5 s.
  The `null` origin a file page sends is deliberately **not** in the CORS
  allow-list: serve the dashboard from the API (`make api`, then
  http://localhost:8000/) instead.
* The browser caches ES modules heuristically. After editing a file, hard-reload
  (Cmd/Ctrl+Shift+R) or you will see the old code.

`?api=URL` overrides the API base for one page load (used to test the
API-unreachable state).

## Configuration (`config.js`)

A classic script that sets `window.RWI_CONFIG`; every key is optional
(defaults in `js/settings.js`).

**`web/config.js` is operator-local and ignored by git** (via
`.gitignore`). The versioned file is `web/config.example.js`: copy it
to `web/config.js` on each machine and put map tokens or API-key
overrides there. The real `config.js` never enters version control.

* `apiBase`: API base URL ("" = same origin, the normal case when the API serves `web/`).
* `requestTimeoutMs`, `pollStateMs`, `pollSlowMs`.
* `freshnessDefaultsS`: fallback freshness limits used **only** while
  `GET /api/health` has not been read. Normally the limits come from
  `freshness_limits_s` in that response (`store.absorbLimits`) and the footer says
  "(/api/health)"; the fallback is labelled "UI default".
* `defaultSpeedKmh`: initial value of the operator speed input (not a measurement).
* `telemetry.sharedSecret`: must equal the backend's `RWI_TELEMETRY_SHARED_SECRET`;
  needed only for "Use computer location (live_gps)" under Operator controls, which
  posts this computer's location-services position every
  `telemetry.computerLocationIntervalS` s (default 10) labelled
  `source=computer_location_services`, `fix_quality=unknown`. See
  `docs/OPERATIONS.md` → "Computer location as a stand-in GPS".
* `map`: see below.

### Changing the map style or key

Edit `map` in `config.js`:

```js
map: {
  styleUrl: "https://api.maptiler.com/maps/streets-v2/style.json",
  apiKey: "YOUR_MAPTILER_KEY",          // appended as ?key=...  (apiKeyParam to rename)
  providerName: "MapTiler Streets v2",   // printed in the footer
  licenceText: "(c) MapTiler (c) OpenStreetMap contributors",
  requiresOsmCredit: true,               // adds the OpenStreetMap credit to the footer
}
```

Any MapLibre-compatible style URL works. The key is visible to anyone who can load
the page, so use a domain-restricted key. The footer prints exactly what is
configured, so keep `providerName` / `licenceText` truthful.

#### Using a Mapbox style with MapLibre

The code loads a MapLibre style and does NOT embed the Mapbox GL SDK. If the team
holds a Mapbox account and wants to use one of their styles, point MapLibre at
Mapbox's Style Specification endpoint and let MapLibre render it; nothing in the
repo assumes a Mapbox token exists, and no token is checked into git.

```js
map: {
  styleUrl: "https://api.mapbox.com/styles/v1/<username>/<style_id>",
  apiKey:   "YOUR_MAPBOX_ACCESS_TOKEN",   // keep OUT of git; config.js is operator-local
  apiKeyParam: "access_token",             // Mapbox uses access_token=... not key=...
  providerName: "Mapbox <style_id>",
  licenceText:  "(c) Mapbox (c) OpenStreetMap contributors",
  requiresOsmCredit: true,
}
```

Known caveats of the MapLibre-reads-Mapbox path:

* Mapbox styles may use SDF icons, `mapbox://` sprite/glyph URLs, or custom
  layer types that MapLibre does not implement. If something does not draw,
  check `read_console_messages` for the first missing resource; a style built
  on Mapbox Streets v12 usually works out of the box, a custom Studio style may
  not.
* The Mapbox access token in `config.js` is served by the API and visible to
  every page viewer, so it MUST be a domain-restricted "URL referrer"
  token - the Mapbox dashboard sets this; the token SHOULD NOT be a
  secret-key (`sk.*`) token. `config.js` itself stays outside the git repo on
  operator machines (add it to `.gitignore` in your checkout).
* Mapbox counts map loads against the account monthly quota. For race-day use
  consider pre-caching tiles or switching back to the MapTiler / OpenFreeMap
  style on the race convoy.

### Tile source and licence in use by default

MapLibre demo tiles (`https://demotiles.maplibre.org/style.json`): a coarse world
vector layer built from **Natural Earth** (public domain) with MapLibre's style.
It is real geography (country polygons, coastline, graticule) but **not road
level**, and it contains no OpenStreetMap data, so no OSM credit is required with
this style. Any OSM-derived style (MapTiler Streets, OpenFreeMap, ...) **does**
require "(c) OpenStreetMap contributors"; set `requiresOsmCredit: true`. The route
geometry itself is OpenStreetMap data served by the API (see its
`source_description`). The BoM observation licence text comes from the API
provenance and is shown in value hover text.

If the style cannot be loaded, the map switches to a plain background and says
**BASEMAP UNAVAILABLE**; route, stops and vehicle are still drawn.

## Views

`#/command` Race Command Center, `#/map` Live Weather Map, `#/solar` Solar
Intelligence, `#/forecast` Forecast Center, `#/route` Route Intelligence,
`#/strategy` Energy & Strategy, `#/alerts` Alerts & Risk, `#/history` Historical
Analysis. Light/dark theme (dark default, persisted in localStorage inside
try/catch) and a Local/UTC switch for chart axes in the top bar.

## Layout

```
index.html        shell, vendored library tags (SRI), boot watchdog
vendor/           pinned MapLibre + uPlot builds (see vendor/README.md)
config.js         user configuration
css/              base (tokens, themes, chrome), components, views
js/api.js         the only module that knows endpoints, timeouts, error mapping; JSDoc typedefs
js/store.js       resources (data/status/error/last-good time), derived facts, prefs
js/loaders.js     which API call feeds which store key, built from the ACTIVE position only
js/format.js      units, numbers, ages, local/UTC time, provenance text, freshness
js/ui.js          shared components (chips, value cells, panels, error/unknown states)
js/charts.js      uPlot wrappers (observed/forecast boundary, night shading)
js/main.js        router, header, banners, footer, polling
js/views/*.js     one module per view
```

## Endpoints consumed

`GET /api/health`, `GET /api/state`, `POST /api/state/position`,
`POST /api/state/time`, `GET /api/solar/position`, `GET /api/solar/day`,
`GET /api/solar/clearsky/day`, `GET /api/weather/forecast`,
`GET /api/weather/observable`, `GET /api/weather/observations`, `GET /api/route`, `GET /api/route/{id}/geojson`,
`GET /api/route/{id}/progress`, `GET /api/route/{id}/trajectory`,
`GET /api/route/{id}/weather`, `GET /api/energy/forecast`,
`GET /api/strategy/scenarios`, `GET /api/regulations`, `GET /api/alerts`,
`GET /api/forecast/archive`, `GET /api/forecast/verification`, `GET /api/forecast/ensemble`,
`GET /api/forecast/models`, `POST /api/forecast/capture` (operator button, History view),
`GET /api/ml/bands` (experimental, Forecast view; shows "no model" until one is trained),
`GET /api/meteorologist/morning-briefing`, `GET /api/meteorologist/risk-ahead`,
`GET /api/meteorologist/forecast-reliability`, `POST /api/meteorologist/ask` (Meteo view;
`ask` answers 501 until `RWI_ANTHROPIC_API_KEY` is set).

Not called: `/api/solar/clearsky` (single instant), `/api/weather/variables`,
`/api/energy/array`, `/api/strategy/vehicle`, `/api/route/{id}` (full route).

Shapes follow `docs/CONTRACTS.md` section 4 as implemented (`/api/state` is
`{state, timezone_name, local_time, is_simulated, ...}`; `/api/route` returns counts, so
control stops come from the route GeoJSON marker features; `/api/route/{id}/weather` is
`{forecast, local_arrival_times, notes}`; `/api/energy/forecast` is `{estimates, energy,
array, notes, ...}`). `/api/solar/day` and `/clearsky/day` take `time`; the UI sends
`date` as well. Errors arrive as `{error}`, `{detail:{error}}` or `{detail}` (`api.extractError`).

Observation requests are built from `GET /api/weather/observable`
(`loaders.loadObservations`): only variables some provider MEASURES are requested. An
empty map means the platform measures nothing, and no request is made (the UI says so).
Where the nearest station is too far, the backend answers 503 and the UI shows its message.

Route progress comes from `state.route_distance_m` (the backend projects each fix onto the
route); `/api/route/{id}/progress` is used for cross-track and as a fallback. The
trajectory is truncated by the API to the requested horizon and the UI says how many points
were dropped. Route weather covers at most 12 points per request by design, so other rows
read "not sampled".

## Honesty rules and where they are enforced

| Rule | Enforced by |
|---|---|
| Every number shows its unit | `format.fmtVar`/`VARS` (unit registry), `ui.valueCell`, `ui.numCell` (unit is a required argument) |
| Provenance on hover or in a detail row | `format.provenanceLines` / `provenanceTitle` (provider, dataset, model, run time, valid/observation time, retrieval age, location, licence, quality); applied by `ui.valueCell`, `ui.numCell`; visible summary by `ui.provFooter` |
| OBSERVED vs FORECAST vs MODEL visually distinct | `format.kindInfo` (vocabulary) + `ui.kindChip` (solid / dashed / dotted / double border plus colour, survives greyscale); `charts.makeChart` (observed = solid with markers, forecast = dashed); `views/command.weatherPanel` keeps observed and forecast in separate columns; `seriesutil.pointsOf` never merges two series |
| Observed/forecast boundary on charts | `charts.makeChart` `draw` hook (dashed rule at the effective time, labelled both sides; "PAST/FUTURE" when no observations exist) |
| Clear-sky is a model upper bound, never measured | `format.kindInfo("clear_sky_model")` label "CLEAR-SKY MODEL - UPPER BOUND"; banner in `views/solar.irradiancePanel`; dotted amber filled style in `charts.makeChart` (kind `clear`) with series names containing "MODEL BOUND" |
| Stale data is marked with its age | `format.freshnessOf` / `freshnessText` (limit from `/api/health` `freshness_limits_s` via `store.absorbLimits`, else the labelled UI default only while health is unread; API quality flag `stale` always wins); `ui.valueCell` (STALE chip + wavy underline); `ui.ageSpan` + `ui.startTicker` (live age that flips to STALE with the limit shown); `format.fixFreshness` + `main.renderBanners` (STALE POSITION banner) |
| Position unknown: say so, no marker, no default | `store.activePoint` (null without a fix), `loaders.*` (`store.block` instead of requesting), `ui.positionUnknown`, `views/map.refresh` (vehicle and accuracy sources are set to an empty collection), `store.vehicleTimezone` (no timezone without a fix) |
| SIMULATION banner in route_sim / historical_replay | `store.isSimulation` + `main.renderBanners` (rebuilt from state on every render; there is no close control) |
| Missing values as an explicit dash with a reason | `format.DASH`, `ui.valueCell` (reason from quality flag or "not provided by this source"), `ui.numCell`, `ui.textCell`; `route.js` shows "not sampled" and "grade unknown" (when `grade_known` is false) instead of 0 |
| API unreachable: endpoint, error, last good age | `api.ApiError`, `ui.errorPanel`, `ui.withResource` (renders last good data under "LAST LOADED n ago - NOT current"), `main.renderBanners` (API ERROR banner), `main.renderStatus` |
| No fabricated data | There is no demo mode and no sample data in `web/`. Operator inputs (speed, start distance, start SoC) are labelled as operator parameters or assumptions (`views/route`, `views/strategy`) |
| Head/tail wind is not confused with apparent wind | `format.trueHeadwind` (true wind along track); the API's `relative_wind.headwind_mps` (includes vehicle speed) is shown separately as "Apparent head" |
| Assumed vehicle/array parameters are flagged as illustrative | `views/strategy.contextPanel` (ASSUMED chip, "not predictions for the team's car"), `views/command.solarPanel` and `views/solar.powerPanel` (array parameter_source) |
| Blocking violation cannot be mistaken for a warning | `views/strategy.scenariosPanel`: solid red BLOCKING chip, red row and left rule, INFEASIBLE chip, blocking count in the row; warnings use a yellow WARNING chip |
| Verification score never bare; provisional below 30 pairs | `forecastquality.scoreCell` (MAE with unit, `n =`, PROVISIONAL chip), `forecastquality.isProvisional` (`MIN_SAMPLES = 30`) |
| Negative skill reads as worse than "nothing changes" | `forecastquality.skillNode` (WORSE THAN PERSISTENCE chip with signed value) |
| Zero scores is a stated state, notes always shown, pair diagnostics explained | `forecastquality.verificationPanel`, `forecastquality.pairsExplanation` (waiting / no run time / leakage guard, from `coverage.pairs`) |
| Ensemble caveat always visible; wind direction has no band | `forecastquality.ensemblePanel` (caveat box; circular-SD line when percentiles are empty) |
| Preferred model never defaulted | `forecastquality.modelsPanel` (shows NONE plus `preference_basis`) |
| Provider outage is not an empty chart | `ui.errorPanel` (PROVIDER UNAVAILABLE / BACKEND ERROR), `ui.withResource` (LAST LOADED age + stale data), `forecastquality.outageNote`, `forecast.chartPanel` empty text |
| Archive states never merged | `history.archivePanel` (endpoint failed / `available:false` / empty / populated) |
| Capture makes real calls | `history.capturePanel` (warning text, confirm dialog) |
| Regulations cited with clauses; unpublished items shown | `views/strategy.regulationsPanel` and `views/alerts.regulationsNote` from `/api/regulations` (class limits with the clause string the API supplies, "not yet published", "silent on"). The UI never states a limit without data from that endpoint |
| Data gaps and regulatory notes rendered | `views/strategy.contextPanel` (`data_gaps`, `regulatory_notes`, `human_review_required`) |
| Defaults the backend applied are shown, not guessed | `views/strategy.contextPanel` "Inputs applied by the backend" renders the echoed `assumptions` (`ECHO` list); `views/command.energyPanel` labels start SoC "MODEL INPUT" |
| Scenario output is a MODEL, not a simulation | `ui.kindChip` renders the backend's provenance kind; scenarios and the recommendation now arrive as `physical_model` and show a MODEL chip |

## Placeholders pending backend endpoints

* **Historical Analysis**: no archive, session or verification endpoint exists, so
  the view is an explicit empty state plus health/uptime.
* **Forecast verification**: live, but with zero scoreable pairs it shows the archive diagnostics instead of numbers.
* **Model comparison**: compares providers (from `/api/health`) only; model-level
  comparison inside one provider is not exposed.
* **Battery state of charge now**: no telemetry endpoint (by design for now); shown as
  unavailable. Scenario start SoC is shown as a model input echoed by the backend.
* `/api/alerts` carries weather and data-integrity alerts only (by design); strategy
  alerts such as "no feasible scenario" appear in Energy & Strategy.
* **Next-control-stop ETA**: the Command Center gives distance only; ETA is in
  the Route view at an assumed speed.

## Verification

Verified in the browser (Browser pane, 1280x900) against the real backend
(`rwi.api.app` on a local port, live Open-Meteo and BoM) and, for failure states,
against a contract-shaped mock kept outside the repository:

* all eight views render and switch with no uncaught JavaScript errors
  (checked with an error collector); the only console errors are the browser's
  own log of the API's 501/503 answers for observations, which the UI handles;
* MapLibre tiles render (Natural Earth land, coastline, Tropic of Capricorn,
  "Australia" label), with the real BWSC route, nine named control stops, the
  vehicle marker, route weather samples and wind barbs; hover readout works;
* Command Center against live data: position, ACST local time and UTC,
  projected route progress, next control stop, observed (real BoM, marked STALE
  at 31 min) beside forecast values;
* no position: "POSITION UNKNOWN" in every panel, no vehicle marker, local time
  withheld; position loaded from the operator form; SIMULATION banner (route_sim);
  STALE POSITION banner; API unreachable (`?api=` to a dead port): error panels with
  endpoint and error, header chip, banner; observations 501 and 503 states;
* freshness limits read from `/api/health` (footer shows "(/api/health)"); observation
  request built from `/api/weather/observable`; a fresh real BoM observation (13 min old
  at Alice Springs) rendered as OBSERVED and not stale, while a position 232 km from any
  station produced the backend's 503 message; route progress from state; trajectory
  truncated at 4 h with the dropped-points note; recommendation chip is MODEL;
* strategy: four infeasible scenarios with blocking violations, data gaps,
  regulatory notes, assumed-vehicle warning, regulation context; light theme
  persists across reload; layout has no horizontal page overflow at 1280 and 1360.

Could not verify:

* Real-time position updates in `route_sim` (position was set by POST; the
  backend has no simulator advancing it, so movement and polling under motion were
  not observed).
* Live GPS: no telemetry source. A POSTed fix does not refresh itself, so
  freshness was exercised with a test loop re-posting the fix.
* Mobile/tablet widths (desktop-first; minimum width 1200 px).
* MapTiler or any other style (only the default demo style was loaded).
* Any browser other than the Browser pane's Chromium. The pane throttles
  rendering, so map tiles took about 10 to 15 s to appear.
* Printing, accessibility audit, long-session memory use.

Phase 2 verification: ensemble fan chart (real ECMWF IFS members), verification panel in its
zero-evidence state with the real pair diagnostics, History archive panel, and the Forecast
Center during a real Open-Meteo 503 (PROVIDER UNAVAILABLE). Verification scores, the 503 on the
ensemble endpoint and the capture result were exercised with canned client-side responses (the
live backend has no scores, and a real capture was not run because it writes to the shared
archive). `/api/forecast/models` returned HTTP 500 with no body on the live API (backend raises on an
empty model set); the UI renders it as BACKEND ERROR.
