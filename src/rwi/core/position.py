"""Positioning model: four explicit modes, one authoritative state.

The platform is GPS-first. There is no default location anywhere in the code: if
no fix is available, :class:`~rwi.core.errors.PositionUnavailableError` is
raised and the UI says so. Every fix is labelled with its mode so that a
simulated position can never be mistaken for a live one.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rwi.core.geo import GeoPoint
from rwi.core.timebase import ensure_utc, utc_now


class PositionMode(StrEnum):
    LIVE_GPS = "live_gps"
    """Real fix from an authorised device or vehicle telemetry."""
    MANUAL = "manual"
    """Operator-entered coordinate, for planning and testing."""
    ROUTE_SIM = "route_sim"
    """Simulated vehicle advancing along the competition route."""
    HISTORICAL_REPLAY = "historical_replay"
    """Recorded track being replayed."""


SIMULATED_MODES = frozenset({PositionMode.ROUTE_SIM, PositionMode.HISTORICAL_REPLAY})


class FixQuality(StrEnum):
    NO_FIX = "no_fix"
    GPS = "gps"
    DGPS = "dgps"
    RTK = "rtk"
    MANUAL = "manual"
    SIMULATED = "simulated"
    UNKNOWN = "unknown"


class PositionFix(BaseModel):
    """A single position report.

    ``gps_time_utc`` is the instant the *receiver* stamped the fix;
    ``received_at_utc`` is when this platform got it. Keeping them apart is what
    makes stale-telemetry detection possible.
    """

    model_config = ConfigDict(frozen=True)

    point: GeoPoint
    mode: PositionMode
    source: str = Field(description="e.g. 'vehicle_telemetry', 'operator', 'route_sim'")
    received_at_utc: datetime
    gps_time_utc: datetime | None = None
    accuracy_m: float | None = Field(
        default=None, ge=0.0, description="horizontal 1-sigma, or derived from HDOP"
    )
    speed_mps: float | None = Field(default=None, ge=0.0, description="ground speed")
    course_deg: float | None = Field(
        default=None, ge=0.0, lt=360.0, description="course over ground"
    )
    fix_quality: FixQuality = FixQuality.UNKNOWN
    satellites: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _normalise(self) -> Self:
        object.__setattr__(self, "received_at_utc", ensure_utc(self.received_at_utc))
        if self.gps_time_utc is not None:
            object.__setattr__(self, "gps_time_utc", ensure_utc(self.gps_time_utc))
        return self

    @property
    def is_simulated(self) -> bool:
        return self.mode in SIMULATED_MODES or self.fix_quality is FixQuality.SIMULATED

    @property
    def clock_skew_s(self) -> float | None:
        """GPS timestamp minus receipt time. Large magnitudes mean a clock problem."""
        if self.gps_time_utc is None:
            return None
        return (self.gps_time_utc - self.received_at_utc).total_seconds()

    def age_s(self, now: datetime | None = None) -> float:
        reference = self.gps_time_utc or self.received_at_utc
        return ((ensure_utc(now) if now else utc_now()) - reference).total_seconds()

    def is_stale(self, max_age_s: float, now: datetime | None = None) -> bool:
        return self.age_s(now) > max_age_s


class ActiveState(BaseModel):
    """The single source of truth for *where* and *when* the platform is looking.

    Everything downstream - weather requests, solar geometry, route projection,
    energy model, AI explanations - reads this and nothing else.
    """

    model_config = ConfigDict(frozen=True)

    mode: PositionMode
    fix: PositionFix | None = None
    #: A suspect fix held aside, not promoted to the active ``fix``. The
    #: HTTP position endpoint parks an implausible-jump live_gps fix here
    #: instead of letting it clobber the last trusted position; weather,
    #: route and strategy read ``fix``, so they keep using the last trusted
    #: location until the operator confirms or the next trusted fix arrives.
    #: UI shows the discrepancy (lat/lon, implied speed, age) via
    #: :mod:`rwi.alerts`.
    suspect_fix: PositionFix | None = None
    selected_time_utc: datetime | None = Field(
        default=None, description="None means 'follow real time'"
    )
    route_id: str | None = None
    route_distance_m: float | None = Field(
        default=None, ge=0.0, description="progress along the active route, when known"
    )
    heading_deg: float | None = Field(
        default=None, ge=0.0, lt=360.0, description="travel direction used by wind/POA models"
    )
    updated_at_utc: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _normalise(self) -> Self:
        if self.selected_time_utc is not None:
            object.__setattr__(self, "selected_time_utc", ensure_utc(self.selected_time_utc))
        object.__setattr__(self, "updated_at_utc", ensure_utc(self.updated_at_utc))
        if self.fix is not None and self.fix.mode is not self.mode:
            raise ValueError(
                f"fix mode {self.fix.mode} does not match active mode {self.mode}"
            )
        return self

    @property
    def is_time_frozen(self) -> bool:
        return self.selected_time_utc is not None

    def effective_time(self, now: datetime | None = None) -> datetime:
        """The instant all calculations must use."""
        if self.selected_time_utc is not None:
            return self.selected_time_utc
        return ensure_utc(now) if now else utc_now()

    @property
    def effective_heading_deg(self) -> float | None:
        """Heading from explicit state, else course over ground from the fix."""
        if self.heading_deg is not None:
            return self.heading_deg
        return self.fix.course_deg if self.fix else None
