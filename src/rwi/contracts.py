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
from collections.abc import Iterable, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Measured, Provenance, QualityFlag
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
        if self.solar_elevation_deg is not None and self.solar_elevation_deg <= 0.0:
            if max(self.ghi_w_m2, self.dni_w_m2, self.dhi_w_m2) > 0.0:
                raise ValueError(
                    "sun below horizon (elevation "
                    f"{self.solar_elevation_deg:.3f} deg) but irradiance is positive: "
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
        if any(b < a for a, b in zip(times, times[1:], strict=False)):
            raise ValueError("WeatherSeries steps must be sorted by valid_time_utc")
        return self

    @property
    def times(self) -> list[datetime]:
        return [s.valid_time_utc for s in self.steps]

    @property
    def variables(self) -> frozenset[str]:
        return frozenset().union(*(frozenset(s.values) for s in self.steps)) if self.steps else frozenset()

    @property
    def window(self) -> TimeWindow | None:
        if not self.steps:
            return None
        return TimeWindow(start_utc=self.steps[0].valid_time_utc, end_utc=self.steps[-1].valid_time_utc)

    def series(self, variable: str) -> list[tuple[datetime, float]]:
        """(time, value) pairs for one variable, skipping unusable entries."""
        out: list[tuple[datetime, float]] = []
        for step in self.steps:
            v = step.value(variable)
            if v is not None:
                out.append((step.valid_time_utc, v))
        return out

    def nearest_step(self, when: datetime, *, tolerance_s: float | None = None) -> WeatherStep | None:
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
        return Provenance(
            provider=self.provider,
            dataset=self.dataset,
            kind=self.kind,
            retrieved_at_utc=self.retrieved_at_utc,
            valid_time_utc=self.steps[0].valid_time_utc if self.kind is DataKind.FORECAST and self.steps else None,
            observation_time_utc=self.steps[-1].valid_time_utc if self.kind is DataKind.OBSERVATION and self.steps else None,
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

    def member_values(self, variable: str, when: datetime, *, tolerance_s: float = 1800.0) -> list[float]:
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
        if any(b < a for a, b in zip(d, d[1:], strict=False)):
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
        le=0.0, description="relative dP/dT, 1/degC, negative for silicon and GaAs"
    )
    mppt_efficiency: float = Field(gt=0.0, le=1.0)
    wiring_efficiency: float = Field(gt=0.0, le=1.0)
    mounting: Literal["horizontal", "fixed_tilt", "vehicle_relative"] = "horizontal"
    surface_tilt_deg: float = Field(default=0.0, ge=0.0, le=90.0)
    surface_azimuth_deg: float | None = Field(
        default=None, description="None with vehicle_relative mounting: derived from heading"
    )
    noct_c: float = Field(default=48.0, description="nominal operating cell temperature")
    albedo: float = Field(default=0.2, ge=0.0, le=1.0)
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


__all__ = [
    "Alert",
    "AlertCategory",
    "AlertSeverity",
    "ArrayConfig",
    "ConsumptionEstimate",
    "EnergyEstimate",
    "EnsembleForecast",
    "IrradianceComponents",
    "POAIrradiance",
    "ParameterSource",
    "PowerEstimate",
    "ProviderCapabilities",
    "ProviderHealth",
    "Recommendation",
    "Route",
    "RouteProjection",
    "RouteWeatherForecast",
    "RouteWeatherSample",
    "RouteWaypoint",
    "ScenarioConstraintViolation",
    "ScenarioResult",
    "SolarDayEvents",
    "SolarPosition",
    "Trajectory",
    "TrajectoryPoint",
    "VehicleConfig",
    "WaypointKind",
    "WeatherProvider",
    "WeatherSeries",
    "WeatherStep",
]
