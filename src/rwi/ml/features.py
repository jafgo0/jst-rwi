"""Feature schema for the ML post-processor (ADR 0008 §2).

One row describes one upstream forecast value at one place and valid time. The
schema is fixed in code; its hash is written to every model manifest and the
loader refuses a model trained on a different schema. Adding or reordering a
feature is therefore a deliberate, versioned change.

Solar geometry and clear sky come from pvlib via `rwi.solar` (hard rule 6). The
hour angle uses pvlib's equation of time, so local time never enters (rule 3).
"""

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from pvlib import solarposition

from rwi.core.geo import GeoPoint
from rwi.core.timebase import ensure_utc
from rwi.solar.clearsky import clear_sky_series

__all__ = [
    "FEATURE_NAMES",
    "FEATURE_SCHEMA_VERSION",
    "SOURCE_FEATURE",
    "SolarContext",
    "build_features",
    "feature_schema_hash",
    "solar_context",
    "source_id",
]

FloatArray = NDArray[np.float64]

FEATURE_SCHEMA_VERSION = 1
#: Column order of the feature matrix. ``source_code`` is categorical (an index
#: into the model's source vocabulary); ``route_distance_km`` is NaN when no
#: route is known, which LightGBM handles natively.
FEATURE_NAMES: tuple[str, ...] = (
    "source_code",
    "forecast_value",
    "clear_sky_ghi_w_m2",
    "clear_sky_dni_w_m2",
    "solar_elevation_deg",
    "lead_time_h",
    "hour_angle_sin",
    "hour_angle_cos",
    "latitude_deg",
    "longitude_deg",
    "route_distance_km",
)
SOURCE_FEATURE = FEATURE_NAMES.index("source_code")


def feature_schema_hash() -> str:
    """SHA-256 of the feature schema (names, order, version)."""
    payload = json.dumps(
        {"version": FEATURE_SCHEMA_VERSION, "features": list(FEATURE_NAMES)}, sort_keys=True
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def source_id(provider: str, model_name: str | None) -> str:
    """Stable identifier of an upstream forecast source, e.g. ``open-meteo/best_match``."""
    return f"{provider}/{model_name or '-'}"


class SolarContext:
    """pvlib clear sky and geometry for a batch of (point, time) rows."""

    def __init__(
        self, ghi: FloatArray, dni: FloatArray, elevation: FloatArray, hour_angle: FloatArray
    ) -> None:
        self.clear_sky_ghi = ghi
        self.clear_sky_dni = dni
        self.elevation_deg = elevation
        self.hour_angle_deg = hour_angle

    def clear_sky_for(self, variable: str) -> FloatArray:
        """Clear-sky W/m2 of ``variable`` (``ghi`` or ``dni``) per row."""
        if variable == "ghi":
            return self.clear_sky_ghi
        if variable == "dni":
            return self.clear_sky_dni
        raise ValueError(f"no clear-sky reference for {variable!r}")

    def take(self, rows: Sequence[int]) -> "SolarContext":
        """The context restricted to ``rows``, in that order."""
        idx = np.asarray(rows, dtype=int)
        return SolarContext(
            self.clear_sky_ghi[idx],
            self.clear_sky_dni[idx],
            self.elevation_deg[idx],
            self.hour_angle_deg[idx],
        )


def _key(p: GeoPoint) -> tuple[float, float, float | None]:
    return (p.latitude_deg, p.longitude_deg, p.elevation_m)


def solar_context(points: Sequence[GeoPoint], times: Sequence[datetime]) -> SolarContext:
    """Clear-sky GHI/DNI, apparent elevation and hour angle per row (pvlib, UTC)."""
    n = len(points)
    if len(times) != n:
        raise ValueError("points and times must have the same length")
    ghi = np.zeros(n)
    dni = np.zeros(n)
    elev = np.zeros(n)
    ha = np.zeros(n)
    groups: dict[tuple[float, float, float | None], list[int]] = defaultdict(list)
    for i, p in enumerate(points):
        groups[_key(p)].append(i)
    for idx in groups.values():
        point = points[idx[0]]
        t = [ensure_utc(times[i]) for i in idx]
        comps = clear_sky_series(point, t)
        index = pd.DatetimeIndex(t)
        eot = solarposition.equation_of_time_spencer71(index.dayofyear)
        hour_angle = np.asarray(
            solarposition.hour_angle(index, point.longitude_deg, eot), dtype=float
        )
        for j, i in enumerate(idx):
            ghi[i] = comps[j].ghi_w_m2
            dni[i] = comps[j].dni_w_m2
            elev[i] = comps[j].solar_elevation_deg
            ha[i] = hour_angle[j]
    return SolarContext(ghi, dni, elev, ha)


def build_features(
    *,
    source_codes: Sequence[int],
    forecast_values: Sequence[float],
    points: Sequence[GeoPoint],
    lead_times_s: Sequence[float],
    solar: SolarContext,
    route_distance_km: Sequence[float | None] | None = None,
) -> FloatArray:
    """Feature matrix ``(n, len(FEATURE_NAMES))`` in `FEATURE_NAMES` order."""
    n = len(points)
    route = (
        [math.nan] * n
        if route_distance_km is None
        else [math.nan if d is None else float(d) for d in route_distance_km]
    )
    ha = np.radians(solar.hour_angle_deg)
    cols = [
        np.asarray(source_codes, dtype=float),
        np.asarray(forecast_values, dtype=float),
        solar.clear_sky_ghi,
        solar.clear_sky_dni,
        solar.elevation_deg,
        np.asarray(lead_times_s, dtype=float) / 3600.0,
        np.sin(ha),
        np.cos(ha),
        np.asarray([p.latitude_deg for p in points], dtype=float),
        np.asarray([p.longitude_deg for p in points], dtype=float),
        np.asarray(route, dtype=float),
    ]
    return np.column_stack(cols) if n else np.zeros((0, len(FEATURE_NAMES)))
