"""Shared scientific primitives. Import from here, not from submodule paths,
when you only need the common types."""

from rwi.core.errors import (
    CapabilityNotSupportedError,
    InvalidCoordinateError,
    ModelConfigurationError,
    NaiveDatetimeError,
    PositionUnavailableError,
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    RouteError,
    RwiError,
    StaleDataError,
    UnknownVariableError,
)
from rwi.core.geo import GeoPoint, geodesic_distance_m, initial_bearing_deg
from rwi.core.position import ActiveState, FixQuality, PositionFix, PositionMode
from rwi.core.provenance import (
    DataKind,
    Measured,
    Provenance,
    QualityFlag,
    Uncertainty,
    measured,
)
from rwi.core.state import StateStore, get_state_store
from rwi.core.timebase import TimeWindow, ensure_utc, to_local, utc_now
from rwi.core.variables import CANONICAL_VARIABLES, VariableSpec, spec_for, unit_for
from rwi.core.wind import RelativeWind, relative_wind

__all__ = [
    "CANONICAL_VARIABLES",
    "ActiveState",
    "CapabilityNotSupportedError",
    "DataKind",
    "FixQuality",
    "GeoPoint",
    "InvalidCoordinateError",
    "Measured",
    "ModelConfigurationError",
    "NaiveDatetimeError",
    "PositionFix",
    "PositionMode",
    "PositionUnavailableError",
    "Provenance",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderUnavailableError",
    "QualityFlag",
    "RelativeWind",
    "RouteError",
    "RwiError",
    "StaleDataError",
    "StateStore",
    "TimeWindow",
    "Uncertainty",
    "UnknownVariableError",
    "VariableSpec",
    "ensure_utc",
    "geodesic_distance_m",
    "get_state_store",
    "initial_bearing_deg",
    "measured",
    "relative_wind",
    "spec_for",
    "to_local",
    "unit_for",
    "utc_now",
]
