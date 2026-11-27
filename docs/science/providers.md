# Weather providers and quality control (`rwi.providers`, `rwi.quality`)

Owner: providers. Code: `src/rwi/providers/`, `src/rwi/quality/`. Tests: `tests/unit/test_provider_*.py`,
`tests/unit/test_quality_*.py`, `tests/integration/test_provider_live.py`. Fixtures: `tests/fixtures/`.

Everything below marked **VERIFIED** was read from the named page or measured with a live call on
**2026-11-01** (the date of this document). Anything I could not verify is marked **NOT VERIFIED**.
Licence, cost and access-method summaries by the documentation engineer are in
`docs/research/data-sources.md`; this file adds the technical facts (variables, resolution, latency,
what the APIs do not tell us) and does not repeat the prose of that file.

Times are UTC, units are the canonical units of `rwi.core.variables` (Pa, m/s, W/m2, degC, ...).

---

## 1. Providers evaluated

Latency = wall-clock time of one `GET` from the development machine (location not recorded, so
treat as order of magnitude): first call includes DNS+TLS, "warm" is a re-used connection.
Update interval = how often the source publishes new data. Horizon = how far ahead it forecasts.

| Provider / dataset | Latency (VERIFIED) | Spatial / temporal resolution | Update interval | Horizon | Variables | Rate limit | Cost | Licence and attribution | Verified from |
|---|---|---|---|---|---|---|---|---|---|
| **Open-Meteo forecast** `api.open-meteo.com/v1/forecast`, `models=best_match` (default blend) | 220 ms cold, 43-44 ms warm (5 calls) | Per variable and hour the highest-resolution available of ICON (2-11 km), GFS (3-25 km), IFS (9-25 km); "Everywhere else: global models only" - Australia gets the global models. **No single resolution.** Hourly output. | Per model: ICON 3 h, GFS 1 h, IFS/AIFS 6 h | API accepted `end_hour` up to the end of UTC day today+15 d (16 d); `start_hour` back to today-93 d | 25 canonical variables accepted; for Darwin/48 h none returned null | Free: <10,000/day, 5,000/h, 600/min, 300,000/month; weighted (see sec. 1.1) | Free, non-commercial only; paid plans (commercial use, `customer-api` host + key), price not shown on the page | Data CC BY 4.0; link to open-meteo.com next to displayed data; indicate changes | <https://open-meteo.com/en/docs>, `/terms`, `/licence`, `/pricing`; live calls |
| **Open-Meteo forecast, single model** (`ecmwf_ifs025`, `ecmwf_ifs`, `ecmwf_aifs025_single`) | same host, 43-44 ms warm | `ecmwf_ifs` 9 km hourly; `ecmwf_ifs025` 25 km, **native 3-hourly (interpolated to hourly)**; `ecmwf_aifs025_single` 25 km native 6-hourly | 6 h (metadata `update_interval_seconds=21600`) | 15 d (docs table) | see null table in sec. 1.2 | as above | as above | as above | docs table; `/data/<model>/static/meta.json` |
| **Open-Meteo archive** `archive-api.open-meteo.com/v1/archive` | 197 ms cold, 42-44 ms warm | `ecmwf_ifs` 9 km hourly (2017-present, "every 6 hours with no delay"); `era5` 0.25 deg hourly (1940-present, **5-day delay** per the docs - the last 30 hours I queried returned `null` on 2026-11-01); `era5_land` 0.1 deg | IFS: 6 h; ERA5: daily | past only; `start_date`/`end_date` after today (UTC) rejected with HTTP 400 | all 25 names accepted; ERA5 returns null for precipitation_probability, uv_index, visibility, soil_temperature_0cm; IFS null for uv_index | same pool as forecast | as above | as above | <https://open-meteo.com/en/docs/historical-weather-api>; live calls |
| **Open-Meteo ensemble** `ensemble-api.open-meteo.com/v1/ensemble` | see sec. 1.1 (not measured: quota exhausted by my own probing, then fixture captured after the reset) | `ecmwf_ifs025`: 0.25 deg, 3-hourly, 51 members (control + 50); `gfs025`: 0.25 deg, 3-hourly, 31 members (control + 30); ICON-EPS 40 members hourly | IFS/GFS: 6 h; ICON-EPS 12 h | IFS 15 d, GFS 10 d, ICON-EPS 7.5 d; **history only ~3 days** | variables accepted; ifs025 null: precipitation_probability, uv_index, visibility; gfs025 null: cloud_cover_low/mid/high, precipitation_probability, uv_index, soil_temperature_0cm | **Separate daily pool, heavily weighted** (sec. 1.1) | as above | as above | <https://open-meteo.com/en/docs/ensemble-api>; live calls |
| **Open-Meteo `bom_access_global`** (BoM ACCESS-G via Open-Meteo) | n/a | docs table: 15 km, 10 d, every 6 h | **dead**: every value returned as `null` on 2026-11-01. Re-checked 2026-11-27: `last_run_initialisation_time` = 2025-06-26 18:00 UTC, `data_end_time` = 2025-06-30 07:00 UTC (ensemble `bom_access_global_ensemble`: same run, data end 2025-07-07 03:00 UTC). Rejected in ADR 0013; the adapter now refuses a model whose `data_end_time` is before the requested window (`ProviderUnavailableError`, "stale") | - | - | - | - | BoM CC BY (licence page) | `/data/bom_access_global/static/meta.json`, `/data/bom_access_global_ensemble/static/meta.json`; live calls (fixtures `openmeteo_forecast_all_null_bom_access.json`, `openmeteo_meta_bom_access_global*.json`) |
| **BoM station observations, 72 h history** `www.bom.gov.au/fwo/IDx60910/IDx60910.<WMO>.json` (product bundle IDB00011) | 116 ms cold, 49-51 ms warm | Point station measurements, **half-hourly** (some synop sites 3-hourly) | "every half hour, at approximately x:05 and x:35" (user guide v3.2) | observations: last ~72 h | air_temp, apparent_t, dewpt, rel_hum, press_msl/press/press_qnh, wind_spd_kmh, gust_kmh, wind_dir_deg, cloud_oktas, vis_km, rain_trace/rain_ten/rain_hour, weather; **no irradiance, no forecast** | none stated; "no guarantee of availability" | Free anonymously; **not for commercial use** | (c) Commonwealth of Australia; free anonymous products: personal use or use within your organisation, **may not be supplied to others**; publishing needs Registered User / data licence | <https://www.bom.gov.au/catalogue/data-feeds.shtml>, <https://www.bom.gov.au/other/copyright.shtml>, <https://www.bom.gov.au/catalogue/72_hr_historical_obs.pdf>, <https://www.bom.gov.au/catalogue/anon-ftp.shtml> |
| **BoM anonymous FTP** `ftp://ftp.bom.gov.au/anon/gen/fwo/` | listing fetched OK | `IDD60910.tgz` (all NT stations, 72 h), `IDD60920.xml` (state obs), same for `IDS` | `IDD60920.xml` was 1 min old in the 22:20 UTC listing; BoM's guide for it is titled "10-minute updates" | obs only | as above | not stated | free, not for commercial use | same as above | live FTP listing |
| **BoM Registered User services** (real-time data; forecast grids, radar, satellite, geospatial) | not accessible without registration | not verified | not verified | NWP forecast grids exist per the page | not verified | n/a | **paid** ("Subscribe to become a registered user ... This paid service"); fee schedule at `reg.bom.gov.au/climate/data-services/charges.shtml` - **NOT READ by me** | BoM Data Licence Agreement (PDF `bureau-of-meteorology-data-licence-agreement-october-2026.pdf`, **NOT READ by me**) | <https://www.bom.gov.au/resources/data-services> |

### 1.1 Rate limits as actually observed

* `429 Too Many Requests` response from `ensemble-api.open-meteo.com` captured on 2026-11-01 22:25 UTC
  (fixture `openmeteo_429_daily_limit.json`): body `{"error":true,"reason":"Daily API request limit
  exceeded. Please try again tomorrow."}`, headers `X-RateLimit-Limit: 10000.0`,
  `X-RateLimit-Remaining: 0.0`, `X-RateLimit-Reset: 1793577617` (2026-11-02 00:00:17 UTC),
  `X-RateLimit-Window: 86400`, `Retry-After: 5693`.
* I triggered this myself with about 55 ensemble requests (mostly one variable and 2 hours each, 31 or 51
  members): 55 requests cannot reach 10,000 if each counts 1, so the counter is evidently **weighted by
  data volume** (members x variables x hours) - or the pool was already partly used by someone else on the same IP.
  Weighting is not stated on the pages I read and I did not establish the formula. Budget ensemble calls conservatively and cache them (the adapter
  does; TTL 900 s).
* The forecast host kept answering normally while the ensemble host was exhausted: **the pools are per
  host**.
* `Retry-After` of 5693 s is longer than any sensible wait, so the adapter raises
  `ProviderRateLimitError` immediately when `Retry-After` exceeds `max_retry_after_s` (60 s) instead of
  sleeping through its retries.
* The terms page lists the limits per IP *and application*; shared-IP hosts (Vercel, Netlify, Render,
  Google Sheets) may be limited sooner. A cloud deployment of the dashboard should be checked.

### 1.2 Variables a model returns as `null` (Darwin, 48 h, 2026-11-01)

`best_match`: none. `ecmwf_ifs`: uv_index. `ecmwf_ifs025`, `icon_global`: uv_index, visibility.
`gfs_global`: soil_temperature_0cm. `ecmwf_aifs025_single`: wind_gusts_10m, precipitation_probability,
uv_index, visibility. Nulls become `MISSING` with `value=None` (never zero). `capabilities.variables`
means "accepted by the API", not "every model fills it".

### 1.3 Not evaluated

No facts are claimed for: MET Norway, NOAA GFS/NOMADS direct, ECMWF open data (GRIB; see
`docs/research/data-sources.md`), DWD open data, Himawari satellite irradiance, Solcast, Visual Crossing,
Tomorrow.io, Meteomatics, OpenWeatherMap, Meteostat. They were out of scope for this task.

---

## 2. What each adapter does

### 2.1 Open-Meteo (`OpenMeteoProvider`, provider name `open-meteo`)

| Call | Returns | Endpoint |
|---|---|---|
| `fetch_forecast(point, window, variables, *, models=None)` | `WeatherSeries`, `kind=FORECAST` | `/v1/forecast` |
| `fetch_ensemble(point, window, variables, *, models=None)` | `EnsembleForecast`, one `WeatherSeries` per member (`dataset="ensemble/control"`, `"ensemble/member07"`, ...), each `kind=FORECAST` | `/v1/ensemble` |
| `fetch_analysis(point, window, variables, *, models=None)` | `WeatherSeries`, `kind=ANALYSIS` (not in the `WeatherProvider` protocol) | `/v1/archive` |
| `fetch_observations(...)` | **always raises `CapabilityNotSupportedError`** | - |

**Decision on observations.** Open-Meteo serves numerical-model output; nothing it returns is a
measurement. The task allowed "expose as ANALYSIS or raise"; I chose *raise* for
`fetch_observations` and offer the model-derived past through a separately named `fetch_analysis`
with `kind=ANALYSIS`. Reason: a caller (or `fetch_with_fallback(kind=OBSERVATION)`) asking for
"what is measured" must get an error rather than a model value that merely carries a different label.
`fetch_analysis` defaults to `ecmwf_ifs` (9 km, available up to now). `era5` is the true reanalysis
but lags 5 days. Past steps requested from `fetch_forecast` stay `kind=FORECAST` (they are short-lead
model output for a past time; for Darwin on 2026-11-01 they were numerically identical to the
`ecmwf_ifs` archive).

**Request.** `timezone=UTC` always; `start_hour`/`end_hour` (forecast, ensemble) or
`start_date`/`end_date` (archive); `hourly=<api names>`; `models=<one model>`; no `wind_speed_unit`
(default km/h). The response is rejected unless `utc_offset_seconds == 0`.

**Window semantics.** The series contains every native hourly step from `floor_hour(window.start)` to
`ceil_hour(window.end)` inclusive, i.e. the steps *bracketing* the window, so a caller can interpolate
at any instant inside it. Nothing is interpolated by the adapter.

**Name and unit mapping** (the conversion is chosen from the unit string the API reports in
`hourly_units`; an unrecognised unit raises `ProviderUnavailableError` instead of guessing):

| Canonical | Open-Meteo name | API unit | Conversion |
|---|---|---|---|
| `ghi` / `dni` / `dhi` | `shortwave_radiation` / `direct_normal_irradiance` / `diffuse_radiation` (`*_instant` with `radiation_mode="instant"`) | W/m2 | none |
| `pressure_msl`, `surface_pressure` | same | hPa | x100 -> Pa |
| `wind_speed_10m`, `wind_gusts_10m` | same | km/h (m/s accepted if reported) | /3.6 -> m/s |
| `temperature_2m`, `dew_point_2m`, `apparent_temperature`, `soil_temperature_0cm` | same | degC | none |
| `relative_humidity_2m`, `cloud_cover*`, `precipitation_probability` | same | % | none |
| `precipitation`, `rain` | same | mm | none |
| `wind_direction_10m` | same | deg | none |
| `visibility` | same | m | none |
| `is_day`, `weather_code`, `uv_index`, `terrestrial_radiation` | same | -, wmo code, -, W/m2 | none |

A converted value outside the registry range is kept but flagged `OUT_OF_RANGE` (so it is not
usable); `null` -> `MISSING`.

**Time semantics (important, easy to get wrong).** From the docs, VERIFIED: radiation
(`ghi`, `dni`, `dhi`, `terrestrial_radiation`) is the **mean of the preceding hour**; `precipitation`
and `rain` are **preceding-hour sums**; `wind_gusts_10m` is the **preceding-hour maximum**; everything
else is instantaneous. So a `ghi` stamped 03:00 describes 02:00-03:00. This matters for comparing with
instantaneous solar geometry (`rwi.solar`): either integrate with the stamp as the interval end, or
construct the provider with `radiation_mode="instant"` to request `*_instant` (VERIFIED to exist and
to differ: 180.7 W/m2 instant vs 107 W/m2 hourly mean at 22:00 UTC on 2026-11-01). The
note is written into every value's `provenance.notes`.

**Grid cell vs requested point.** Open-Meteo snaps to a grid cell ("land" selection with 90 m DEM
statistical downscaling): asking for (-12.4634, 130.8456) returned (-12.4077, 130.8574) for
best_match and (-12.5, 130.75) for `ecmwf_ifs025`, i.e. up to ~11 km away. `WeatherSeries.point` is the returned
cell (with the DEM `elevation`), `requested_point` is the caller's point; both are also on every
value's provenance. The returned `elevation` is the 90 m DEM elevation of the requested location,
**not** the elevation of the model grid cell.

**Model init time.** See sec. 4.

**Licence / attribution** are in `ProviderCapabilities.license` and `.attribution` and in every
value's provenance.

### 2.2 BoM (`BomProvider`, provider name `bom`)

Genuinely usable today: **station observations** from the 72-hour half-hourly JSON feed, `kind=OBSERVATION`
(`observation_time_utc` from the feed's `aifstime_utc`). `fetch_forecast` raises
`CapabilityNotSupportedError`.

* URL: `{RWI_BOM_FTP_BASE}/{IDx60910}/{IDx60910}.{WMO}.json`, e.g.
  `https://www.bom.gov.au/fwo/IDD60910/IDD60910.94120.json`. The configured default is `http://...`,
  which BoM answers with a 301 to `https://`; the client follows redirects. (The setting is called
  `bom_ftp_base` but this is HTTP, not FTP.)
* Stations configured (all fetched successfully 2026-11-01; coordinates/heights from the state XML
  `IDD60920.xml` / `IDS60920.xml`): Darwin Airport 94120, Daly Waters 94234, Tennant Creek Airport 94238,
  Alice Springs Airport 94326, Coober Pedy Airport 95458, Woomera 94659, Port Augusta 95666,
  Adelaide West Terrace 94648, Adelaide Airport 94146.
  **Not found in either state XML:** Katherine, Dunmarra, Barrow Creek, Kulgera, Glendambo. Along those
  stretches there is no BoM point observation to use.
* The adapter picks the nearest configured station and **refuses** (`CapabilityNotSupportedError`) if it
  is more than `max_station_distance_km` (default 100 km) away, rather than present a distant
  station as "weather at this point". Distance is in every value's provenance notes. Between Tennant Creek and
  Alice Springs (~460 km) and between Alice Springs and Coober Pedy (~600 km) most of the route is therefore uncovered.
* Variables mapped: temperature_2m, apparent_temperature, dew_point_2m, relative_humidity_2m,
  pressure_msl, wind_speed_10m, wind_gusts_10m, wind_direction_10m, cloud_cover (oktas x 12.5; oktas 9 =
  obscured -> missing), visibility (km -> m). Calm direction (`wind_dir == "CALM"`, which the feed prints
  as 0) -> missing; "-" -> missing.
  **Deliberately not mapped:** rain (`rain_trace` is "since 9 am", `rain_ten`/`rain_hour` are overlapping
  windows), `press`/`press_qnh` (MSLP or QNH, not station pressure), surface_pressure, irradiance (not in
  the feed at all).
* Wind averaging depends on the source message (`wind_src`): 1-minute mean (`OMD`) or 10-minute mean
  (`metar_10`, `synop`); gust = highest 3-second mean in that period; no gust for `synop`/`mdf`. `wind_src`
  is recorded per value.
* The feed publishes **no QC status**, no sensor heights (10 m for wind is the WMO standard but is
  **not stated** in the documents I read) and no instrument accuracy.
* Spot check (not a validation): Darwin Airport observations vs the `ecmwf_ifs` archive over 61
  matching hours (2026-10-30 to 2026-11-01): temperature bias -0.58 K (MAE 1.03 K), dew point bias -0.80 K,
  mean wind bias -0.80 m/s (MAE 1.02 m/s), gust bias +1.60 m/s, MSL pressure bias -17 Pa (MAE 23 Pa). One
  station, ~2.5 days, wet-season onset: it only shows the unit conversions and time alignment are sane.

#### What the team must obtain from BoM (action list)

1. **Decide the licence route before anything is displayed outside the team.** The free anonymous
   products are for "personal use, or use within your organisation"; they may not be supplied to
   any other person or used commercially ([copyright notice](https://www.bom.gov.au/other/copyright.shtml),
   [data feeds](https://www.bom.gov.au/catalogue/data-feeds.shtml)). A dashboard shown to sponsors, the public
   or other teams is "supplying". The adapter is fit for the strategists' own use only.
   `ProviderCapabilities.license/notes` and every value's provenance say so.
2. **Registered User access** for (a) the supported, guaranteed feed of the product
   **"Observations - Australia - 72-hour history", bundle `IDB00011`** (state products `IDD60910`,
   `IDS60910`; cloud (S)FTP, support and change notices - anonymous users cannot be told about service changes) and
   (b) any right to publish. Contact `webreg@bom.gov.au` (real-time data). Fees: <https://reg.bom.gov.au/climate/data-services/charges.shtml>
   (I did **not** read this page; `docs/research/data-sources.md` quotes AUD 1,271 establishment + AUD 1,220/yr
   from a search extract - unverified).
3. **Licence agreement:** "Bureau of Meteorology Data Licence Agreement" (PDF linked from
   <https://www.bom.gov.au/resources/data-services>). I did not read it.
4. **NWP / forecast grids:** the data-services page lists "weather forecast grids", radar and satellite
   under the *paid* real-time Registered User services. I could not identify product codes or formats from
   public pages. Ask `webreg@bom.gov.au` which product delivers ACCESS-G/ACCESS-C grids and what a
   student/non-profit team pays. Do not expect this through Open-Meteo (see the dead `bom_access_global` row).
5. **Historical observations** for later verification: `climatedata@bom.gov.au` (named in the user guide).
6. Optional: read the user guide's Appendix list and request the stations **Katherine, Barrow Creek,
   Kulgera, Glendambo** if BoM operates AWS there that are not in the public product.

Access note: BoM web pages returned HTTP 403 to automated clients in the documentation engineer's
attempt; with a plain, honest `User-Agent` (`jst-rwi/0.1 (...)`) they returned 200 for me on
2026-11-01. I did not use any browser-impersonating header and the adapter identifies itself.

---

## 3. Fallback policy (`registry.fetch_with_fallback`)

1. `available_providers(settings)` returns `{"open-meteo": ..., "bom": ...}` in priority order.
2. For a request of `kind`, providers are tried in that order. A provider is **skipped without a
   request** when its declared capabilities do not cover it: it lacks that kind (observations / forecast /
   archive), lacks any requested variable, or the requested horizon exceeds `max_forecast_hours` (+24 h
   slack because the Open-Meteo horizon is counted from 00:00 UTC).
3. A provider **fails** if it raises `ProviderError` (unavailable, rate limited, unsupported), returns a
   series whose `kind` differs from the one requested (rejected, logged), or returns no usable value for
   any requested variable.
4. The first usable series is returned unchanged except that every value's `provenance.notes` gains
   `Fallback: <provider>: skipped/failed (<reason>); ...` for each provider that did not serve. (There is no
   `notes` field on `WeatherSeries`; `fetch_with_fallback_detailed` also returns the structured
   `FallbackAttempt` list.)
5. **Kind is never changed.** OBSERVATION never degrades to FORECAST/ANALYSIS and vice versa.
6. Nothing succeeded: `ProviderUnavailableError` listing every attempt; or `CapabilityNotSupportedError`
   when no provider was eligible at all (nothing was tried) - the API layer should map the first to 503 and the second to
   501.
7. Fusion of sources is not done here (ADR 0004).

Consequence today: forecast -> Open-Meteo only; observation -> BoM only (within 100 km of a configured
station); analysis -> Open-Meteo only. There is no second forecast provider, so "fallback" for forecasts
means "serve cache or fail".

---

## 4. Metadata the APIs do NOT expose

### 4.1 Open-Meteo

The forecast, archive and ensemble responses contain exactly: `latitude`, `longitude`, `elevation`,
`generationtime_ms`, `utc_offset_seconds`, `timezone`, `timezone_abbreviation`, `hourly_units`, `hourly`
(VERIFIED by inspecting live responses; the docs list the same set). Therefore **not available in the response**:

* **Model run / initialisation time.** `generationtime_ms` is server compute time, not run time. This is
  the most important gap.
* Which model supplied each value under `best_match` (a per-variable, per-hour blend of ICON, GFS, IFS):
  no model attribution per value, no resolution.
* Whether an hourly value was interpolated from a 3-hourly or 6-hourly native step.
* Grid-cell elevation (the returned `elevation` is the 90 m DEM elevation of the request).
* Any uncertainty or quality information (except the ensemble spread, which we compute from members).
* Licence/attribution inside the payload (it is on the website only).

**What we do about the init time** (`model_init_time_utc`):

* For `models=best_match` and any multi-source selection: **always `None`**, with a note.
* For a single model with a known metadata directory (`ecmwf_ifs`, `ecmwf_ifs025`,
  `ecmwf_aifs025_single`; ensembles `ecmwf_ifs025` -> `ecmwf_ifs025_ensemble`, `gfs025` -> `ncep_gefs025`)
  the adapter reads Open-Meteo's separately documented *Metadata API*
  `https://api.open-meteo.com/data/<dir>/static/meta.json` ([documented here](https://open-meteo.com/en/docs/model-updates),
  not counted against API limits) *after* the data call and uses `last_run_initialisation_time` **only if**
  `last_run_availability_time` is at least 10 minutes older than that check (Open-Meteo's own advice: servers are
  eventually consistent, wait 10 min). Otherwise `None` with a note. Archive data: `None` always.
* This is a **model-level** fact ("latest published run"), not a statement made by the data response.
  It can be wrong if the response was served from the previous run; the 10-minute rule reduces but does not
  eliminate that. Status: implemented, not validated (sec. 6).
* The exact, auditable alternative is the **Single Runs API** (`single-runs-api.open-meteo.com/v1/forecast?...&run=2026-11-01T12:00`),
  which returns one named run (VERIFIED responds on the free host; ECMWF IFS from March 2024, other models from
  2026-04-02; it returned `elevation 0.0`, i.e. no DEM downscaling, so values differ from the normal endpoint).
  Not implemented; recommended if lead-time-aware verification becomes a requirement.
* Data latency observed in the metadata on 2026-11-01 (one snapshot, init -> available on the API):
  `ecmwf_ifs` 12 UTC run 6.9 h; `ecmwf_ifs025` 7.8 h; `ecmwf_aifs025_single` 5.6 h; `ncep_gfs013` 5.5 h;
  `ecmwf_ifs025_ensemble` (06 UTC run) 9.5 h; `ncep_gefs025` 5.7 h. So a forecast fetched at 22:30 UTC was
  already 10.5 h past its 12 UTC initialisation (lead time at the window start = 10 h). Lead time must
  be tracked (`Provenance.lead_time_s`), not assumed zero.

### 4.2 BoM observation JSON

Not exposed: QC status/flags, instrument type and accuracy, sensor heights, station elevation (taken from the state XML),
whether a value is a one-second sample or a mean beyond the `wind_src` description, and any licence field beyond
the copyright notice URL. Also: rain accumulations whose windows differ per field; observation time zone is given
(`aifstime_utc`, `aifstime_local`), which is why UTC parsing is unambiguous.

---

## 5. Caching and freshness policy

* **Cache**: `settings.cache_dir/<provider>/<sha256>.json`, one file per request, written atomically.
  Key = (provider, dataset, latitude and longitude **rounded to 0.01 deg (~1.1 km)**, window start/end
  (hour-aligned by the adapter), sorted variables, model name, radiation mode). The cached object is the raw
  API payload (plus metadata payload) so parsing improvements apply to old cache entries. The *requested*
  point is never cached; it is attached on every read.
* **TTL**: `cache_ttl_forecast_s` (900 s) for forecast, ensemble and analysis; `cache_ttl_observation_s`
  (300 s) for observations. BoM publishes every 30 min, so for BoM 300 s is polite but not necessary.
* **Cache hits keep the original `retrieved_at_utc`.** A hit never "refreshes" a timestamp; the provenance
  note says it was served from cache and how old it is. Age = `provenance.age_s(now)`.
* **Offline** (`allow_network=False`): serve the cache (even past its TTL, because age is honest and the
  quality layer flags it `STALE`) or raise `ProviderUnavailableError`. No request is made, no value is invented.
* **Online failure** with a stale cache entry: raise (default). `serve_stale_on_error=True` opts in to serving it,
  again with the original `retrieved_at_utc`.
* **Staleness limits** (`rwi.quality`): defaults from settings: observations `max_observation_age_s` (1800 s),
  forecasts and analyses `max_forecast_age_s` (10800 s). For observations the *newest observation time* is
  compared; for every other kind the *retrieval time* is (a forecast for a future valid time is not old).
  A stale series is flagged `STALE` on every value; nothing is deleted.
* **Retry**: timeouts, transport errors, 429 and 5xx are retried `http_retries` times with delay
  `http_backoff_s * 2**attempt`; a `Retry-After` is honoured up to 60 s. Other 4xx are not retried. Error messages
  never contain the query string.

---

## 6. Quality control (`rwi.quality`)

`check_series(series, *, now=None, max_age_s=None) -> WeatherSeries`, `check_value(value, *, previous=None) -> Measured`
(plus optional `now`, `max_age_s`), `summarise(series) -> QualityReport`, and `analyse_series(...)` returning
`(series, QualityReport with the explanatory issues)`. **Flags only**: no step is removed, no value changed. The most
severe flag wins (`MISSING` > `OUT_OF_RANGE` > `SENSOR_SUSPECT` > `RATE_IMPLAUSIBLE` > `STALE`); all findings stay in
`QualityReport.issues`. Re-running is idempotent (a previous `STALE` is re-evaluated against the new `now`).

| Check | Flag | Rule |
|---|---|---|
| missing | `MISSING` | `value is None` |
| range | `OUT_OF_RANGE` | outside the registry min/max |
| rate | `RATE_IMPLAUSIBLE` | `|dv|/dt` above `max_plausible_rate_per_hour`; `dt` clamped to >= 15 min; skipped when the limit >= the variable's span (vacuous, e.g. cloud cover); compares with the previous in-range value |
| dew point | `SENSOR_SUSPECT` (both) | dew point > temperature + 0.1 degC |
| gust | `SENSOR_SUSPECT` (both) | gust < mean wind - 0.1 m/s |
| DHI | `SENSOR_SUSPECT` (both) | DHI > GHI + 1 W/m2 |
| GHI at night | `SENSOR_SUSPECT` (GHI) | provider `is_day == 0` at two consecutive steps (or first step) and GHI > 5 W/m2 |
| duplicate timestamps | `SENSOR_SUSPECT` | every copy flagged |
| non-monotonic timestamps | `SENSOR_SUSPECT` | the out-of-order step flagged |
| frozen | `SENSOR_SUSPECT` | >= 6 identical in-range values of temperature, apparent temp., dew point, RH, MSL/surface pressure, wind direction, soil temp. (not at the physical bound) |
| stale | `STALE` | newest data older than the limit (sec. 5) |

Judgement calls worth knowing:

* **Dusk**: Open-Meteo `is_day` is instantaneous but GHI is the *preceding-hour mean*, so the first night hour
  legitimately has GHI > 0 (real data, 2026-10-20 Darwin: `is_day=0`, GHI 2 W/m2 after 109 W/m2). A naive
  "GHI must be 0 when is_day is 0" check would flag good data; hence the two-step rule and 5 W/m2 allowance.
* **Model data and `SENSOR_SUSPECT`**: the flag set is fixed by `QualityFlag`; for model output the flag means
  "internally inconsistent", not "faulty sensor". Real example in the archive fixture: one hour where the model's
  gust (18.7 km/h) is below its own mean wind (20.4 km/h). It is reported, not corrected.
* The rate limits come from the registry (integrator-owned) and have not been calibrated against Australian
  data; the 0.1 degC / 0.1 m/s / 1 W/m2 / 5 W/m2 / 6-step thresholds are my choices, unvalidated.

---

## 7. Reproducing the evidence

* Fixtures in `tests/fixtures/` are unmodified captured responses (except `bom_idd60910_94120_darwin_trimmed.json`,
  trimmed to the header and the newest 6 of 149 records, and `openmeteo_429_daily_limit.json`, which wraps the captured status,
  headers and body): `openmeteo_forecast_darwin_ifs025.json`, `openmeteo_archive_darwin_ifs.json`,
  `openmeteo_ensemble_darwin_ifs025.json`, `openmeteo_meta_ecmwf_ifs025.json`,
  `openmeteo_forecast_all_null_bom_access.json`, `openmeteo_error_400_out_of_range.json`, all captured 2026-11-01/02.
* Live smoke tests: `RWI_RUN_NETWORK_TESTS=1 .venv/bin/pytest tests/integration/test_provider_live.py -m network`
  (skipped by default). They assert the response shape, not values, and make one request per API.

---

## 8. Validation status

Vocabulary: not started / researching / implementing / implemented, not validated / tested / validated against real data / operationally ready.

* Component `providers.base` (retry, backoff, rate limit, disk cache, offline mode): **tested**
* Component `providers.openmeteo` forecast: **tested**
* Component `providers.openmeteo` archive / analysis: **tested**
* Component `providers.openmeteo` ensemble: **tested**
* Component `providers.openmeteo` model init time via the Metadata API: **implemented, not validated**
* Component `providers.bom` station observations: **tested**
* Component `providers.bom` forecasts, radar, registered-user services: **researching**
* Component `providers.registry` fallback: **tested**
* Component `quality` checks and `QualityReport`: **tested**
* Provider skill (accuracy of Open-Meteo vs measurements over the route): **not started**

"Tested" here means unit tests against real captured responses plus one live smoke run on 2026-11-01. The 61-hour Darwin
spot check in sec. 2.2 is too small to count as validation against real data. Nothing is operationally ready: there is
no licensed observation source for the route, the free-tier licences need confirmation (docs/research/data-sources.md),
and nothing has been exercised under the connectivity conditions of the race.
