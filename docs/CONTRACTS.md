# Module contracts and ownership — JST-RWI

Coordination document for parallel development. Read before touching code.

## 1. The rule that makes parallel work possible

`src/rwi/core/**` and `src/rwi/contracts.py` define **all shared types**.
Feature modules implement **behaviour** against those types and must not
redefine them, widen them locally, or duplicate a field with a different unit.

If a contract genuinely needs a new field, do not edit `contracts.py` yourself —
report it; the integrator changes it once.

## 2. File ownership

| Area | Owner | Paths (exclusive) |
|---|---|---|
| Core + contracts + API + alerts | integrator | `src/rwi/core/**`, `src/rwi/contracts.py`, `src/rwi/config.py`, `src/rwi/api/**`, `src/rwi/alerts/**`, `tests/unit/test_core_*.py`, `tests/conftest.py`, `pyproject.toml`, `README.md` |
| Solar geometry + irradiance | solar | `src/rwi/solar/**`, `tests/unit/test_solar_*.py`, `docs/science/solar.md` |
| Weather providers + QC | providers | `src/rwi/providers/**`, `src/rwi/quality/**`, `tests/unit/test_provider_*.py`, `tests/unit/test_quality_*.py`, `tests/integration/test_provider_live.py`, `tests/fixtures/**`, `docs/science/providers.md` |
| Route, projection, ETA | route | `src/rwi/route/**`, `tests/unit/test_route_*.py`, `data/routes/**`, `docs/science/route.md` |
| PV energy model | energy | `src/rwi/energy/**`, `tests/unit/test_energy_*.py`, `docs/science/pv.md` |
| Vehicle dynamics + strategy | vehicle | `src/rwi/vehicle/**`, `src/rwi/strategy/**`, `tests/unit/test_vehicle_*.py`, `tests/unit/test_strategy_*.py`, `docs/science/vehicle.md` |
| Requirements + architecture docs | docs | `docs/REQUIREMENTS.md`, `docs/ARCHITECTURE.md`, `docs/adr/**` |
| Dashboard | web | `web/**` |

Never edit a path you do not own. Never run `git commit`, `git checkout`,
`git push` or `uv pip install` — the integrator manages the repository and the
environment. If a dependency is missing, say so instead of installing it.

## 3. Required public API per module

Signatures are binding. Keyword-only where shown.

### `rwi.solar`
```python
# rwi/solar/geometry.py
def solar_position(point: GeoPoint, when: datetime) -> SolarPosition
def solar_positions(point: GeoPoint, times: Sequence[datetime]) -> list[SolarPosition]   # vectorised
def solar_day_events(point: GeoPoint, when: datetime) -> SolarDayEvents   # local day containing `when`

# rwi/solar/clearsky.py
def clear_sky_irradiance(point: GeoPoint, when: datetime, *,
                         pressure_pa: float | None = None,
                         temperature_c: float | None = None,
                         linke_turbidity: float | None = None) -> IrradianceComponents
def clear_sky_series(point: GeoPoint, times: Sequence[datetime], **kw) -> list[IrradianceComponents]
def extraterrestrial_horizontal_w_m2(point: GeoPoint, when: datetime) -> float

# rwi/solar/decomposition.py
def decompose_ghi(ghi_w_m2: float, solar: SolarPosition, *, model: str = "erbs",
                  provenance: Provenance) -> IrradianceComponents
```
Hard requirements: `apparent_elevation_deg <= 0` ⇒ `ghi = dni = dhi = 0.0`
exactly; `kind=DataKind.CLEAR_SKY_MODEL` for clear-sky output; all geometry
from pvlib (NREL SPA), never a hand-rolled approximation.

### `rwi.providers`
```python
# rwi/providers/base.py
class BaseProvider:  # implements the WeatherProvider protocol, owns retry/backoff/caching hooks
# rwi/providers/openmeteo.py
class OpenMeteoProvider(BaseProvider)        # forecast + archive + ensemble
# rwi/providers/registry.py
def available_providers(settings) -> dict[str, WeatherProvider]
async def fetch_with_fallback(providers, point, window, variables, *, kind) -> WeatherSeries
```
Hard requirements: canonical variable names and units at the boundary
(hPa→Pa, km/h→m/s); full `Provenance` including `model_init_time_utc` when the
API exposes it; `CapabilityNotSupportedError` instead of substituting a
forecast for an observation; offline mode must raise or serve cache, never
invent numbers.

### `rwi.quality`
```python
def check_series(series: WeatherSeries, *, now: datetime | None = None,
                 max_age_s: float | None = None) -> WeatherSeries   # flags, never deletes
def check_value(value: Measured, *, previous: Measured | None = None) -> Measured
def summarise(series: WeatherSeries) -> QualityReport
```

### `rwi.route`
```python
def load_route_geojson(path: Path, *, route_id: str, name: str) -> Route
def load_route_gpx(path: Path, *, route_id: str, name: str) -> Route
def project_onto_route(route: Route, point: GeoPoint, *, tolerance_m: float = 500.0) -> RouteProjection
def point_at_distance(route: Route, distance_m: float) -> tuple[GeoPoint, float]   # (point, heading_deg)
def build_trajectory(route: Route, *, start_distance_m: float, start_time_utc: datetime,
                     speed_mps: float | Callable[[float], float],
                     sample_spacing_m: float = 25_000.0,
                     honour_control_stops: bool = True,
                     daily_window_local: tuple[time, time] | None = None) -> Trajectory
def elevation_profile(route: Route) -> list[tuple[float, float]]
```

### `rwi.energy`
```python
def poa_irradiance(solar: SolarPosition, irradiance: IrradianceComponents, *,
                   surface_tilt_deg: float, surface_azimuth_deg: float,
                   albedo: float = 0.2, model: str = "haydavies") -> POAIrradiance
def cell_temperature_c(poa_global_w_m2: float, air_temperature_c: float,
                       wind_speed_mps: float, *, model: str = "faiman",
                       noct_c: float | None = None) -> float
def array_power(poa: POAIrradiance, array: ArrayConfig, *, air_temperature_c: float | None,
                wind_speed_mps: float | None) -> PowerEstimate
def integrate_energy(estimates: Sequence[PowerEstimate]) -> EnergyEstimate
def surface_azimuth_for_heading(array: ArrayConfig, heading_deg: float | None) -> float
```

### `rwi.vehicle` / `rwi.strategy`
```python
def consumption(vehicle: VehicleConfig, *, speed_mps: float, grade: float = 0.0,
                air_density_kg_m3: float | None = None,
                relative_wind: RelativeWind | None = None) -> ConsumptionEstimate
def evaluate_scenario(...) -> ScenarioResult
def compare_scenarios(...) -> list[ScenarioResult]
def recommend(results: Sequence[ScenarioResult], ...) -> Recommendation
```

## 3b. Phase 2 modules — forecast intelligence

New ownership (same rules: own your paths, never edit `contracts.py`, never run
git or install packages).

| Area | Owner | Paths (exclusive) |
|---|---|---|
| SQLite forecast/observation archive | archive | `src/rwi/archive/**`, `tests/unit/test_archive_*.py`, `docs/science/archive.md` |
| Forecast verification and scoring | verification | `src/rwi/verification/**`, `tests/unit/test_verification_*.py`, `docs/science/verification.md` |
| Ensembles and uncertainty | ensemble | `src/rwi/ensemble/**`, `tests/unit/test_ensemble_*.py`, `docs/science/ensemble.md` |
| Multi-model comparison | fusion | `src/rwi/fusion/**`, `tests/unit/test_fusion_*.py`, `docs/science/fusion.md` |
| Capture worker, API, integration | integrator | `src/rwi/capture/**`, `src/rwi/api/**` |

### `rwi.archive` — implements the `ForecastArchive` protocol
```python
class SqliteArchive:                       # ForecastArchive
    def __init__(self, path: Path | str, *, read_only: bool = False) -> None
    def record_series(self, series: WeatherSeries) -> int        # rows written
    def pairs(*, variable, period=None, provider=None, model_name=None,
              max_separation_m=50_000.0, max_time_offset_s=1800.0
              ) -> list[ForecastObservationPair]
    def coverage(self) -> dict[str, object]
    def prune(self, *, older_than_utc: datetime, dry_run: bool = False) -> int
    def close(self) -> None
def open_archive(settings) -> SqliteArchive
```
Hard requirements: a recorded forecast is immutable (a later run for the same
valid time is a new row); full provenance stored with every record; a pair is
only produced when the forecast was recorded **before** its valid time;
WAL mode; schema version in `PRAGMA user_version`; a corrupt or missing archive
degrades to "verification unavailable", never breaks a weather request.

### `rwi.verification`
```python
def score_pairs(pairs: Sequence[ForecastObservationPair], *, variable: str,
                unit: str, lead_time_buckets_s: Sequence[tuple[float, float]] | None = None,
                now: datetime | None = None) -> list[VerificationScore]
def verify(archive, *, variables, period=None, providers=None) -> VerificationReport
def persistence_baseline(pairs) -> float | None
def bias_correction(scores, *, variable, provider, model_name, lead_time_s) -> float | None
def apply_bias_correction(series: WeatherSeries, scores) -> WeatherSeries
```
Hard requirements: MAE, RMSE and bias computed per variable **and lead-time
bucket**; skill stated against the persistence baseline; `sample_count` on
every score; wind direction scored with circular differences, never linear;
a bias correction is returned only where `is_meaningful` holds.

### `rwi.ensemble`
```python
def summarise(forecast: EnsembleForecast, *, variable: str,
              percentiles: Sequence[int] = (10, 25, 50, 75, 90)) -> list[EnsembleSummary]
def probability_above(forecast, *, variable, threshold, valid_time_utc) -> float
def attach_uncertainty(series: WeatherSeries, summaries) -> WeatherSeries
def spread_skill_note(summary) -> str
```
Hard requirements: percentiles from the members that actually ran; a
member-frequency is labelled as such and never as a calibrated probability;
circular statistics for wind direction; `attach_uncertainty` must not alter any
value, only attach `Uncertainty`.

### `rwi.fusion`
```python
def compare_models(series_by_model: dict[str, WeatherSeries], *, variable: str,
                   significance_thresholds: dict[str, float] | None = None
                   ) -> ModelComparison
def disagreements(series_by_model, *, variable, threshold) -> list[ModelDisagreement]
def preferred_model(scores: Sequence[VerificationScore], *, variable,
                    lead_time_s) -> tuple[str | None, str]
```
Hard requirements: `preferred_model` returns `None` with a reason unless a
score `is_meaningful` and beats persistence; disagreement thresholds are
declared per variable with their justification; no model is ever called "best"
without a score behind it.

## 4. HTTP API (owned by the integrator; the dashboard codes against this)

All responses are JSON; every numeric payload carries `provenance`.
`GET` unless stated. Errors use `{"error": {"code", "message", "detail"}}` with
409 for "position unavailable" and 503 for provider failure.

```
GET  /api/health                      -> {status, version, server_time_utc, uptime_s,
                                          capabilities, unavailable_modules, providers,
                                          provider_errors, network_enabled,
                                          freshness_limits_s:{observation,forecast,gps}}
GET  /api/regulations                 -> verified BWSC 2027 limits with clause numbers,
                                         plus not_yet_published and silent_in_regulations
GET  /api/state                       -> {state: ActiveState, effective_time_utc, local_time,
                                          timezone_name, utc_offset_hours, position_available,
                                          is_simulated, position_age_s, server_time_utc}
POST /api/state/position              {mode, latitude_deg, longitude_deg, elevation_m?, heading_deg?, source?}
POST /api/state/time                  {selected_time_utc|null}
GET  /api/solar/position?lat&lon&elev&time        -> SolarPosition
GET  /api/solar/day?lat&lon&date                  -> SolarDayEvents
GET  /api/solar/clearsky?lat&lon&time             -> IrradianceComponents
GET  /api/solar/clearsky/day?lat&lon&date&step_s  -> [IrradianceComponents]
GET  /api/weather/forecast?lat&lon&hours&variables&provider -> WeatherSeries
GET  /api/weather/observations?lat&lon&hours&variables      -> WeatherSeries, or 501 when no
                                         configured provider MEASURES the requested variables
                                         (never a forecast in their place), or 503 when the
                                         nearest station is too far away
GET  /api/weather/providers            -> [ProviderCapabilities]
GET  /api/weather/observable           -> {provider: [variables it measures]}
GET  /api/weather/variables            -> the canonical registry with units and ranges
GET  /api/route                       -> [RouteSummary: {id, name, total_distance_m,
                                          waypoint_count, control_stop_count,
                                          source_description, provenance_label}]
                                          (counts only; the stops themselves come from
                                           /api/route/{id} or /{id}/geojson)
GET  /api/route/{id}/geojson          -> GeoJSON LineString + waypoint features
GET  /api/route/{id}                   -> Route
GET  /api/route/{id}/progress          -> {projection, distance_remaining_m, next waypoint
                                           and next control stop with distances}
GET  /api/route/{id}/trajectory?speed_kmh&start_distance_m&hours -> Trajectory, truncated to
                                         the horizon (assumptions record what was dropped)
GET  /api/route/{id}/weather?speed_kmh&start_distance_m&hours    -> {forecast:
                                         RouteWeatherForecast, local_arrival_times, notes};
                                         at most 12 sampled points per request, by design, to
                                         respect provider rate limits - trajectory rows beyond
                                         those points legitimately have no weather
GET  /api/energy/array                 -> ArrayConfig in use (parameter_source says if measured)
GET  /api/energy/forecast?hours&heading_deg&provider -> {estimates:[PowerEstimate], energy:
                                         EnergyEstimate|null, array, notes, weather_provider,
                                         weather_kind, weather_retrieved_at_utc}
GET  /api/strategy/vehicle             -> VehicleConfig in use
GET  /api/strategy/scenarios?route_id&speed_kmh&start_soc&hours&start_distance_m&
     posted_limit_kmh&uncertainty      -> {scenarios, recommendation,
                                         optimal_constant_speed_kmh, optimal_is_feasible,
                                         vehicle, alerts, assumptions (echoes every default
                                         applied), data_gaps, regulatory_notes,
                                         human_review_required}
GET  /api/alerts                      -> [Alert]
```

## 4b. Contract clarifications agreed during implementation

1. **`ArrayConfig.surface_azimuth_deg` under `vehicle_relative` mounting** is a
   bearing *relative to the vehicle heading*: `None`/`0` means the surface faces
   the heading, `+90` means it faces heading + 90 deg. For `fixed_tilt` it is an
   absolute azimuth. A `vehicle_relative` array with an unknown heading raises
   `ModelConfigurationError` rather than assuming a direction.
2. **`decompose_ghi` on an OBSERVATION input returns `DataKind.DERIVED`**, not
   `OBSERVATION`. Only GHI was measured; the DNI/DHI split is a model estimate,
   so calling it a measurement would be a provenance lie. Forecast and
   clear-sky inputs keep their kind.
3. **A provider's positive irradiance while the Sun is below the horizon is
   clipped to zero** and flagged, at both the decomposition boundary and the
   API integration boundary. Geometry wins over a grid-interpolated forecast.
4. **`ArrayConfig` has no soiling, glass-reflection, mismatch or Faiman u0/u1
   fields** in phase 1. They live as documented module constants in
   `rwi.energy` until there is measured data to fit them.

### 4c. Known API limitations, by design

* `/api/alerts` carries weather and data-integrity alerts only. Strategy alerts
  (for example "no feasible scenario") need the scenario inputs and therefore
  arrive inside the `/api/strategy/scenarios` response.
* There is no battery, telemetry, archive or forecast-verification endpoint:
  those subsystems do not exist yet, and the History view says so rather than
  showing an empty chart.
* Route weather samples at most 12 points per request. Denser sampling would
  need a provider plan that permits it.

## 5. Definition of done for every module

1. Public functions exactly as specified above, fully type annotated.
2. Docstrings stating units, frame of reference, model name and references.
3. `pytest` tests: happy path, boundary, failure, and at least one test with a
   **documented external reference value** (marked `@pytest.mark.science`).
4. No network access in unit tests — use `tests/fixtures/**` and `respx`.
5. `docs/science/<module>.md`: methods, formulas, assumptions, limitations,
   validation status in the vocabulary of §17 of the master prompt
   (*not started / researching / implementing / implemented, not validated /
   tested / validated against real data / operationally ready*).
6. Honest status reporting: say what is untested or assumed.
