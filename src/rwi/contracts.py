"""Cross-module interface layer.

Every type that crosses a module boundary is defined here and nowhere else, so
that `rwi.solar`, `rwi.providers`, `rwi.route`, `rwi.energy`, `rwi.vehicle`,
`rwi.strategy` and `rwi.api` can be developed and tested independently against
a fixed contract. Modules own *behaviour*; this module owns the *shapes*.

Conventions that apply to everything here:
  * times are aware UTC datetimes (`rwi.core.timebase`);
  * units are the canonical units of `rwi.core.variables`;
  * irradiance W/m2, power W, energy Wh - never mixed;
  * angles in degrees; wind directions are "from", clockwise from true north;
  * every quantitative payload carries a `Provenance`.
"""

from __future__ import annotations

import bisect
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from enum import StrEnum
from itertools import pairwise
from typing import Literal, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rwi.core.geo import GeoPoint
from rwi.core.position import PositionFix
from rwi.core.provenance import DataKind, Measured, Provenance, QualityFlag, Uncertainty
from rwi.core.timebase import TimeWindow, ensure_utc
from rwi.core.wind import RelativeWind

# ---------------------------------------------------------------------------
# Solar geometry and irradiance (implemented by rwi.solar)
# ---------------------------------------------------------------------------


class SolarPosition(BaseModel):
    """Apparent position of the Sun for one place and one instant.

    ``elevation_deg`` is geometric (no refraction); ``apparent_elevation_deg``
    includes atmospheric refraction and is the one that decides day/night.
    """

    model_config = ConfigDict(frozen=True)

    point: GeoPoint
    time_utc: datetime
    elevation_deg: float = Field(ge=-90.0, le=90.0)
    apparent_elevation_deg: float = Field(ge=-90.0, le=90.0)
    azimuth_deg: float = Field(ge=0.0, le=360.0, description="clockwise from true north")
    zenith_deg: float = Field(ge=0.0, le=180.0)
    apparent_zenith_deg: float = Field(ge=0.0, le=180.0)
    declination_deg: float | None = None
    equation_of_time_min: float | None = None
    air_mass: float | None = Field(
        default=None, description="relative airmass; None when the Sun is below the horizon"
    )
    provenance: Provenance

    @property
    def is_daylight(self) -> bool:
        """True when the Sun's apparent centre is above the astronomical horizon."""
        return self.apparent_elevation_deg > 0.0

    @property
    def is_civil_twilight(self) -> bool:
        return -6.0 < self.apparent_elevation_deg <= 0.0


class SolarDayEvents(BaseModel):
    """Sunrise/sunset/solar-noon for one place and one local day.

    ``sunrise_utc``/``sunset_utc`` are None during polar day or polar night,
    which is a real state at Swedish latitudes in midsummer/midwinter and must
    not be faked with a default time.
    """

    model_config = ConfigDict(frozen=True)

    point: GeoPoint
    local_date: str = Field(description="ISO date in the location's own timezone")
    timezone_name: str | None
    sunrise_utc: datetime | None
    sunset_utc: datetime | None
    solar_noon_utc: datetime
    transit_elevation_deg: float
    day_length_s: float = Field(ge=0.0)
    is_polar_day: bool = False
    is_polar_night: bool = False
    provenance: Provenance


class IrradianceComponents(BaseModel):
    """The three irradiance components on which all PV modelling rests.

    ``kind`` says what this *is*: a measurement, a forecast, or a clear-sky
    physical model result. A clear-sky value is an upper bound under a cloudless
    sky and must never be labelled or displayed as an observation.

    Hard physical constraint, enforced here: when the Sun is below the horizon
    all three components are exactly zero.
    """

    model_config = ConfigDict(frozen=True)

    time_utc: datetime
    point: GeoPoint
    ghi_w_m2: float = Field(ge=0.0, le=1600.0, description="global horizontal irradiance")
    dni_w_m2: float = Field(ge=0.0, le=1400.0, description="direct normal irradiance")
    dhi_w_m2: float = Field(ge=0.0, le=900.0, description="diffuse horizontal irradiance")
    kind: DataKind
    provenance: Provenance
    solar_elevation_deg: float | None = None
    clearness_index: float | None = Field(
        default=None, ge=0.0, le=1.5, description="GHI / extraterrestrial horizontal irradiance"
    )
    quality: QualityFlag = QualityFlag.OK

    @model_validator(mode="after")
    def _enforce_night_zero(self) -> Self:
        night = self.solar_elevation_deg is not None and self.solar_elevation_deg <= 0.0
        if night and max(self.ghi_w_m2, self.dni_w_m2, self.dhi_w_m2) > 0.0:
            raise ValueError(
                "sun below horizon (elevation "
                f"{self.solar_elevation_deg} deg) but irradiance is positive: "
                f"ghi={self.ghi_w_m2}, dni={self.dni_w_m2}, dhi={self.dhi_w_m2}"
            )
        return self

    def as_measured(self) -> dict[str, Measured]:
        """Expose the components as registry-checked `Measured` values."""
        return {
            name: Measured(
                variable=name,
                value=value,
                unit="W/m2",
                provenance=self.provenance,
                quality=self.quality,
            )
            for name, value in (
                ("ghi", self.ghi_w_m2),
                ("dni", self.dni_w_m2),
                ("dhi", self.dhi_w_m2),
            )
        }


class POAIrradiance(BaseModel):
    """Plane-of-array irradiance on the vehicle's panel surface."""

    model_config = ConfigDict(frozen=True)

    time_utc: datetime
    poa_global_w_m2: float = Field(ge=0.0)
    poa_direct_w_m2: float = Field(ge=0.0)
    poa_sky_diffuse_w_m2: float = Field(ge=0.0)
    poa_ground_diffuse_w_m2: float = Field(ge=0.0)
    aoi_deg: float = Field(ge=0.0, le=180.0, description="angle of incidence on the surface")
    surface_tilt_deg: float = Field(ge=0.0, le=90.0)
    surface_azimuth_deg: float = Field(ge=0.0, le=360.0)
    albedo: float = Field(ge=0.0, le=1.0)
    transposition_model: str
    provenance: Provenance


# ---------------------------------------------------------------------------
# Weather data (implemented by rwi.providers, validated by rwi.quality)
# ---------------------------------------------------------------------------


class WeatherStep(BaseModel):
    """All variables a provider gave for one valid time at one point."""

    model_config = ConfigDict(frozen=True)

    valid_time_utc: datetime
    values: dict[str, Measured]

    def get(self, variable: str) -> Measured | None:
        return self.values.get(variable)

    def value(self, variable: str) -> float | None:
        item = self.values.get(variable)
        return item.value if item is not None and item.is_usable else None


class WeatherSeries(BaseModel):
    """A time series of weather for a single point from a single source.

    Observations and forecasts are never mixed inside one series: ``kind``
    applies to the whole series. Fusing sources is the explicit job of a
    higher layer, which must keep the originals.
    """

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    point: GeoPoint
    requested_point: GeoPoint | None = None
    provider: str
    dataset: str
    kind: DataKind
    model_name: str | None = None
    model_version: str | None = None
    model_init_time_utc: datetime | None = None
    retrieved_at_utc: datetime
    spatial_resolution_km: float | None = None
    temporal_resolution_s: float | None = None
    license: str | None = None
    source_url: str | None = None
    steps: list[WeatherStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def _sorted_and_utc(self) -> Self:
        times = [ensure_utc(s.valid_time_utc) for s in self.steps]
        if any(b < a for a, b in pairwise(times)):
            raise ValueError("WeatherSeries steps must be sorted by valid_time_utc")
        return self

    @property
    def times(self) -> list[datetime]:
        return [s.valid_time_utc for s in self.steps]

    @property
    def variables(self) -> frozenset[str]:
        if not self.steps:
            return frozenset()
        return frozenset().union(*(frozenset(s.values) for s in self.steps))

    @property
    def window(self) -> TimeWindow | None:
        if not self.steps:
            return None
        return TimeWindow(
            start_utc=self.steps[0].valid_time_utc, end_utc=self.steps[-1].valid_time_utc
        )

    def series(self, variable: str) -> list[tuple[datetime, float]]:
        """(time, value) pairs for one variable, skipping unusable entries."""
        out: list[tuple[datetime, float]] = []
        for step in self.steps:
            v = step.value(variable)
            if v is not None:
                out.append((step.valid_time_utc, v))
        return out

    def nearest_step(
        self, when: datetime, *, tolerance_s: float | None = None
    ) -> WeatherStep | None:
        """Step closest in time, or None when nothing is within ``tolerance_s``."""
        if not self.steps:
            return None
        target = ensure_utc(when)
        times = self.times
        idx = bisect.bisect_left(times, target)
        candidates = [i for i in (idx - 1, idx) if 0 <= i < len(times)]
        best = min(candidates, key=lambda i: abs((times[i] - target).total_seconds()))
        if tolerance_s is not None and abs((times[best] - target).total_seconds()) > tolerance_s:
            return None
        return self.steps[best]

    def base_provenance(self) -> Provenance:
        """Provenance for the series as a whole (no single valid time)."""
        first_time = self.steps[0].valid_time_utc if self.steps else None
        last_time = self.steps[-1].valid_time_utc if self.steps else None
        return Provenance(
            provider=self.provider,
            dataset=self.dataset,
            kind=self.kind,
            retrieved_at_utc=self.retrieved_at_utc,
            valid_time_utc=first_time if self.kind is DataKind.FORECAST else None,
            observation_time_utc=last_time if self.kind is DataKind.OBSERVATION else None,
            model_name=self.model_name,
            model_version=self.model_version,
            model_init_time_utc=self.model_init_time_utc,
            point=self.point,
            requested_point=self.requested_point,
            spatial_resolution_km=self.spatial_resolution_km,
            temporal_resolution_s=self.temporal_resolution_s,
            license=self.license,
            source_url=self.source_url,
        )


class EnsembleForecast(BaseModel):
    """Several forecast members for the same point, time range and variables."""

    model_config = ConfigDict(frozen=True)

    point: GeoPoint
    provider: str
    model_name: str | None
    retrieved_at_utc: datetime
    members: list[WeatherSeries]

    def member_values(
        self, variable: str, when: datetime, *, tolerance_s: float = 1800.0
    ) -> list[float]:
        values: list[float] = []
        for member in self.members:
            step = member.nearest_step(when, tolerance_s=tolerance_s)
            if step is not None:
                v = step.value(variable)
                if v is not None:
                    values.append(v)
        return values


class ProviderCapabilities(BaseModel):
    """What a provider can actually deliver - checked, not assumed."""

    model_config = ConfigDict(frozen=True)

    provider: str
    datasets: tuple[str, ...]
    supports_observations: bool
    supports_forecast: bool
    supports_ensemble: bool
    supports_archive: bool
    variables: frozenset[str]
    max_forecast_hours: float | None
    spatial_resolution_km: float | None
    update_interval_s: float | None
    license: str
    attribution: str
    documentation_url: str
    requires_api_key: bool = False
    notes: str | None = None


class ProviderHealth(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    reachable: bool
    checked_at_utc: datetime
    latency_ms: float | None = None
    message: str | None = None


@runtime_checkable
class WeatherProvider(Protocol):
    """Adapter interface every weather source implements.

    Implementations must: convert to canonical units and variable names; attach
    full provenance; raise `CapabilityNotSupportedError` rather than silently
    substituting a forecast for an observation; and never fabricate values.
    """

    name: str

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    async def fetch_forecast(
        self, point: GeoPoint, window: TimeWindow, variables: Sequence[str]
    ) -> WeatherSeries: ...

    async def fetch_observations(
        self, point: GeoPoint, window: TimeWindow, variables: Sequence[str]
    ) -> WeatherSeries: ...

    async def health(self) -> ProviderHealth: ...


# ---------------------------------------------------------------------------
# Route and trajectory (implemented by rwi.route)
# ---------------------------------------------------------------------------


class WaypointKind(StrEnum):
    START = "start"
    WAYPOINT = "waypoint"
    CONTROL_STOP = "control_stop"
    FINISH = "finish"
    TOWN = "town"
    HAZARD = "hazard"


class RouteWaypoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    point: GeoPoint
    cumulative_distance_m: float = Field(ge=0.0)
    kind: WaypointKind = WaypointKind.WAYPOINT
    name: str | None = None
    mandatory_dwell_s: float | None = Field(
        default=None, ge=0.0, description="required stop time, e.g. BWSC control stop"
    )
    notes: str | None = None


class Route(BaseModel):
    """A polyline with cumulative distances, loaded from a verified source."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    waypoints: list[RouteWaypoint]
    total_distance_m: float = Field(ge=0.0)
    provenance: Provenance
    source_description: str = Field(description="where the geometry came from and its accuracy")

    @model_validator(mode="after")
    def _monotonic(self) -> Self:
        if len(self.waypoints) < 2:
            raise ValueError("a route needs at least two waypoints")
        d = [w.cumulative_distance_m for w in self.waypoints]
        if any(b < a for a, b in pairwise(d)):
            raise ValueError("cumulative_distance_m must be non-decreasing")
        return self

    @property
    def control_stops(self) -> list[RouteWaypoint]:
        return [w for w in self.waypoints if w.kind is WaypointKind.CONTROL_STOP]


class RouteProjection(BaseModel):
    """Result of matching a GPS fix onto a route."""

    model_config = ConfigDict(frozen=True)

    distance_along_m: float
    cross_track_m: float = Field(description="signed; positive = right of travel direction")
    segment_index: int = Field(ge=0)
    point_on_route: GeoPoint
    heading_deg: float = Field(ge=0.0, le=360.0)
    is_on_route: bool
    tolerance_m: float


class TrajectoryPoint(BaseModel):
    """Where the vehicle is predicted to be, and when."""

    model_config = ConfigDict(frozen=True)

    distance_m: float = Field(ge=0.0)
    point: GeoPoint
    eta_utc: datetime
    heading_deg: float = Field(ge=0.0, le=360.0)
    speed_mps: float = Field(ge=0.0)
    grade: float = Field(default=0.0, description="road gradient, rise/run (dimensionless)")
    waypoint: RouteWaypoint | None = None


class Trajectory(BaseModel):
    """A predicted space-time path, the basis of all route-aware forecasting.

    This is the concrete form of Weather = f(lat, lon, elevation, future time).
    """

    model_config = ConfigDict(frozen=True)

    route_id: str
    generated_at_utc: datetime
    points: list[TrajectoryPoint]
    assumptions: dict[str, str | float | bool] = Field(default_factory=dict)
    provenance: Provenance

    @property
    def arrival_utc(self) -> datetime | None:
        return self.points[-1].eta_utc if self.points else None


class RouteWeatherSample(BaseModel):
    """Weather at one trajectory point, valid at the predicted arrival time."""

    model_config = ConfigDict(frozen=True)

    trajectory_point: TrajectoryPoint
    series_point: GeoPoint
    values: dict[str, Measured]
    lead_time_s: float | None = None
    relative_wind: RelativeWind | None = None


class RouteWeatherForecast(BaseModel):
    model_config = ConfigDict(frozen=True)

    route_id: str
    generated_at_utc: datetime
    samples: list[RouteWeatherSample]
    provider: str
    assumptions: dict[str, str | float | bool] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Photovoltaic array and vehicle (implemented by rwi.energy and rwi.vehicle)
# ---------------------------------------------------------------------------


ParameterSource = Literal["measured", "datasheet", "assumed"]


class ArrayConfig(BaseModel):
    """Solar array description.

    Every parameter records whether it is measured, from a datasheet, or an
    explicit assumption. Nothing here may be presented as a JU Solar Team
    vehicle specification unless `parameter_source` says "measured".
    """

    model_config = ConfigDict(frozen=True)

    name: str
    area_m2: float = Field(gt=0.0)
    cell_efficiency_stc: float = Field(gt=0.0, le=0.5, description="module efficiency at STC, 0-1")
    temperature_coefficient_pmax_per_c: float = Field(
        le=0.0,
        description=(
            "relative dP/dT, 1/degC; negative for silicon. Note clause 2.4.4 prohibits "
            "GaAs, CdTe and CuInSe2 for solar conversion - see rwi.regulations"
        ),
    )
    mppt_efficiency: float = Field(gt=0.0, le=1.0)
    wiring_efficiency: float = Field(gt=0.0, le=1.0)
    mounting: Literal["horizontal", "fixed_tilt", "vehicle_relative"] = "horizontal"
    surface_tilt_deg: float = Field(default=0.0, ge=0.0, le=90.0)
    surface_azimuth_deg: float | None = Field(
        default=None,
        description=(
            "absolute azimuth for fixed_tilt mounting; for vehicle_relative mounting it is "
            "a bearing RELATIVE to the vehicle heading (0 or None = facing the heading, "
            "+90 = tilted about the longitudinal axis facing heading+90)"
        ),
    )
    noct_c: float = Field(default=48.0, description="nominal operating cell temperature")
    albedo: float = Field(default=0.2, ge=0.0, le=1.0)
    angular_response: object | None = Field(
        default=None,
        description=(
            "Optional per-cell angular response (AngularResponseTable) that captures "
            "how each cell sees the sun as the vehicle heading rotates the panel. "
            "None keeps the flat-plane behaviour the rest of this module assumes. "
            "Typed as `object` to avoid a hard import of numpy from the contracts "
            "layer; the energy model does isinstance-check at use."
        ),
    )
    parameter_source: ParameterSource = "assumed"
    source_notes: str | None = None


class PowerEstimate(BaseModel):
    """Modelled electrical power from the array at one instant."""

    model_config = ConfigDict(frozen=True)

    time_utc: datetime
    poa: POAIrradiance
    cell_temperature_c: float
    air_temperature_c: float | None
    wind_speed_mps: float | None
    dc_power_w: float = Field(ge=0.0, description="array output before MPPT/wiring losses")
    net_power_w: float = Field(ge=0.0, description="power delivered to the bus/battery")
    effective_efficiency: float = Field(ge=0.0, le=1.0)
    array: ArrayConfig
    cell_temperature_model: str
    provenance: Provenance


class EnergyEstimate(BaseModel):
    """Energy over a window, integrated from a power series."""

    model_config = ConfigDict(frozen=True)

    window: TimeWindow
    energy_wh: float
    sample_count: int = Field(ge=0)
    integration_method: str = "trapezoidal"
    provenance: Provenance
    uncertainty_wh: float | None = None


class VehicleConfig(BaseModel):
    """Longitudinal dynamics parameters.

    Defaults are deliberately absent for the parameters that decide the answer:
    an unknown vehicle must be configured, not guessed.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    mass_kg: float = Field(gt=0.0, description="vehicle plus driver plus ballast")
    drag_area_cda_m2: float = Field(gt=0.0, description="Cd * A")
    rolling_resistance_coefficient: float = Field(gt=0.0, le=0.05)
    drivetrain_efficiency: float = Field(gt=0.0, le=1.0)
    auxiliary_power_w: float = Field(ge=0.0)
    battery_capacity_wh: float = Field(gt=0.0)
    battery_reserve_fraction: float = Field(ge=0.0, lt=1.0)
    battery_round_trip_efficiency: float = Field(default=0.95, gt=0.0, le=1.0)
    max_legal_speed_kmh: float = Field(default=130.0, gt=0.0)
    yaw_drag_sensitivity_per_deg: float = Field(
        default=0.0,
        ge=0.0,
        description="fractional CdA increase per degree of apparent-wind yaw; 0 until measured",
    )
    parameter_source: ParameterSource = "assumed"
    source_notes: str | None = None


class ConsumptionEstimate(BaseModel):
    """Tractive and auxiliary power demand at one operating point."""

    model_config = ConfigDict(frozen=True)

    speed_mps: float = Field(ge=0.0)
    aerodynamic_power_w: float
    rolling_power_w: float
    gradient_power_w: float
    auxiliary_power_w: float
    total_power_w: float
    relative_wind: RelativeWind | None
    air_density_kg_m3: float
    vehicle: VehicleConfig
    provenance: Provenance


class ScenarioConstraintViolation(BaseModel):
    model_config = ConfigDict(frozen=True)

    constraint: str
    detail: str
    severity: Literal["blocking", "warning"]


class ScenarioResult(BaseModel):
    """Outcome of one candidate driving strategy.

    Decision support only: the platform never issues speed or control commands.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    target_speed_kmh: float
    distance_m: float
    energy_generated_wh: float
    energy_consumed_wh: float
    net_energy_wh: float
    start_soc_fraction: float = Field(ge=0.0, le=1.0)
    end_soc_fraction: float
    arrival_utc: datetime | None
    travel_time_s: float
    feasible: bool
    violations: list[ScenarioConstraintViolation] = Field(default_factory=list)
    assumptions: dict[str, str | float | bool] = Field(default_factory=dict)
    uncertainty_notes: list[str] = Field(default_factory=list)
    provenance: Provenance


class Recommendation(BaseModel):
    """What the platform tells the strategists, with its reasoning attached."""

    model_config = ConfigDict(frozen=True)

    headline: str
    rationale: str
    recommended_scenario: str | None
    alternatives: list[str] = Field(default_factory=list)
    energy_impact_wh: float | None = None
    time_impact_s: float | None = None
    confidence: Literal["low", "medium", "high"]
    uncertainty_notes: list[str] = Field(default_factory=list)
    safety_and_regulatory_notes: list[str] = Field(default_factory=list)
    reevaluate_after_utc: datetime | None = None
    evidence: list[str] = Field(default_factory=list)
    provenance: Provenance


# ---------------------------------------------------------------------------
# Alerts (implemented by rwi.alerts)
# ---------------------------------------------------------------------------


class AlertSeverity(StrEnum):
    INFO = "info"
    WATCH = "watch"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertCategory(StrEnum):
    WIND = "wind"
    GUST = "gust"
    CROSSWIND = "crosswind"
    PRECIPITATION = "precipitation"
    THUNDERSTORM = "thunderstorm"
    VISIBILITY = "visibility"
    HEAT = "heat"
    IRRADIANCE_DROP = "irradiance_drop"
    ENERGY_DEFICIT = "energy_deficit"
    DATA_QUALITY = "data_quality"
    POSITION = "position"
    PROVIDER = "provider"


class Alert(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    severity: AlertSeverity
    category: AlertCategory
    headline: str
    detail: str
    issued_at_utc: datetime
    valid_window: TimeWindow | None = None
    location: GeoPoint | None = None
    distance_along_route_m: float | None = None
    evidence: list[str] = Field(default_factory=list)
    recommended_action: str | None = None
    provenance: Provenance




# ---------------------------------------------------------------------------
# Forecast verification, ensembles and model comparison (phase 2)
#
# These exist because the platform currently has no way to answer "how good is
# this forecast?". Without stored forecast/observation pairs there is no bias
# correction, no calibrated uncertainty and no evidence-based model choice -
# only claims. Everything below is built around one rule: a score is only ever
# computed from a forecast that was recorded BEFORE its valid time, so no
# future information can leak into the evaluation.
# ---------------------------------------------------------------------------


class ArchiveRecordKind(StrEnum):
    FORECAST = "forecast"
    OBSERVATION = "observation"


class ForecastObservationPair(BaseModel):
    """One forecast matched to the measurement it was predicting.

    The unit of verification. ``lead_time_s`` is valid time minus the model's
    initialisation time, which is what forecast skill actually degrades with -
    not the time since we happened to download it.
    """

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    variable: str
    valid_time_utc: datetime
    forecast_value: float
    observed_value: float
    lead_time_s: float = Field(ge=0.0)
    provider: str
    model_name: str | None = None
    forecast_point: GeoPoint
    observation_point: GeoPoint
    separation_m: float = Field(
        ge=0.0, description="distance between the forecast grid point and the measuring station"
    )
    time_offset_s: float = Field(
        description="observation time minus forecast valid time; non-zero means a tolerance match"
    )
    forecast_recorded_at_utc: datetime = Field(
        description="when the forecast was archived; must precede valid_time_utc"
    )

    @model_validator(mode="after")
    def _no_future_leakage(self) -> Self:
        if ensure_utc(self.forecast_recorded_at_utc) > ensure_utc(self.valid_time_utc):
            raise ValueError(
                "forecast was recorded after its own valid time "
                f"({self.forecast_recorded_at_utc.isoformat()} > "
                f"{self.valid_time_utc.isoformat()}): this is not a forecast of that instant "
                "and must not be scored as one"
            )
        return self

    @property
    def error(self) -> float:
        """Forecast minus observation. Positive means the forecast was too high."""
        return self.forecast_value - self.observed_value


class VerificationScore(BaseModel):
    """Measured skill of one model, for one variable, over one lead-time bucket.

    Deterministic scores only. ``sample_count`` is reported with every score
    because an MAE over three pairs is not evidence of anything, and the UI must
    be able to say so.
    """

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    variable: str
    provider: str
    model_name: str | None
    lead_time_bucket_s: tuple[float, float] = Field(
        description="half-open [lower, upper) lead-time range the score covers"
    )
    sample_count: int = Field(ge=0)
    mean_absolute_error: float | None
    root_mean_square_error: float | None
    bias: float | None = Field(default=None, description="mean forecast minus mean observation")
    mean_observed: float | None = None
    mean_forecast: float | None = None
    persistence_mae: float | None = Field(
        default=None, description="MAE of the trivial 'nothing changes' baseline"
    )
    skill_vs_persistence: float | None = Field(
        default=None,
        description="1 - MAE/persistence_MAE; positive means better than the trivial baseline, "
        "negative means worse. A model that cannot beat persistence has not earned its place.",
    )
    unit: str
    period: TimeWindow | None = None
    provenance: Provenance

    @property
    def is_meaningful(self) -> bool:
        """Whether there are enough pairs for the score to mean anything.

        Thirty is a convention, not a theorem; it is stated so the UI can show
        a score as provisional rather than silently implying confidence.
        """
        return self.sample_count >= 30


class VerificationReport(BaseModel):
    """All scores computed in one evaluation, with its coverage."""

    model_config = ConfigDict(frozen=True)

    scores: list[VerificationScore]
    computed_at_utc: datetime
    period: TimeWindow | None
    pair_count: int = Field(ge=0)
    variables: tuple[str, ...] = ()
    notes: list[str] = Field(default_factory=list)

    @property
    def has_evidence(self) -> bool:
        return any(score.is_meaningful for score in self.scores)


class EnsembleSummary(BaseModel):
    """Distribution of an ensemble at one time and place, for one variable.

    This is the platform's only honest source of forecast uncertainty today:
    the spread of members that were actually run, rather than an error bar
    invented after the fact.
    """

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    variable: str
    valid_time_utc: datetime
    point: GeoPoint
    unit: str
    member_count: int = Field(ge=1)
    mean: float
    median: float
    minimum: float
    maximum: float
    standard_deviation: float = Field(ge=0.0)
    percentiles: dict[str, float] = Field(
        description="keyed by percentile as a string, e.g. {'10': ..., '90': ...}"
    )
    provider: str
    model_name: str | None
    provenance: Provenance

    def as_uncertainty(
        self, *, lower_percentile: str = "10", upper_percentile: str = "90"
    ) -> Uncertainty:
        """Ensemble spread expressed as the platform's uncertainty type."""
        lower = self.percentiles.get(lower_percentile)
        upper = self.percentiles.get(upper_percentile)
        probability: float | None = None
        if lower is not None and upper is not None:
            probability = (float(upper_percentile) - float(lower_percentile)) / 100.0
        return Uncertainty(
            standard_deviation=self.standard_deviation,
            lower=lower,
            upper=upper,
            interval_probability=probability,
            method=(
                f"ensemble_spread ({self.member_count} members, "
                f"{self.model_name or self.provider})"
            ),
        )

    # Deliberately no probability_above() here. A summary holds statistics, not
    # the members, so it cannot compute a member frequency: that lives in
    # rwi.ensemble.probability_above(forecast, ...), which has the members. A
    # method that always raised would be a trap for the next engineer.


class ModelDisagreement(BaseModel):
    """How far apart the available models are at one time and place.

    Disagreement is information: it is the cheapest available signal that a
    forecast should not be acted on without a margin.
    """

    model_config = ConfigDict(frozen=True)

    variable: str
    valid_time_utc: datetime
    point: GeoPoint
    unit: str
    values_by_model: dict[str, float]
    spread: float = Field(ge=0.0, description="max minus min across models")
    standard_deviation: float = Field(ge=0.0)
    mean: float
    median: float
    is_significant: bool = Field(
        description="spread exceeds the variable's configured significance threshold"
    )
    threshold: float
    provenance: Provenance

    @property
    def model_count(self) -> int:
        return len(self.values_by_model)


class ModelComparison(BaseModel):
    """Several models for the same point and period, side by side."""

    model_config = ConfigDict(frozen=True)

    point: GeoPoint
    variable: str
    generated_at_utc: datetime
    series_by_model: dict[str, list[tuple[datetime, float]]]
    disagreements: list[ModelDisagreement] = Field(default_factory=list)
    preferred_model: str | None = Field(
        default=None,
        description="model with the best verified skill for this variable, when evidence exists; "
        "None means no model has earned preference yet",
    )
    preference_basis: str | None = None
    notes: list[str] = Field(default_factory=list)


@runtime_checkable
class ForecastArchive(Protocol):
    """Durable store of forecasts and observations, for verification.

    Implementations must preserve the original provenance of everything they
    store, must never overwrite a recorded forecast with a later revision of
    the same valid time (that would erase the evidence), and must be able to
    answer which pairs exist without loading the whole archive.
    """

    def record_series(self, series: WeatherSeries) -> int: ...

    def pairs(
        self,
        *,
        variable: str,
        period: TimeWindow | None = None,
        provider: str | None = None,
        model_name: str | None = None,
        max_separation_m: float = 50_000.0,
        max_time_offset_s: float = 1800.0,
    ) -> list[ForecastObservationPair]: ...

    def coverage(self) -> dict[str, object]: ...


# ---------------------------------------------------------------------------
# Satellite cloud intelligence (phase 5)
#
# Cloud shading is the fastest way for a solar car to lose power, and a weather
# model on a 9-25 km grid updated every 6 hours cannot see an individual cloud
# band arriving in twenty minutes. Geostationary imagery at 0.5 km every 10
# minutes can.
#
# Two honesty constraints shape every type below. First, a satellite-derived
# irradiance is a RETRIEVAL, not a measurement: nothing on the ground measured
# it, so it is never DataKind.OBSERVATION. Second, we have no ground truth for
# irradiance at all, so no retrieval or nowcast here can be validated - and the
# types carry that statement rather than leaving it to a footnote.
# ---------------------------------------------------------------------------


class SatelliteTile(BaseModel):
    """One tile of one band at one scan time, as published by the provider."""

    model_config = ConfigDict(frozen=True)

    satellite: str
    band: str = Field(description="instrument channel, e.g. 'C03' (0.64 um visible)")
    tile_id: str
    slot_time_utc: datetime = Field(description="nominal scan start time")
    key: str = Field(description="object key or URL at the source")
    size_bytes: int | None = None
    published_at_utc: datetime | None = Field(
        default=None, description="when the file appeared; slot time plus latency"
    )

    @property
    def latency_s(self) -> float | None:
        if self.published_at_utc is None:
            return None
        return (self.published_at_utc - self.slot_time_utc).total_seconds()


class GeoGrid(BaseModel):
    """A georeferenced raster subset: the common currency of this module."""

    model_config = ConfigDict(frozen=True)

    latitudes: list[float] = Field(description="row centres, degrees north")
    longitudes: list[float] = Field(description="column centres, degrees east")
    shape: tuple[int, int]
    resolution_km: float
    bounds: tuple[float, float, float, float] = Field(
        description="(min_lat, min_lon, max_lat, max_lon)"
    )

    @property
    def pixel_count(self) -> int:
        return self.shape[0] * self.shape[1]


class SatelliteScene(BaseModel):
    """One decoded, geolocated image subset over a region of interest.

    ``sun_normalised`` is on this type rather than left to documentation for a
    specific reason. A Level-2 reflectance factor is usually NOT divided by
    cos(solar zenith), while the Heliosat cloud index assumes it is. Wire those
    two together with the convention undeclared and every retrieved irradiance
    carries a sun-angle-dependent bias that grows through the morning and
    evening, is invisible at midday, and would be caught by neither module's
    own tests. With no ground-truth irradiance anywhere in this platform, it
    would never be caught at all. So the producer must state it and the
    consumer must check it.

    The pixel values live outside this model (a numpy array), because a
    thousand-by-thousand grid does not belong in a pydantic object or in JSON.
    This carries what must travel with them.
    """

    model_config = ConfigDict(frozen=True)

    satellite: str
    band: str
    slot_time_utc: datetime
    grid: GeoGrid
    quantity: Literal["reflectance", "brightness_temperature", "cloud_index"]
    unit: str
    solar_zenith_range_deg: tuple[float, float] | None = Field(
        default=None,
        description="min and max solar zenith over the scene; a visible-band product "
        "is meaningless at night and the caller must be able to tell",
    )
    sun_normalised: bool | None = Field(
        default=None,
        description=(
            "whether a reflectance has already been divided by cos(solar zenith). "
            "None means the producer did not declare it, which a retrieval must treat "
            "as an error rather than a default"
        ),
    )
    provenance: Provenance

    @property
    def is_usable_visible(self) -> bool:
        """Whether a visible-band product can mean anything for this scene.

        Reflectance needs sunlight. Past about 80 degrees zenith the signal
        degrades badly, and after sunset there is nothing to reflect.
        """
        if self.quantity == "brightness_temperature":
            return True
        if self.solar_zenith_range_deg is None:
            return False
        return self.solar_zenith_range_deg[0] < 80.0


class CloudMotion(BaseModel):
    """Estimated advection of the cloud field between two scenes."""

    model_config = ConfigDict(frozen=True)

    from_time_utc: datetime
    to_time_utc: datetime
    u_east_m_s: float
    v_north_m_s: float
    speed_m_s: float = Field(ge=0.0)
    direction_to_deg: float = Field(
        ge=0.0, le=360.0, description="direction the cloud field moves TOWARDS"
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="correlation peak quality; low means the field changed shape"
    )
    method: str
    provenance: Provenance

    @property
    def interval_s(self) -> float:
        return (self.to_time_utc - self.from_time_utc).total_seconds()


class IrradianceNowcast(BaseModel):
    """Short-term irradiance outlook for one point, from satellite imagery.

    `kind` is never OBSERVATION. A satellite retrieval is a model applied to
    radiances; the only thing measured is what the instrument saw from orbit.
    """

    model_config = ConfigDict(frozen=True)

    point: GeoPoint
    issued_at_utc: datetime
    valid_times_utc: list[datetime]
    ghi_w_m2: list[float]
    clear_sky_ghi_w_m2: list[float] = Field(
        description="the physical upper bound at the same times, for comparison"
    )
    clear_sky_index: list[float] = Field(
        description="ghi / clear-sky ghi, the quantity the satellite actually constrains"
    )
    horizon_s: float
    motion: CloudMotion | None
    method: str
    kind: DataKind = DataKind.DERIVED
    validation_status: str = Field(
        default=(
            "NOT VALIDATED: no ground-truth irradiance measurement exists for this "
            "platform, so the error of this nowcast is unknown"
        ),
        description="stated on the object so a consumer cannot omit it",
    )
    limitations: list[str] = Field(default_factory=list)
    provenance: Provenance

    @model_validator(mode="after")
    def _never_an_observation(self) -> Self:
        if self.kind is DataKind.OBSERVATION:
            raise ValueError(
                "a satellite irradiance retrieval is not an observation: nothing on the "
                "ground measured it"
            )
        if not (len(self.valid_times_utc) == len(self.ghi_w_m2) == len(self.clear_sky_index)):
            raise ValueError("valid_times_utc, ghi_w_m2 and clear_sky_index must align")
        return self


# ---------------------------------------------------------------------------
# Vehicle telemetry (phase 3)
#
# The team already produces live telemetry over CAN -> sub-GHz radio -> pit
# laptop -> MongoDB (drive-system/.../telemetry/). This platform does not
# redefine any of that. These types are the ingest contract: whatever reader
# we write - MongoDB adapter, logfile replay, future UDP listener - produces
# values of these shapes, and the rest of the platform does not care which.
#
# Rules the types enforce:
#   * every sample carries a timestamp from the vehicle, not from "now";
#   * state of charge is reported as the ESTIMATE a BMS gave, not re-derived
#     from a model, so a model vs. measurement comparison is possible;
#   * array power from the MPPTs is a MEASUREMENT (DataKind.OBSERVATION),
#     unlike anything the weather and solar modules can give us;
#   * speed, position and heading come from the vehicle's own sensors, which
#     the StateStore prefers over operator-entered manual fixes.
# ---------------------------------------------------------------------------


class TelemetrySource(StrEnum):
    LIVE_RADIO = "live_radio"
    """Sub-GHz radio from the car, arriving within seconds."""
    LIVE_CELLULAR = "live_cellular"
    """Cellular backup link (where coverage exists)."""
    LIVE_ONBOARD_SENSOR = "live_onboard_sensor"
    """Read live from a sensor on the car or an escort (ADR 0010); freshness-gated
    like the radio links."""
    MONGO_REPLAY = "mongo_replay"
    """Replayed from the team's MongoDB collection; the receive time stays the
    vehicle time, not the replay time."""
    FILE_REPLAY = "file_replay"
    """Replayed from a logfile (ndjson, csv, mcap). Same rule: times are the
    vehicle's own."""
    SIMULATED = "simulated"
    """Produced by rwi.simulation; a consumer that must know whether it is
    looking at real driving can tell from this field."""


class BatteryTelemetry(BaseModel):
    """One sample of the pack as the BMS reports it."""

    model_config = ConfigDict(frozen=True)

    time_utc: datetime
    state_of_charge_fraction: float | None = Field(default=None, ge=0.0, le=1.0,
        description="BMS estimate, 0..1. None when the BMS has not published one yet.")
    pack_voltage_v: float | None = Field(default=None, ge=0.0, le=200.0)
    pack_current_a: float | None = Field(
        default=None, description="positive = discharging, negative = charging"
    )
    pack_power_w: float | None = None
    pack_energy_wh: float | None = Field(default=None, ge=0.0,
        description="energy in the pack right now, when the BMS reports it")
    min_cell_voltage_v: float | None = None
    max_cell_voltage_v: float | None = None
    min_cell_temperature_c: float | None = None
    max_cell_temperature_c: float | None = None
    provenance: Provenance


class ArrayTelemetry(BaseModel):
    """What the MPPTs actually delivered. This is the measurement the entire
    PV model has been waiting to be validated against."""

    model_config = ConfigDict(frozen=True)

    time_utc: datetime
    total_power_w: float | None = Field(default=None, ge=0.0,
        description="sum across all MPPTs, if reported")
    per_group_power_w: dict[str, float] = Field(default_factory=dict,
        description="power per MPPT group; keys match ArrayConfig cell groups")
    bus_voltage_v: float | None = None
    cell_temperature_c: float | None = Field(default=None,
        description="measured on-array temperature sensor, if present")
    provenance: Provenance


class MotorTelemetry(BaseModel):
    """Drivetrain power draw and vehicle speed as the motor controller sees it."""

    model_config = ConfigDict(frozen=True)

    time_utc: datetime
    speed_mps: float | None = Field(default=None, ge=0.0,
        description="vehicle ground speed from the motor controller / wheel sensor")
    bus_power_w: float | None = Field(default=None,
        description="power draw from the bus; negative = regen if the car has it")
    motor_current_a: float | None = None
    motor_rpm: float | None = None
    provenance: Provenance


class VehicleTelemetrySample(BaseModel):
    """A unified vehicle sample built from one or more subsystem frames.

    Fields are all optional because the CAN frames arrive asynchronously and
    one radio burst usually does not carry everything at once. The reader is
    responsible for stitching contemporaneous frames into one sample; the
    stitching window is recorded in ``aggregation_window_s``.
    """

    model_config = ConfigDict(frozen=True)

    time_utc: datetime = Field(description="the vehicle's own clock, not the receiver's")
    source: TelemetrySource
    position: PositionFix | None = None
    battery: BatteryTelemetry | None = None
    array: ArrayTelemetry | None = None
    motor: MotorTelemetry | None = None
    aggregation_window_s: float | None = Field(default=None, ge=0.0,
        description="width of the window used to stitch sub-frames into this sample")
    provenance: Provenance


class TelemetryGap(BaseModel):
    """A period during which no telemetry was received.

    Gaps are information. A silent reader that just "catches up" when data
    arrives again hides from the strategist that the car was out of radio
    coverage for ten minutes.
    """

    model_config = ConfigDict(frozen=True)

    start_utc: datetime
    end_utc: datetime
    expected_cadence_s: float = Field(gt=0.0,
        description="what cadence the source normally delivers")
    missing_samples_estimate: int = Field(ge=0)
    reason: str | None = Field(default=None,
        description="e.g. 'radio dropout', 'MongoDB connection lost', 'end of file'")

    @property
    def duration_s(self) -> float:
        return (self.end_utc - self.start_utc).total_seconds()


@runtime_checkable
class TelemetryReader(Protocol):
    """A source of VehicleTelemetrySample objects.

    Implementations: rwi.telemetry.mongo.MongoReader (team's live MongoDB),
    rwi.telemetry.replay.FileReader (ndjson / csv logfile), and later a UDP
    or websocket live reader.

    The reader does NOT advance the active position by itself. It yields
    samples; a coordinator decides whether to feed them to StateStore.
    """

    source: TelemetrySource

    def stream(self) -> AsyncIterator[VehicleTelemetrySample]: ...

    async def gaps_so_far(self) -> list[TelemetryGap]:
        """Gaps the reader has detected up to now."""
        ...

    async def close(self) -> None: ...


__all__ = [
    "Alert",
    "AlertCategory",
    "AlertSeverity",
    "ArchiveRecordKind",
    "ArrayConfig",
    "ArrayTelemetry",
    "BatteryTelemetry",
    "CloudMotion",
    "ConsumptionEstimate",
    "EnergyEstimate",
    "EnsembleForecast",
    "EnsembleSummary",
    "ForecastArchive",
    "ForecastObservationPair",
    "GeoGrid",
    "IrradianceComponents",
    "IrradianceNowcast",
    "ModelComparison",
    "ModelDisagreement",
    "MotorTelemetry",
    "POAIrradiance",
    "ParameterSource",
    "PowerEstimate",
    "ProviderCapabilities",
    "ProviderHealth",
    "Recommendation",
    "Route",
    "RouteProjection",
    "RouteWaypoint",
    "RouteWeatherForecast",
    "RouteWeatherSample",
    "SatelliteScene",
    "SatelliteTile",
    "ScenarioConstraintViolation",
    "ScenarioResult",
    "SolarDayEvents",
    "SolarPosition",
    "TelemetryGap",
    "TelemetryReader",
    "TelemetrySource",
    "Trajectory",
    "TrajectoryPoint",
    "VehicleConfig",
    "VehicleTelemetrySample",
    "VerificationReport",
    "VerificationScore",
    "WaypointKind",
    "WeatherProvider",
    "WeatherSeries",
    "WeatherStep",
]
