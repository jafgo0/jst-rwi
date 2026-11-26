"""Provenance and data-kind metadata.

Non-negotiable platform rule: a number without provenance cannot be displayed.
Each value carries where it came from, what kind of statement it is (measured,
forecast, or model output), which instant it is valid for, and how fresh it is.

In particular :class:`DataKind` keeps three things apart that look alike on a
dashboard but mean completely different things:
  * ``OBSERVATION``      - somebody measured this;
  * ``FORECAST``         - a model predicts this for a future valid time;
  * ``CLEAR_SKY_MODEL``  - a physical upper bound computed from geometry, which
                           is *not* a measurement and *not* a forecast.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rwi.core.geo import GeoPoint
from rwi.core.timebase import ensure_utc, utc_now
from rwi.core.variables import spec_for


class DataKind(StrEnum):
    OBSERVATION = "observation"
    """Measured by an instrument."""
    ANALYSIS = "analysis"
    """Model state assimilated to observations, valid at or before now."""
    FORECAST = "forecast"
    """Model prediction for a future valid time."""
    CLEAR_SKY_MODEL = "clear_sky_model"
    """Physical clear-sky upper bound from solar geometry; never a measurement."""
    PHYSICAL_MODEL = "physical_model"
    """Deterministic physical model output (e.g. PV power from irradiance)."""
    SIMULATION = "simulation"
    """Synthetic value produced by the platform for testing or route simulation."""
    DERIVED = "derived"
    """Computed from other values in this platform."""
    CLIMATOLOGY = "climatology"
    """Long-term statistic, not a forecast for a specific day."""


OBSERVED_KINDS = frozenset({DataKind.OBSERVATION})
PREDICTED_KINDS = frozenset({DataKind.FORECAST})
MODELLED_KINDS = frozenset(
    {DataKind.CLEAR_SKY_MODEL, DataKind.PHYSICAL_MODEL, DataKind.DERIVED, DataKind.ANALYSIS}
)


class QualityFlag(StrEnum):
    OK = "ok"
    STALE = "stale"
    MISSING = "missing"
    OUT_OF_RANGE = "out_of_range"
    RATE_IMPLAUSIBLE = "rate_implausible"
    SENSOR_SUSPECT = "sensor_suspect"
    UNVALIDATED = "unvalidated"
    INTERPOLATED = "interpolated"


USABLE_FLAGS = frozenset({QualityFlag.OK, QualityFlag.INTERPOLATED, QualityFlag.UNVALIDATED})


class Uncertainty(BaseModel):
    """Uncertainty attached to a value. All fields optional: an honest ``None``
    beats an invented error bar."""

    model_config = ConfigDict(frozen=True)

    standard_deviation: float | None = None
    lower: float | None = None
    upper: float | None = None
    interval_probability: float | None = Field(default=None, ge=0.0, le=1.0)
    method: str | None = Field(
        default=None, description="how it was obtained, e.g. 'ensemble_spread', 'verified_mae'"
    )

    @model_validator(mode="after")
    def _check_interval(self) -> Self:
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError("lower must not exceed upper")
        return self


class Provenance(BaseModel):
    """Full lineage of a single value or series."""

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    provider: str = Field(description="organisation or system, e.g. 'open-meteo', 'bom', 'rwi'")
    dataset: str = Field(description="dataset or endpoint, e.g. 'forecast', 'ensemble', 'pvlib'")
    kind: DataKind
    retrieved_at_utc: datetime = Field(description="when this platform obtained the value")
    valid_time_utc: datetime | None = Field(
        default=None, description="instant the value describes (forecasts and analyses)"
    )
    observation_time_utc: datetime | None = Field(
        default=None, description="instant of measurement (observations only)"
    )
    model_name: str | None = None
    model_version: str | None = None
    model_init_time_utc: datetime | None = Field(
        default=None, description="NWP run initialisation ('cycle') time"
    )
    training_cutoff_utc: datetime | None = Field(
        default=None,
        description="learned models only: latest forecast issue time in the training set",
    )
    point: GeoPoint | None = Field(
        default=None, description="location the value applies to, after any grid selection"
    )
    requested_point: GeoPoint | None = Field(
        default=None, description="location that was asked for, if different from `point`"
    )
    spatial_resolution_km: float | None = None
    temporal_resolution_s: float | None = None
    processing: str | None = Field(
        default=None, description="e.g. 'nearest_grid_point', 'linear_time_interp'"
    )
    license: str | None = None
    source_url: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _normalise_times(self) -> Self:
        for field in ("retrieved_at_utc", "valid_time_utc", "observation_time_utc",
                      "model_init_time_utc", "training_cutoff_utc"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, ensure_utc(value))
        if self.kind is DataKind.OBSERVATION and self.observation_time_utc is None:
            raise ValueError("observations must carry observation_time_utc")
        if self.kind is DataKind.FORECAST and self.valid_time_utc is None:
            raise ValueError("forecasts must carry valid_time_utc")
        return self

    @property
    def reference_time_utc(self) -> datetime:
        """The instant that matters for age: measurement time, else valid time, else retrieval."""
        return self.observation_time_utc or self.valid_time_utc or self.retrieved_at_utc

    @property
    def lead_time_s(self) -> float | None:
        """Forecast lead time (valid time minus model init), seconds."""
        if self.valid_time_utc is None or self.model_init_time_utc is None:
            return None
        return (self.valid_time_utc - self.model_init_time_utc).total_seconds()

    def age_s(self, now: datetime | None = None) -> float:
        """Seconds since this value was retrieved."""
        return ((ensure_utc(now) if now else utc_now()) - self.retrieved_at_utc).total_seconds()

    @property
    def is_measurement(self) -> bool:
        return self.kind in OBSERVED_KINDS

    @property
    def is_prediction(self) -> bool:
        return self.kind in PREDICTED_KINDS

    def label(self) -> str:
        """Short human-readable attribution for the UI."""
        parts = [self.provider, self.dataset]
        if self.model_name:
            parts.append(self.model_name)
        if self.model_version:
            parts.append(f"v{self.model_version}")
        return " / ".join(parts)


class Measured(BaseModel):
    """One scalar value with units, provenance and quality.

    ``value is None`` means the source had no number for this variable; it is
    never silently replaced by zero.
    """

    model_config = ConfigDict(frozen=True)

    variable: str
    value: float | None
    unit: str
    provenance: Provenance
    quality: QualityFlag = QualityFlag.OK
    uncertainty: Uncertainty | None = None

    @model_validator(mode="after")
    def _check_unit(self) -> Self:
        expected = spec_for(self.variable).unit
        if self.unit != expected:
            raise ValueError(
                f"{self.variable} must be in {expected!r}, got {self.unit!r}; "
                "convert at the provider boundary"
            )
        if self.value is None and self.quality is QualityFlag.OK:
            object.__setattr__(self, "quality", QualityFlag.MISSING)
        return self

    @property
    def is_usable(self) -> bool:
        return self.value is not None and self.quality in USABLE_FLAGS

    def is_fresh(self, max_age_s: float, now: datetime | None = None) -> bool:
        reference = self.provenance.reference_time_utc
        age = ((ensure_utc(now) if now else utc_now()) - reference).total_seconds()
        return abs(age) <= max_age_s


def measured(
    variable: str,
    value: float | None,
    provenance: Provenance,
    *,
    quality: QualityFlag = QualityFlag.OK,
    uncertainty: Uncertainty | None = None,
) -> Measured:
    """Construct a :class:`Measured`, taking the unit from the variable registry."""
    return Measured(
        variable=variable,
        value=value,
        unit=spec_for(variable).unit,
        provenance=provenance,
        quality=quality,
        uncertainty=uncertainty,
    )
