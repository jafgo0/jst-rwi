"""Geodesy primitives on the WGS84 ellipsoid.

All distances are metres, all bearings are degrees clockwise from true north.
Ellipsoidal calculations use pyproj (Karney's geodesic algorithms); the
spherical haversine implementation is kept only as an independent cross-check
in tests, never as the production path.
"""

from __future__ import annotations

import math
from functools import cached_property
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core.core_schema import ValidationInfo
from pyproj import Geod

from rwi.core.errors import InvalidCoordinateError

_GEOD = Geod(ellps="WGS84")

EARTH_MEAN_RADIUS_M = 6_371_008.8
"""IUGG mean radius, used only by the haversine cross-check."""

Latitude = Annotated[float, Field(ge=-90.0, le=90.0, description="degrees north, WGS84")]
Longitude = Annotated[float, Field(ge=-180.0, le=180.0, description="degrees east, WGS84")]


class GeoPoint(BaseModel):
    """A point on Earth. Elevation is metres above the WGS84 ellipsoid-referenced
    mean sea level as supplied by the source; ``None`` means unknown, which is
    different from 0 m."""

    model_config = ConfigDict(frozen=True)

    latitude_deg: Latitude
    longitude_deg: Longitude
    elevation_m: float | None = Field(
        default=None,
        ge=-500.0,
        le=9000.0,
        description="metres above mean sea level; None = unknown",
    )

    @field_validator("latitude_deg", "longitude_deg", "elevation_m", mode="before")
    @classmethod
    def _reject_non_finite(cls, value: object, info: ValidationInfo) -> object:
        """Raise our own typed error for NaN/inf before pydantic's range check.

        Pydantic's ``ge``/``le`` constraints reject NaN too, but as a generic
        validation error. The API layer maps :class:`InvalidCoordinateError` to a
        specific client response, so the typed error must win.
        """
        if isinstance(value, float) and not math.isfinite(value):
            raise InvalidCoordinateError(f"{info.field_name} is not finite: {value!r}")
        return value

    @cached_property
    def elevation_or_zero_m(self) -> float:
        """Elevation with an explicit 0 m substitution.

        Only for physical models that require a number. Callers must not present
        the result as a measured elevation.
        """
        return 0.0 if self.elevation_m is None else self.elevation_m

    def __str__(self) -> str:
        elev = "?" if self.elevation_m is None else f"{self.elevation_m:.0f}"
        return f"({self.latitude_deg:.5f}, {self.longitude_deg:.5f}, {elev} m)"


def geodesic_distance_m(a: GeoPoint, b: GeoPoint) -> float:
    """Shortest ellipsoidal (geodesic) surface distance between two points."""
    _, _, dist = _GEOD.inv(a.longitude_deg, a.latitude_deg, b.longitude_deg, b.latitude_deg)
    return float(abs(dist))


def initial_bearing_deg(a: GeoPoint, b: GeoPoint) -> float:
    """Forward azimuth at ``a`` of the geodesic towards ``b``, in [0, 360)."""
    az, _, _ = _GEOD.inv(a.longitude_deg, a.latitude_deg, b.longitude_deg, b.latitude_deg)
    return float(az % 360.0)


def destination_point(origin: GeoPoint, bearing_deg: float, distance_m: float) -> GeoPoint:
    """Point reached by travelling ``distance_m`` from ``origin`` along ``bearing_deg``."""
    lon, lat, _ = _GEOD.fwd(
        origin.longitude_deg, origin.latitude_deg, bearing_deg % 360.0, distance_m
    )
    return GeoPoint(latitude_deg=lat, longitude_deg=lon, elevation_m=origin.elevation_m)


def interpolate_point(a: GeoPoint, b: GeoPoint, fraction: float) -> GeoPoint:
    """Point a given fraction (0..1) of the geodesic from ``a`` to ``b``.

    Elevation is linearly interpolated when both endpoints have one.
    """
    fraction = min(max(fraction, 0.0), 1.0)
    az, _, dist = _GEOD.inv(a.longitude_deg, a.latitude_deg, b.longitude_deg, b.latitude_deg)
    lon, lat, _ = _GEOD.fwd(a.longitude_deg, a.latitude_deg, az, dist * fraction)
    elev: float | None = None
    if a.elevation_m is not None and b.elevation_m is not None:
        elev = a.elevation_m + (b.elevation_m - a.elevation_m) * fraction
    return GeoPoint(latitude_deg=lat, longitude_deg=lon, elevation_m=elev)


def haversine_m(a: GeoPoint, b: GeoPoint) -> float:
    """Great-circle distance on a sphere. Independent cross-check for tests only.

    Differs from :func:`geodesic_distance_m` by up to ~0.5 % because Earth is not
    a sphere.
    """
    phi1, phi2 = math.radians(a.latitude_deg), math.radians(b.latitude_deg)
    dphi = phi2 - phi1
    dlam = math.radians(b.longitude_deg - a.longitude_deg)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_MEAN_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))


def cross_track_distance_m(point: GeoPoint, seg_start: GeoPoint, seg_end: GeoPoint) -> float:
    """Signed perpendicular distance from ``point`` to the great circle through the
    segment. Positive means right of the start->end direction.

    Spherical approximation; adequate for map matching at the few-metre level
    over segments of a few kilometres.
    """
    d13 = haversine_m(seg_start, point) / EARTH_MEAN_RADIUS_M
    theta13 = math.radians(initial_bearing_deg(seg_start, point))
    theta12 = math.radians(initial_bearing_deg(seg_start, seg_end))
    return math.asin(math.sin(d13) * math.sin(theta13 - theta12)) * EARTH_MEAN_RADIUS_M


def along_track_distance_m(point: GeoPoint, seg_start: GeoPoint, seg_end: GeoPoint) -> float:
    """Distance from ``seg_start`` to the projection of ``point`` onto the segment.

    May be negative (behind the segment start) or exceed the segment length.
    """
    d13 = haversine_m(seg_start, point) / EARTH_MEAN_RADIUS_M
    xt = cross_track_distance_m(point, seg_start, seg_end) / EARTH_MEAN_RADIUS_M
    cos_d13 = math.cos(d13)
    cos_xt = math.cos(xt)
    if cos_xt == 0.0:
        return 0.0
    ratio = min(1.0, max(-1.0, cos_d13 / cos_xt))
    along = math.acos(ratio) * EARTH_MEAN_RADIUS_M
    # acos loses the sign; recover it from the bearing difference.
    bearing_delta = math.radians(initial_bearing_deg(seg_start, seg_end)) - math.radians(
        initial_bearing_deg(seg_start, point)
    )
    return -along if math.cos(bearing_delta) < 0 else along


def bounding_box(points: list[GeoPoint]) -> tuple[float, float, float, float]:
    """(min_lat, min_lon, max_lat, max_lon). Does not handle antimeridian crossing;
    the BWSC route does not cross it."""
    if not points:
        raise ValueError("bounding_box requires at least one point")
    lats = [p.latitude_deg for p in points]
    lons = [p.longitude_deg for p in points]
    return min(lats), min(lons), max(lats), max(lons)
