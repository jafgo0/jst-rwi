# Requirements traceability matrix: 130 competencies

Generated 2026-11-01 from the repository state on disk (see section 2 for how status is decided). This is the traceability matrix required by the master specification (sections 4 and 19, step 2): one row per competency, numbered as in the master prompt.

## 1. How to read this document

* **Category**: *operational requirement* (software must do it), *scientific foundation* (knowledge the models and documents must be correct about), *professional-behaviour standard* (a design rule embodied in validation, disclosure, workflow, tests or documentation, not a screen), *optional research* (only if evidence justifies it).
* **Priority** (master specification section 4): **P0** essential operational capability; **P1** important advanced capability; **P2** optional specialisation; **R&D** research that needs additional data, testing or scientific validation. For design-standard items the priority is that of the rule being embodied.
* **Component**: module path where code exists or is contracted; `n/a - design standard` where the honest answer is that no software feature exists or should exist (the text in brackets says how the item is embodied). `Planned` marks a component that is not in `docs/CONTRACTS.md` and has no owner yet.
* **Status** uses exactly the vocabulary of master specification section 17: not started / researching / implementing / implemented, not validated / tested / validated against real data / operationally ready.
* Cells are deliberately short. Depth is in `docs/ARCHITECTURE.md`, `docs/science/*`, the ADRs and `docs/research/*`.

## 2. How status was decided (and what it does not mean)

Status was derived from the files on disk, not from intentions:

* **not started**: no non-empty code file for the component (only an empty `__init__.py`, or nothing).
* **researching**: no code, but written research or an ADR exists for it.
* **implementing**: code exists but no passing automated tests for it, or the capability is only partly covered, or it needs a module that does not exist yet. Parallel work is in progress, so this is the conservative reading; a finished module without tests is still reported here.
* **tested**: code exists and its unit tests pass (`pytest`, run during generation). This is *not* meteorological accuracy and not validation: it means the code does what its tests say, including documented external reference values where marked `@pytest.mark.science`.
* **validated against real data**, **operationally ready**: used by no row. There is no vehicle telemetry, no recorded forecast/observation history and no observation source yet, so nothing can honestly claim either.
* A row that needs several modules takes the *least* advanced of them, so a row whose weather input does not exist cannot read 'tested' merely because its solar-geometry half does.

Snapshot of the modules on the generation date:

| Module | Code files | Test files | Result | Derived status |
|---|---|---|---|---|
| `core` | 10 | 7 | 72 passed in 0.30s | tested |
| `solar` | 4 | 3 | 57 passed in 3.38s | tested |
| `providers` | 4 | 0 | - | implementing |
| `quality` | 2 | 0 | - | implementing |
| `route` | 1 | 0 | - | implementing |
| `energy` | 4 | 5 | 48 passed in 2.39s | tested |
| `vehicle` | 2 | 2 | 44 passed in 0.99s | tested |
| `strategy` | 2 | 0 | - | implementing |
| `alerts` | 1 | 1 | 16 passed in 0.04s | tested |
| `api` | 11 | 1 | 12 passed, 1 warning in 3.45s | tested |
| `web` | 23 | 0 | - | implementing |

Caveat: a module marked *tested* may still implement only part of its contracted API (check `docs/CONTRACTS.md`). The rows that depend on the missing parts are marked lower through the 'least advanced' rule only where the dependency is another module; missing functions inside a tested module are not detected by this scan.

## 3. The matrix

### A. Atmospheric science and physics (1-20)

| # | Competency | Category | Relevance to solar racing | Method / approach | Data requirement | Software component | Validation criterion | Priority | Status |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Atmospheric Physics | scientific foundation | Basis for reading every forecast field correctly | Governing relations used through validated libraries; assumptions written per module | Variable registry | n/a - design standard (`docs/science/*`) | Every relation cited and unit-tested against a published value | P1 | not started |
| 2 | Thermodynamics | scientific foundation | Air density sets drag; cell temperature sets PV derate | Ideal-gas air density; Faiman/NOCT cell-temperature models | T2m, surface pressure, humidity, wind | `rwi.energy.thermal`; `rwi.vehicle.dynamics` (density) | Cell temperature reproduces pvlib/published example; density matches standard-atmosphere reference | P1 | tested |
| 3 | Atmospheric Pressure | scientific foundation | Needed for air density; QC and synoptic indicator | Surface vs MSL pressure; reduction by elevation | surface_pressure, pressure_msl, elevation | Variables only in `rwi.core.variables`; use in `rwi.vehicle.dynamics` | Range/unit tests; barometric reduction matches formula | P1 | tested |
| 4 | Wind Dynamics | scientific foundation | Head and cross wind drive drag and stability | Wind vector algebra; 'from' convention; apparent wind | 10 m wind speed/direction, vehicle heading | `rwi.core.wind` | Sign tests for head/tail/cross in the cardinal cases; u,v round trip | P0 | tested |
| 5 | Atmospheric Humidity | scientific foundation | Minor effect on air density; fog/cloud diagnosis | Dew point, vapour pressure (Magnus) | RH2m, dew point | Variables only in `rwi.core.variables`; no analysis code | Dew point within 0.1 K of Magnus reference | P2 | not started |
| 6 | Cloud Physics | scientific foundation | Clouds dominate irradiance variability | Cloud-cover layers; clearness index as the diagnostic | cloud_cover (low/mid/high), GHI | Clearness index in `rwi.solar.decomposition`; no cloud model | Clearness index reproduces reference; no cloud-shadow claims | P1 | implementing |
| 7 | Precipitation Physics | scientific foundation | Rain affects safety, visibility and irradiance | Rate/accumulation thresholds, phase from weather code | precipitation, probability, weather_code | `rwi.alerts.rules` (precipitation) | Alert fires at stated threshold on fixture and not on stale data | P1 | tested |
| 8 | Solar Radiation | operational requirement | Irradiance is the vehicle's fuel | NREL SPA geometry, Ineichen-Perez clear sky, Erbs/DISC/DIRINT decomposition (pvlib) | Position, time, Linke turbidity (bundled) | `rwi.solar.geometry`, `.clearsky`, `.decomposition` | Zero at night; clear-sky GHI vs published reference for named place/time; clear-sky never labelled measured | P0 | tested |
| 9 | Thermal Radiation | scientific foundation | Longwave exchange is absorbed into empirical cell-temperature models | Not modelled separately | n/a | n/a - design standard (decision: not modelled) | Cell temperature compared with array telemetry once available | P2 | not started |
| 10 | Atmospheric Circulation | scientific foundation | Synoptic context for pre-race briefing | Qualitative reading of NWP pressure/wind fields | NWP fields | n/a - design standard (briefing notes) | Briefing cites model run and valid time | P2 | not started |
| 11 | Turbulence | scientific foundation | Gust loading and stability | Use provider gusts; no turbulence model | wind_gusts_10m | n/a - design standard (gusts handled under #23) | n/a (covered by gust tests) | P2 | not started |
| 12 | Boundary Layer Meteorology | scientific foundation | 10 m wind differs from wind at vehicle height | Log-law height factor | 10 m wind, roughness | `rwi.energy.thermal.log_wind_height_factor` | Factor equals log-law reference; vehicle-height drag correction not yet modelled | P1 | tested |
| 13 | Convection | scientific foundation | Afternoon convection gives rapid irradiance changes and storms | Instability flags from NWP; weather code | CAPE/instability if offered by provider | None yet (planned inside alerts/forecast) | Flag raised on a known convective case in fixtures | P1 | not started |
| 14 | Weather Fronts | scientific foundation | Wind shifts and cloud bands, mainly at the SA end | Qualitative from pressure/wind fields | NWP fields | n/a - design standard (briefing notes) | Briefing cites model run | P2 | not started |
| 15 | Thunderstorms | scientific foundation | Severe gusts, rain and lightning hazard | Weather-code and gust/precipitation rules | weather_code, gusts, precipitation | `rwi.alerts.rules` (thunderstorm category in contract) | Alert on fixture with storm code; no alert without data | P1 | tested |
| 16 | Climatology | scientific foundation | Priors and sanity bounds; turbidity climatology | pvlib Linke-turbidity climatology; route climatology later | Bundled turbidity file; archive (later) | `rwi.solar.clearsky` (turbidity only) | Climatology labelled `climatology`, never as forecast | P1 | implementing |
| 17 | Topographic Effects | scientific foundation | Route is mostly flat; ranges near Port Augusta; gradient affects energy | Elevation profile and grade along route | Route elevation | `rwi.route` (elevation_profile) | Elevation profile within tolerance of source; grade sign tests | P2 | implementing |
| 18 | Ocean-Atmosphere Interaction | scientific foundation | Regional influence at the southern end only | Not modelled | n/a | n/a - design standard | n/a | P2 | not started |
| 19 | Atmospheric Chemistry | scientific foundation | Dust/smoke change direct vs diffuse light | Turbidity parameter in clear sky; aerosol data R&D | Linke turbidity; AOD (not sourced) | `rwi.solar.clearsky` (turbidity parameter) | DNI responds monotonically to turbidity in test; AOD source unvalidated | P2 | implementing |
| 20 | Mathematics and Statistics | scientific foundation | Basis of uncertainty, verification and calibration | MAE/RMSE/bias, interval and probabilistic scores, hand-checked | Any paired series | `rwi.energy.series.compare_with_measured`; planned verification module (not in CONTRACTS.md) | Metrics equal hand-computed examples | P0 | implementing |

### B. Observation and real-time analysis (21-40)

| # | Competency | Category | Relevance to solar racing | Method / approach | Data requirement | Software component | Validation criterion | Priority | Status |
|---|---|---|---|---|---|---|---|---|---|
| 21 | Temperature Analysis | operational requirement | Cell temperature, air density, battery thermal inputs | Range, rate-of-change, unit checks | temperature_2m | `rwi.quality` | Impossible/jump values flagged, never deleted | P0 | implementing |
| 22 | Wind Analysis | operational requirement | Measured wind feeds relative-wind and alerts | Vector decomposition and range checks | wind speed/direction | `rwi.core.wind`, `rwi.quality` | Sign tests; out-of-range flagged | P0 | implementing |
| 23 | Wind Gust Analysis | operational requirement | Gusts above 100 km/h are possible (Reg 2.19.3 note); stability | Gust threshold alerts; gust factor | wind_gusts_10m | `rwi.alerts.rules`, `rwi.quality` | Alert at declared (assumed) threshold; threshold set by vehicle limits once known | P0 | implementing |
| 24 | Pressure Analysis | operational requirement | Plausibility check; synoptic tendency | Rate-of-change limit from registry | surface_pressure, pressure_msl | `rwi.quality` | Step > limit flagged | P1 | implementing |
| 25 | Humidity Analysis | operational requirement | Plausibility; fog indicator | Range 0-100 and dew-point <= temperature | RH2m, dew point | `rwi.quality` | Violations flagged | P2 | implementing |
| 26 | Precipitation Measurement | operational requirement | Rain risk validation needs observed rain | Accumulation and intensity QC; needs an observation source | Observed precipitation (source unresolved) | `rwi.providers` (observation adapter not built), `rwi.quality` | Observed rain never mixed with forecast rain | P1 | implementing |
| 27 | Cloud Observation | operational requirement | Verify cloud forecasts; clear-sky index from measured GHI | Station/satellite cloud, clearness from measured irradiance | Observed cloud or GHI (no source yet) | `rwi.providers`; `rwi.solar` | Measured clearness index <= physical limit | P1 | implementing |
| 28 | Visibility Analysis | operational requirement | Reg 3.31.6 penalises driving without adequate visibility (smoke, dust, rain) | Visibility threshold alerts | visibility | `rwi.alerts.rules` | Alert below declared threshold on fixture | P1 | tested |
| 29 | Solar Irradiance Measurement | operational requirement | Compare model with measured array output / pyranometer | Error metrics model vs measured; night values flagged | Telemetry or pyranometer (none yet) | `rwi.energy.series.compare_with_measured` | Metrics reproduce hand calculation; measured and clear-sky kept separate | P0 | tested |
| 30 | UV Analysis | optional research | Little effect on energy; driver exposure only | UV index carried, not analysed | uv_index | Variable only in `rwi.core.variables` | n/a | P2 | not started |
| 31 | Satellite Analysis | optional research | Cloud information beyond NWP (Phase 5) | Himawari access researched (`data-sources.md`); processing not designed | Himawari-9 via NOAA S3 | Planned `rwi.satellite` (not in CONTRACTS.md) | Needs benchmark vs ground data before any claim | R&D | researching |
| 32 | Weather Radar Monitoring | operational requirement | Storm movement near the route | Radar overlay; BoM licence unresolved | BoM radar (licence open) | None (planned) | Timestamps shown per frame | P2 | not started |
| 33 | GPS and Geospatial Analysis | operational requirement | Every value is tied to a place and time | WGS84 geodesics (pyproj); single active state | PositionFix | `rwi.core.geo`, `.position`, `.state` | Distance/bearing vs reference; stale and missing fix raise | P0 | tested |
| 34 | Sensor Calibration | operational requirement | Detect drifting vehicle or station sensors | Compare against clear-sky bound and neighbours | Sensor streams (none yet) | Planned in `rwi.quality` | Seeded drift detected in replay | P1 | not started |
| 35 | Data Quality Control | operational requirement | Bad data must be flagged, not silently used | Range, rate, freshness, missing flags | Series + registry | `rwi.quality` | Flags set on fixtures; originals preserved | P0 | implementing |
| 36 | Real-Time Monitoring | operational requirement | Continuous view of current conditions | Periodic refresh, health, data age | Providers, state | `rwi.api` (health/state), `web/` | Data age visible; refresh failures visible | P0 | implementing |
| 37 | Historical Data Analysis | operational requirement | Compare now with past; replay | Archive queries; storage deferred (ADR 0006) | Archive, recorded tracks | `rwi.providers` (archive); no store | Archive values labelled historical, never current | P1 | not started |
| 38 | Vertical Atmospheric Profile Analysis | scientific foundation | Stability context only | Not planned beyond NWP fields | Pressure-level fields | n/a - design standard | n/a | P2 | not started |
| 39 | Spatial Weather Analysis | operational requirement | Compare conditions along and across the route | Route weather samples; segment comparison | Forecasts at route points | `rwi.route`, `rwi.providers`, `rwi.api` | Samples carry point, time and lead time | P1 | implementing |
| 40 | Microclimate Analysis | optional research | Local heat/shade effects | No validated method | Local sensors | n/a - design standard | Needs data | R&D | not started |

### C. Forecasting and prediction (41-60)

| # | Competency | Category | Relevance to solar racing | Method / approach | Data requirement | Software component | Validation criterion | Priority | Status |
|---|---|---|---|---|---|---|---|---|---|
| 41 | Nowcasting | optional research | 0-3 h solar changes | Persistence baseline first; satellite/ML only if it beats it | Satellite, observations | Planned (Phase 5) | Beats persistence on held-out data | R&D | not started |
| 42 | Short-Range Forecasting | operational requirement | Core product: hours to days | NWP via provider adapter | Open-Meteo / ECMWF | `rwi.providers` | Provenance with model init time; lead time tracked | P0 | implementing |
| 43 | Medium-Range Forecasting | operational requirement | Pre-race planning | Same adapters, longer horizon | Provider forecasts | `rwi.providers` | Horizon respected (`max_forecast_hours`) | P1 | implementing |
| 44 | Seasonal Forecasting | optional research | Little value for an 8-day event | Climatology only | n/a | n/a - design standard | n/a | P2 | not started |
| 45 | Temperature Forecasting | operational requirement | Cell temperature, density, battery | Provider forecast with QC | temperature_2m | `rwi.providers` | Fixture round trip with unit and provenance | P0 | implementing |
| 46 | Wind Forecasting | operational requirement | Head/cross wind along the route | Provider forecast, relative wind at ETA | wind speed/direction | `rwi.providers`, `rwi.core.wind` | Conversion tests (km/h to m/s) | P0 | implementing |
| 47 | Wind Gust Forecasting | operational requirement | Safety | Provider gusts, alert rules | wind_gusts_10m | `rwi.providers`, `rwi.alerts.rules` | Alert on fixture | P0 | implementing |
| 48 | Cloud Cover Forecasting | operational requirement | Largest irradiance driver | Provider cloud layers | cloud_cover* | `rwi.providers` | Fixture round trip | P0 | implementing |
| 49 | Precipitation Forecasting | operational requirement | Rain risk and visibility | Provider precipitation and probability | precipitation, probability | `rwi.providers`, `rwi.alerts.rules` | Fixture round trip; alert rules | P0 | implementing |
| 50 | Solar Irradiance Forecasting | operational requirement | Input to energy forecast | Provider GHI/DNI/DHI, sanity bound by clear sky | ghi, dni, dhi | `rwi.providers`, `rwi.solar`, `rwi.api.integration` | Never above clear-sky bound; zero at night | P0 | implementing |
| 51 | Storm Forecasting | operational requirement | Severe-weather warning | Weather code, gusts, precipitation rules | weather_code etc. | `rwi.alerts.rules` | Alert on fixture | P1 | implementing |
| 52 | Fog Forecasting | operational requirement | Rare on route; Adelaide area | Visibility/humidity flags | visibility, RH | `rwi.alerts.rules` (visibility) | Alert on fixture | P2 | tested |
| 53 | Forecast Uncertainty Analysis | operational requirement | Decisions must carry uncertainty | `Uncertainty` type; ensemble spread; no invented error bars | Ensemble members | `rwi.core.provenance.Uncertainty`; ensemble not built | Spread computed from members equals hand calculation | P0 | implementing |
| 54 | Probabilistic Forecasting | operational requirement | Probabilities for rain, low irradiance | Ensemble percentiles and exceedance probabilities | Ensemble members | `rwi.providers` (ensemble), planned fusion | Percentiles match numpy on fixture; calibration unproven | P1 | implementing |
| 55 | Multi-Model Forecast Comparison | operational requirement | Detect model disagreement | Side-by-side series, disagreement metric | Several models | Planned (Phase 2, not in CONTRACTS.md) | Disagreement flagged on seeded fixture | P1 | not started |
| 56 | Ensemble Forecasting | operational requirement | Scenario and uncertainty source | `EnsembleForecast` contract; Open-Meteo ensemble | Ensemble API | `rwi.providers` | Member count/times preserved | P1 | implementing |
| 57 | Model Bias Correction | optional research | Possible gain, needs history | Only after leakage-safe evaluation | Verification history (none) | Planned (Phase 5) | Improves held-out MAE vs raw | R&D | not started |
| 58 | Local Downscaling | optional research | 9-25 km grid vs route scale | Elevation correction at most; benefit unproven | Elevation, observations | None | Needs data | R&D | not started |
| 59 | Forecast Verification | operational requirement | Know how good each model is here | MAE, RMSE, bias by variable and lead time | Forecast/observation pairs (none stored) | Planned verification module; storage per ADR 0006 triggers | Metrics by lead time reproducible from stored pairs | P1 | not started |
| 60 | Extreme Weather Assessment | operational requirement | Identify severe conditions and consequences | Threshold alerts with consequences stated | Gusts, rain, heat | `rwi.alerts.rules` | Severity ordering test | P1 | tested |

### D. Software, data science and computational engineering (61-80)

| # | Competency | Category | Relevance to solar racing | Method / approach | Data requirement | Software component | Validation criterion | Priority | Status |
|---|---|---|---|---|---|---|---|---|---|
| 61 | Python | operational requirement | Implementation language (ADR 0001) | Python 3.12, typed, ruff, mypy | n/a | `src/rwi/*` | Lint and type checks clean; tests pass | P0 | tested |
| 62 | R | optional research | Not needed; Python covers the statistics | Not adopted | n/a | n/a - design standard (decision: not adopted) | n/a | P2 | not started |
| 63 | MATLAB | optional research | Not needed; licence cost | Not adopted | n/a | n/a - design standard (decision: not adopted) | n/a | P2 | not started |
| 64 | Fortran/C/C++ | optional research | Only if a profiled hot path demands it | NumPy/pvlib are compiled already | n/a | n/a - design standard (adopt only after profiling) | Profile shows a hot path | P2 | not started |
| 65 | SQL and Databases | operational requirement | History and verification need queries later | Deferred; triggers in ADR 0006 | n/a | n/a - design standard (file cache, ADR 0006) | Triggers reviewed at Phase 2 start | P1 | researching |
| 66 | API Integration | operational requirement | Weather sources | Adapter per provider, retry/backoff, fixtures | Open-Meteo first; others later | `rwi.providers` | respx tests for success, 429, malformed, offline | P0 | implementing |
| 67 | Geographic Information Systems | operational requirement | Map, route, projection | GeoJSON/GPX, geodesy, MapLibre map | Route, tiles | `rwi.core.geo`, `rwi.route`, `web/` | Map marker equals API position | P1 | implementing |
| 68 | Numerical Modeling | operational requirement | PV chain, vehicle dynamics, scenarios | Deterministic physical models | Parameters | `rwi.energy`, `rwi.vehicle`, `rwi.strategy` | Energy balance closes; unit tests | P0 | implementing |
| 69 | Machine Learning | optional research | Only if it beats physical and persistence baselines | Benchmark-first, leakage-safe | History (none) | None | Beats baselines on held-out data | R&D | not started |
| 70 | Artificial Intelligence Models | optional research | AIFS available via open data and Open-Meteo | Treat as one more forecast source; compare, do not assume better | AIFS (access researched) | `rwi.providers` (ensemble) | Benchmark evidence before any claim | R&D | researching |
| 71 | Time-Series Analysis | operational requirement | Power and weather series | pandas/NumPy, trapezoidal integration | Series | `rwi.energy.series` | Integration equals analytic case | P1 | tested |
| 72 | Data Visualization | operational requirement | Engineers read charts under time pressure | MapLibre, charts, units from registry, observed vs predicted separated | API payloads | `web/` | Manual check of labels, units, timestamps; no automated front-end tests yet | P0 | implementing |
| 73 | Data Assimilation | optional research | Correct model with measured array output | Not designed | Telemetry | None | Needs data | R&D | not started |
| 74 | Automation | operational requirement | Scheduled refresh and updates | Background refresh with explicit intervals | n/a | Planned in `rwi.api` | Refresh interval honoured; failures visible | P1 | not started |
| 75 | Model Validation | operational requirement | Prove models against independent data | Reference-value tests now; real-data validation later | Observations, telemetry | Tests (`@pytest.mark.science`); planned verification | Documented external reference per model | P1 | implementing |
| 76 | Statistical Calibration | optional research | Probabilities must be reliable | Reliability diagrams after history exists | Verification history | None | Calibration curve on held-out data | R&D | not started |
| 77 | Error Handling | operational requirement | Missing, invalid, delayed data | Typed errors, 409/501/503 mapping | n/a | `rwi.core.errors`, `rwi.api.errors`, `rwi.api.capability` | Each error class maps to a documented HTTP status | P0 | tested |
| 78 | Geospatial Programming | operational requirement | Route-based calculations | Geodesic distance, projection, interpolation | Route | `rwi.core.geo`, `rwi.route` | Projection and distance tests | P0 | implementing |
| 79 | Real-Time Data Processing | operational requirement | Streaming telemetry (Phase 3) | Not designed; protocol unknown | Telemetry (unknown) | None | Needs protocol | P1 | not started |
| 80 | System Integration | operational requirement | Weather, position, energy, strategy together | Composition in `rwi.api`; contracts | n/a | `rwi.api.integration`, `rwi.contracts` | End-to-end test from forecast to energy to scenario | P0 | implementing |

### E. Professional meteorologist behaviour (81-100)

| # | Competency | Category | Relevance to solar racing | Method / approach | Data requirement | Software component | Validation criterion | Priority | Status |
|---|---|---|---|---|---|---|---|---|---|
| 81 | Analytical Thinking | professional-behaviour standard | Recommendations must show the relationships used | Evidence list on every recommendation | n/a | n/a - design standard (`Recommendation.evidence`) | Each recommendation lists traceable evidence items | P1 | implementing |
| 82 | Accuracy | professional-behaviour standard | Wrong numbers lose races | Canonical units, validators, reference-value tests | n/a | n/a - design standard (registry, validators, tests) | Reference-value tests pass in CI; unit mismatch rejected | P0 | tested |
| 83 | Critical Thinking | professional-behaviour standard | Challenge unreliable forecasts | QC flags, clear-sky bound check, disagreement display | n/a | n/a - design standard (`rwi.quality`, comparison) | Implausible forecast flagged on fixture | P0 | implementing |
| 84 | Scientific Curiosity | professional-behaviour standard | Unexplained anomalies deserve a record | Anomaly log with context | n/a | n/a - design standard (log + review routine) | Anomalies reviewed after each session | P2 | not started |
| 85 | Problem Solving | professional-behaviour standard | Failures during a race need known responses | Degradation table, runbooks | n/a | n/a - design standard (ARCHITECTURE section 8) | Each failure mode has a tested response | P1 | implementing |
| 86 | Decision-Making Ability | professional-behaviour standard | Support choice under uncertainty | Scenario comparison with assumptions and reevaluation time | n/a | `rwi.strategy` (`Recommendation`) | Recommendation states assumptions, uncertainty, reevaluate-after | P0 | implementing |
| 87 | Stress Resilience | professional-behaviour standard | Predictable behaviour when data fail | No crash on provider failure; deterministic outputs | n/a | n/a - design standard (failure-injection tests) | Failure-injection suite passes | P0 | implementing |
| 88 | Clear Communication | professional-behaviour standard | Engineers need plain statements with units | Headline/rationale fields; units from registry | n/a | n/a - design standard (`Recommendation`, `web/` text) | Review by a strategist; no unit-less number | P1 | implementing |
| 89 | Collaboration | professional-behaviour standard | Weather, power and strategy teams share one picture | Shared contracts, ownership map, exportable summaries | n/a | n/a - design standard (`docs/CONTRACTS.md`) | Contract tests pass | P2 | implementing |
| 90 | Adaptability | professional-behaviour standard | New data must update assessments safely | Explicit update intervals, hysteresis | n/a | n/a - design standard (update-interval policy, not yet coded) | Recommendation does not flip on noise in replay | P1 | not started |
| 91 | Patience | professional-behaviour standard | No conclusions from too little data | Minimum-sample rules; 'insufficient data' state | n/a | n/a - design standard | No skill claim below sample threshold | P2 | not started |
| 92 | Responsibility | professional-behaviour standard | Consequences of wrong information | Severity grading; safety/regulatory notes; no vehicle commands | n/a | n/a - design standard (`rwi.alerts`, `Recommendation`) | No endpoint writes to the vehicle | P0 | implementing |
| 93 | Objectivity | professional-behaviour standard | Choose by evidence | Model weights from verified metrics only | n/a | n/a - design standard (selection policy) | Selection rule documented and tested | P1 | not started |
| 94 | Self-Criticism | professional-behaviour standard | Own errors must be visible | Verification reports; known-issues log | n/a | n/a - design standard (verification report) | Report generated per session | P1 | not started |
| 95 | Prioritization | professional-behaviour standard | Surface what matters for the race | Alert severity ordering; command-centre ordering | n/a | `rwi.alerts.rules` (`sort_alerts`), `web/` | Ordering test | P1 | tested |
| 96 | Risk Awareness | professional-behaviour standard | Hazards tied to penalties and safety | Thresholds linked to Regulation clauses (3.31.6, 2.19.3) | n/a | `rwi.alerts.rules` | Each alert cites its trigger values | P0 | tested |
| 97 | Continuous Learning | professional-behaviour standard | Improve only with validated evidence | Leakage-safe evaluation before adopting any change | n/a | n/a - design standard | Change log with before/after metrics | R&D | not started |
| 98 | Structured Working Methods | professional-behaviour standard | Reproducibility, traceability | Docs, ADRs, tests, provenance, this matrix | n/a | n/a - design standard (repository practice) | Matrix status matches disk | P0 | implementing |
| 99 | Systems Thinking | professional-behaviour standard | Weather affects energy affects strategy | Chain forecast, POA, power, segments, scenarios in `rwi.api` | n/a | `rwi.api.integration` | End-to-end test | P0 | implementing |
| 100 | Uncertainty Awareness | professional-behaviour standard | Every forecast has limits | Unknown stays `None`; uncertainty shown or declared absent | n/a | n/a - design standard (`Uncertainty`, UI disclosure) | No displayed forecast without uncertainty or an explicit 'not available' | P0 | implementing |

### F. Solar racing meteorology (101-130)

| # | Competency | Category | Relevance to solar racing | Method / approach | Data requirement | Software component | Validation criterion | Priority | Status |
|---|---|---|---|---|---|---|---|---|---|
| 101 | Solar Position Calculation | operational requirement | Everything GPS-dependent | NREL SPA via pvlib; apparent elevation decides day/night | Position, UTC time | `rwi.solar.geometry` | Jonkoping night sun below horizon; Darwin noon high; reference values documented | P0 | tested |
| 102 | Solar Irradiance Analysis | operational requirement | Irradiance along the route | Clear sky and forecast irradiance at ETA points | Route, forecasts | `rwi.solar`, `rwi.route`, `rwi.api` | Clear-sky never shown as measured | P0 | implementing |
| 103 | Cloud Shadow Prediction | optional research | Cannot be resolved at 9-25 km | No claim without resolution and validation | Satellite | None | Needs data | R&D | not started |
| 104 | Cloud Movement Tracking | optional research | Satellite motion vectors | Optical flow on Himawari (evaluate) | Himawari | Planned (Phase 5) | Beats persistence | R&D | not started |
| 105 | GHI Forecasting | operational requirement | PV input | Provider GHI with clear-sky bound | ghi | `rwi.providers`, `rwi.solar` | Bounded by clear sky; night zero | P0 | implementing |
| 106 | DNI Forecasting | operational requirement | Plane-of-array direct term | Provider DNI or decomposition (derived, labelled) | dni or ghi | `rwi.solar.decomposition`, `rwi.providers` | Decomposition output labelled derived; outback skill unvalidated | P0 | implementing |
| 107 | DHI Forecasting | operational requirement | Diffuse term | Provider DHI or decomposition | dhi or ghi | `rwi.solar.decomposition`, `rwi.providers` | Same as #106 | P0 | implementing |
| 108 | Aerodynamic Wind Resistance Analysis | operational requirement | Wind changes drag by direction | Apparent wind with yaw sensitivity | Wind, heading, CdA, yaw coefficient | `rwi.vehicle.dynamics`, `rwi.core.wind` | Headwind raises, tailwind lowers drag power; CdA is a parameter, not a default | P0 | tested |
| 109 | Crosswind Analysis | operational requirement | Stability and drag | Crosswind component and yaw angle; alert thresholds | Wind, heading | `rwi.core.wind`, `rwi.alerts.rules` | Cardinal-case tests; alert on fixture | P0 | tested |
| 110 | Headwind Forecasting | operational requirement | Energy cost | Relative wind at ETA heading | Wind forecast, trajectory | `rwi.core.wind`, `rwi.route`, `rwi.api` | Sign tests; samples per ETA | P0 | implementing |
| 111 | Tailwind Forecasting | operational requirement | Energy gain | Relative wind at ETA heading (negative headwind) | Wind forecast, trajectory | `rwi.core.wind`, `rwi.route`, `rwi.api` | Sign tests; samples per ETA | P0 | implementing |
| 112 | Road Temperature Analysis | optional research | Tyre/rolling effects, minor | Skin-temperature proxy only; no validated method | soil_temperature_0cm | Variable only in registry | Needs measurements | R&D | not started |
| 113 | Battery Temperature Analysis | operational requirement | Battery limits are regulated (Reg 2.5.5) | Ambient and solar load as inputs; battery model temperature checks | Ambient T, cell limits | `rwi.vehicle.battery` | Violation raised when temperature outside limits in test | P1 | tested |
| 114 | Solar Panel Temperature Analysis | operational requirement | Cell temperature lowers efficiency | Faiman / NOCT models | T2m, wind, POA | `rwi.energy.thermal` | Matches pvlib/published example | P0 | tested |
| 115 | Solar Energy Production Forecasting | operational requirement | Energy supply | POA, cell temperature, array power, integration | Irradiance, array config | `rwi.energy` | Units W, Wh; integration equals analytic case; no telemetry comparison yet | P0 | implementing |
| 116 | Energy Balance Analysis | operational requirement | Generation minus consumption | Segment integration with battery limits | Power series, vehicle | `rwi.strategy`, `rwi.vehicle`, `rwi.energy` | End SoC = start + generated - consumed within tolerance | P0 | implementing |
| 117 | Route-Based Forecasting | operational requirement | Weather where the vehicle will be | Trajectory + forecasts at ETA | Route, forecasts | `rwi.route`, `rwi.providers`, `rwi.api` | Sample point and time match trajectory | P0 | implementing |
| 118 | ETA-Linked Weather Analysis | operational requirement | Weather at the time of arrival | Valid-time interpolation at ETA | Trajectory, series | `rwi.route.trajectory`, `rwi.contracts.WeatherSeries.nearest_step` | ETA shift moves sampled time | P0 | implementing |
| 119 | GPS Synchronization | operational requirement | One position and time across subsystems | UTC, GPS vs receipt time, skew | PositionFix | `rwi.core.position`, `.timebase`, `.state` | Skew and age tests; naive datetimes rejected | P0 | tested |
| 120 | Satellite-Based Nowcasting | optional research | Short-term cloud change | Not designed | Himawari | Planned (Phase 5) | Beats persistence | R&D | not started |
| 121 | Local Route Weather Assessment | operational requirement | Variation between segments | Segment comparison table | Route weather | `rwi.api`, `web/` | Differences computed from stated samples | P1 | implementing |
| 122 | Rain Risk Assessment | operational requirement | Safety, visibility penalty, irradiance | Probability/amount at ETA; alert | precipitation*, ETA | `rwi.alerts.rules`, `rwi.providers` | Alert on fixture | P0 | implementing |
| 123 | Solar Forecast Uncertainty | operational requirement | Energy decisions need an error band | Ensemble spread and, later, verified error | Ensemble, history | `EnergyEstimate.uncertainty_wh` (field only) | Band from members; coverage checked later | P0 | implementing |
| 124 | Dynamic Strategy Adaptation | operational requirement | Update advice as weather changes | Explicit-interval re-evaluation | Live state | Planned `rwi.strategy` + `rwi.api` | Stable in replay (no flip-flop) | P1 | not started |
| 125 | Speed Strategy Support | operational requirement | Weather inputs to speed choice | Scenario evaluation within limits (60 km/h minimum, posted maximum) | Segments, vehicle | `rwi.strategy` | Infeasible scenarios reported with reasons | P0 | implementing |
| 126 | Control Stop Analysis | operational requirement | 30 min stops; charging while stationary (Reg 3.27) | Stationary tilted-collector mode, dwell time | Control-stop list (unpublished) | `rwi.route`, `rwi.energy`, `rwi.strategy` | Dwell energy equals POA x area x time | P1 | implementing |
| 127 | Real-Time Weather Alerts | operational requirement | Hazards and data failures | Rule-based alerts with explicit assumed thresholds | Series, state | `rwi.alerts.rules`, `rwi.api` | Alert tests; no alert from fabricated data | P0 | tested |
| 128 | Historical Route Analysis | operational requirement | Past weather along the route | Archive plus storage (ADR 0006) | Archive | `rwi.providers`; no store | Labelled historical | P2 | not started |
| 129 | Forecast Performance Monitoring | operational requirement | Know in-race accuracy | Running verification vs observations | Observations (source unresolved) | Planned verification module | Metrics reproducible | P1 | not started |
| 130 | Strategic Communication | operational requirement | Clear, timely recommendations | Dashboard + `Recommendation` text | n/a | `web/`, `rwi.strategy` | Strategist review | P0 | implementing |

## 4. How design-standard items are embodied

The master specification says not every scientific topic needs a user-interface feature, and that group E is embodied through workflows, validation rules, explanations and decision-support logic. Honest embodiment, by mechanism:

| Mechanism | Items | Where it lives |
|---|---|---|
| Validation rule enforced in code | 82, 83, 92, 100 | Pydantic validators in `contracts.py`/`core` (unit check, night-zero, provenance required, `None` is not zero); `rwi.quality` flags |
| Uncertainty disclosure | 53, 100, 123 | `Uncertainty` type; UI states 'not available' rather than inventing a band |
| Evidence and explanation fields | 81, 86, 88, 96 | `Recommendation.evidence/rationale/uncertainty_notes/safety_and_regulatory_notes`; `Alert.evidence` |
| Workflow | 85, 87, 90, 94, 95 | Degradation table (ARCHITECTURE section 8), update-interval policy (section 7.5), verification report, alert ordering |
| Test or process standard | 82, 98, 97, 84, 91, 93 | `@pytest.mark.science` reference tests, leakage-safe evaluation rule, anomaly log, minimum-sample rule, evidence-based model selection |
| Documentation standard | 1, 10, 14, 89, 98 | `docs/science/*`, ADRs, `docs/CONTRACTS.md`, briefing notes |
| Decision not to build | 9, 11, 18, 44, 62, 63, 64 | Recorded here so the absence is deliberate, not forgotten |

These items cannot reach 'validated against real data' by themselves: for a behaviour standard the closest honest evidence is a passing rule test plus review during a rehearsal. Nothing here claims human qualities for the software (master specification section 4, group E).

## 5. Summary counts

By priority:

| Priority | Count |
|---|---|
| P0 | 54 |
| P1 | 41 |
| P2 | 21 |
| R&D | 14 |
| **Total** | **130** |

By status:

| Status | Count |
|---|---|
| not started | 41 |
| researching | 3 |
| implementing | 61 |
| implemented, not validated | 0 |
| tested | 25 |
| validated against real data | 0 |
| operationally ready | 0 |
| **Total** | **130** |

By category:

| Category | Count |
|---|---|
| operational requirement | 72 |
| scientific foundation | 20 |
| professional-behaviour standard | 20 |
| optional research | 18 |
| **Total** | **130** |

Priority by status:

| Priority | not started | researching | implementing | tested |
|---|---|---|---|---|
| P0 | 0 | 0 | 40 | 14 |
| P1 | 13 | 1 | 17 | 10 |
| P2 | 16 | 0 | 4 | 1 |
| R&D | 12 | 2 | 0 | 0 |

## 6. P0 backlog: P0 items not yet implemented

Status below 'implemented, not validated' (not started, researching, implementing). This is the real backlog. Items that are *tested* are not listed even though none is validated against real data.

| # | Competency | Status | Component(s) involved |
|---|---|---|---|
| 20 | Mathematics and Statistics | implementing | `rwi.energy.series.compare_with_measured`; planned verification module (not in CONTRACTS.md) |
| 21 | Temperature Analysis | implementing | `rwi.quality` |
| 22 | Wind Analysis | implementing | `rwi.core.wind`, `rwi.quality` |
| 23 | Wind Gust Analysis | implementing | `rwi.alerts.rules`, `rwi.quality` |
| 35 | Data Quality Control | implementing | `rwi.quality` |
| 36 | Real-Time Monitoring | implementing | `rwi.api` (health/state), `web/` |
| 42 | Short-Range Forecasting | implementing | `rwi.providers` |
| 45 | Temperature Forecasting | implementing | `rwi.providers` |
| 46 | Wind Forecasting | implementing | `rwi.providers`, `rwi.core.wind` |
| 47 | Wind Gust Forecasting | implementing | `rwi.providers`, `rwi.alerts.rules` |
| 48 | Cloud Cover Forecasting | implementing | `rwi.providers` |
| 49 | Precipitation Forecasting | implementing | `rwi.providers`, `rwi.alerts.rules` |
| 50 | Solar Irradiance Forecasting | implementing | `rwi.providers`, `rwi.solar`, `rwi.api.integration` |
| 53 | Forecast Uncertainty Analysis | implementing | `rwi.core.provenance.Uncertainty`; ensemble not built |
| 66 | API Integration | implementing | `rwi.providers` |
| 68 | Numerical Modeling | implementing | `rwi.energy`, `rwi.vehicle`, `rwi.strategy` |
| 72 | Data Visualization | implementing | `web/` |
| 78 | Geospatial Programming | implementing | `rwi.core.geo`, `rwi.route` |
| 80 | System Integration | implementing | `rwi.api.integration`, `rwi.contracts` |
| 83 | Critical Thinking | implementing | n/a - design standard (`rwi.quality`, comparison) |
| 86 | Decision-Making Ability | implementing | `rwi.strategy` (`Recommendation`) |
| 87 | Stress Resilience | implementing | n/a - design standard (failure-injection tests) |
| 92 | Responsibility | implementing | n/a - design standard (`rwi.alerts`, `Recommendation`) |
| 98 | Structured Working Methods | implementing | n/a - design standard (repository practice) |
| 99 | Systems Thinking | implementing | `rwi.api.integration` |
| 100 | Uncertainty Awareness | implementing | n/a - design standard (`Uncertainty`, UI disclosure) |
| 102 | Solar Irradiance Analysis | implementing | `rwi.solar`, `rwi.route`, `rwi.api` |
| 105 | GHI Forecasting | implementing | `rwi.providers`, `rwi.solar` |
| 106 | DNI Forecasting | implementing | `rwi.solar.decomposition`, `rwi.providers` |
| 107 | DHI Forecasting | implementing | `rwi.solar.decomposition`, `rwi.providers` |
| 110 | Headwind Forecasting | implementing | `rwi.core.wind`, `rwi.route`, `rwi.api` |
| 111 | Tailwind Forecasting | implementing | `rwi.core.wind`, `rwi.route`, `rwi.api` |
| 115 | Solar Energy Production Forecasting | implementing | `rwi.energy` |
| 116 | Energy Balance Analysis | implementing | `rwi.strategy`, `rwi.vehicle`, `rwi.energy` |
| 117 | Route-Based Forecasting | implementing | `rwi.route`, `rwi.providers`, `rwi.api` |
| 118 | ETA-Linked Weather Analysis | implementing | `rwi.route.trajectory`, `rwi.contracts.WeatherSeries.nearest_step` |
| 122 | Rain Risk Assessment | implementing | `rwi.alerts.rules`, `rwi.providers` |
| 123 | Solar Forecast Uncertainty | implementing | `EnergyEstimate.uncertainty_wh` (field only) |
| 125 | Speed Strategy Support | implementing | `rwi.strategy` |
| 130 | Strategic Communication | implementing | `web/`, `rwi.strategy` |

40 of 54 P0 items.

P0 items at 'tested' or higher (14): #4 Wind Dynamics, #8 Solar Radiation, #29 Solar Irradiance Measurement, #33 GPS and Geospatial Analysis, #61 Python, #77 Error Handling, #82 Accuracy, #96 Risk Awareness, #101 Solar Position Calculation, #108 Aerodynamic Wind Resistance Analysis, #109 Crosswind Analysis, #114 Solar Panel Temperature Analysis, #119 GPS Synchronization, #127 Real-Time Weather Alerts. 'Tested' here means unit tests pass; none is validated against real data.

### Components needed by the matrix that no one owns yet

`docs/CONTRACTS.md` assigns modules for solar, providers, quality, route, energy, vehicle, strategy, alerts, API and web. The following capabilities appear in the matrix but have no module or owner, and need a decision before Phase 2:

1. **Forecast verification and calibration** (#20, 53, 55, 57, 59, 75, 76, 94, 129): metrics, storage of forecast/observation pairs (ADR 0006 triggers), reports.
2. **Multi-model / ensemble comparison and fusion** (#54-56).
3. **Telemetry ingestion and live-GPS source adapter** (#29, 33 live mode, 79).
4. **Satellite processing and nowcasting** (#31, 104, 120).
5. **Automation / scheduled refresh** (#74) and the update-interval policy (#90, 124).
6. **AI assistant layer** (master specification section 12): not a numbered competency but a Phase 5 deliverable; it reads structured outputs only.

## 7. Items blocked on inputs the team or organisers must supply

Independent work continues around these; the platform uses explicit, labelled assumptions rather than invented values (`parameter_source` on `ArrayConfig`/`VehicleConfig`).

| Missing input | Rows affected |
|---|---|
| Vehicle parameters (mass, CdA, rolling resistance, drivetrain efficiency, auxiliary power, yaw sensitivity) | #86, #108, #113, #116, #125, #126 |
| Solar array and battery specification (area, cell type, temperature coefficient, MPPT efficiency, usable energy; Group B, due 5 Mar 2027) | #114, #115, #116, #123, #126 |
| Telemetry protocol and link (fields, rate, receiver) | #29, #34, #73, #79, #87, #129 |
| A licensed real observation source (BoM licence path or team sensors) | #26, #27, #32, #34, #37, #59, #128, #129 |
| Official 2027 route geometry, control stops, driver-change points (Route Notes not published as of 2026-11-01) | #17, #39, #102, #110, #111, #117, #118, #121, #126, #128 |
| Segment-level posted speed limits (not in Regulations or Guide) | #86, #125 |
| Competition class and team decision on regulation limits (6 m2, 11 MJ apply to Challenger) | #115, #116, #125 |
| Organiser answers: weather data and external-computation permission, telemetry link, tracker data, strategy workstation location | #36, #79, #92, #130 |
| Forecast verification history (needs months of stored pairs; does not exist) | #53, #55, #57, #58, #59, #69, #73, #76, #94, #97, #123 |

Source of the regulation-related blockers: `docs/research/bwsc-2027-regulations.md` section 5 (questions for the organisers) and section 6 (inputs from the team). Provider licence blockers: `docs/research/data-sources.md`.

## 8. Maintenance

* Update the status of a row only when the evidence on disk changes, and say which evidence (tests, file). A passing unit test never upgrades a row beyond 'tested'.
* 'Validated against real data' requires a stated dataset (telemetry or observations), method and result in `docs/science/<module>.md`.
* When `docs/CONTRACTS.md` changes, re-check the component column.
* This file is regenerated from a table plus a disk scan; when editing by hand keep the vocabulary exact.

