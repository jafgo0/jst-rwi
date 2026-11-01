"""Route geometry: positions along a route, map matching, elevation and grade.

Conventions: distances in metres along the route polyline (cumulative from the
first waypoint), bearings in degrees clockwise from true north, grade is
rise/run (dimensionless). All geodesy is delegated to :mod:`rwi.core.geo`
(WGS84, pyproj/Karney for lengths, bearings, interpolation); nothing is
re-derived here.

Performance: each :class:`~rwi.contracts.Route` is indexed once (cached by
object identity, last 8 routes) into numpy arrays of cumulative distances and
per-segment bounding boxes.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

import numpy as np

from rwi.contracts import Route, RouteProjection
from rwi.core.errors import RouteError
from rwi.core.geo import (
    GeoPoint,
    along_track_distance_m,
    cross_track_distance_m,
    geodesic_distance_m,
    haversine_m,
    initial_bearing_deg,
    interpolate_point,
)

__all__ = [
    "elevation_at_distance",
    "elevation_profile",
    "grade_at_distance",
    "has_elevation",
    "point_at_distance",
    "project_onto_route",
]

_M_PER_DEG_LAT_MIN = 110_500.0  # smallest length of 1 deg of latitude on WGS84 (~110.57 km)
_M_PER_DEG_LON_EQ = 111_300.0  # largest length of 1 deg of longitude on WGS84 (~111.32 km)
_CACHE_SIZE = 8


@dataclass(frozen=True)
class _Index:
    route: Route  # strong reference: keeps id(route) from being reused while cached
    points: list[GeoPoint]
    cum: np.ndarray  # cumulative distance per waypoint, shape (n,)
    cum_list: list[float]
    seg_len: np.ndarray  # shape (n-1,)
    min_lat: np.ndarray
    max_lat: np.ndarray
    min_lon: np.ndarray
    max_lon: np.ndarray
    valid: np.ndarray  # False for zero-length segments


_CACHE: dict[int, _Index] = {}


def _index(route: Route) -> _Index:
    hit = _CACHE.get(id(route))
    if hit is not None and hit.route is route:
        return hit
    pts = [w.point for w in route.waypoints]
    cum_list = [w.cumulative_distance_m for w in route.waypoints]
    cum = np.asarray(cum_list, dtype=float)
    lat = np.array([p.latitude_deg for p in pts])
    lon = np.array([p.longitude_deg for p in pts])
    seg_len = np.diff(cum)
    idx = _Index(
        route=route,
        points=pts,
        cum=cum,
        cum_list=cum_list,
        seg_len=seg_len,
        min_lat=np.minimum(lat[:-1], lat[1:]),
        max_lat=np.maximum(lat[:-1], lat[1:]),
        min_lon=np.minimum(lon[:-1], lon[1:]),
        max_lon=np.maximum(lon[:-1], lon[1:]),
        valid=seg_len > 0.0,
    )
    if len(_CACHE) >= _CACHE_SIZE:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[id(route)] = idx
    return idx


def _segment_for_distance(idx: _Index, distance_m: float) -> int:
    """Index of the non-degenerate segment containing ``distance_m`` (already range-checked)."""
    n_seg = len(idx.points) - 1
    i = min(bisect.bisect_right(idx.cum_list, distance_m) - 1, n_seg - 1)
    i = max(i, 0)
    if idx.valid[i]:
        return i
    for j in range(i + 1, n_seg):  # skip forward over zero-length segments
        if idx.valid[j]:
            return j
    for j in range(i - 1, -1, -1):  # at the very end: fall back to the last real segment
        if idx.valid[j]:
            return j
    raise RouteError("route has no segment of non-zero length")


def point_at_distance(route: Route, distance_m: float) -> tuple[GeoPoint, float]:
    """Position and heading at ``distance_m`` along the route.

    Args:
        route: the route (cumulative distances in metres).
        distance_m: metres from the route start, in ``[0, route.total_distance_m]``.

    Returns:
        ``(point, heading_deg)``. The point is interpolated along the WGS84
        geodesic of the containing segment (elevation interpolated when both
        segment ends have one). ``heading_deg`` in [0, 360) is the travel
        direction of that segment, i.e. the forward azimuth from the point to
        the next vertex; exactly on a vertex it is the heading of the segment
        that starts there, and at the end of the route that of the last segment.

    Raises:
        RouteError: if ``distance_m`` is non-finite or outside the route.

    Complexity: O(log N) after the first (O(N)) indexing of the route.
    """
    if not math.isfinite(distance_m):
        raise RouteError(f"distance is not finite: {distance_m!r}")
    total = route.total_distance_m
    if distance_m < 0.0 or distance_m > total:
        raise RouteError(f"distance {distance_m:.1f} m is outside the route [0, {total:.1f}] m")
    idx = _index(route)
    i = _segment_for_distance(idx, distance_m)
    a, b = idx.points[i], idx.points[i + 1]
    seg = float(idx.seg_len[i])
    frac = min(max((distance_m - float(idx.cum[i])) / seg, 0.0), 1.0)
    heading = initial_bearing_deg(a, b)
    point = a if frac == 0.0 else b if frac == 1.0 else interpolate_point(a, b, frac)
    if 0.0 < frac < 1.0:
        heading = initial_bearing_deg(point, b)
    return point, heading


def _lower_bound_m(idx: _Index, lat0: float, lon0: float) -> np.ndarray:
    """Conservative lower bound on the distance from a point to each segment's bounding box."""
    dlat = np.maximum(np.maximum(idx.min_lat - lat0, lat0 - idx.max_lat), 0.0)
    dlon = np.maximum(np.maximum(idx.min_lon - lon0, lon0 - idx.max_lon), 0.0)
    worst_lat = np.maximum(np.abs(idx.min_lat), np.abs(idx.max_lat))
    worst_lat = np.maximum(worst_lat, abs(lat0))
    cos_min = np.cos(np.radians(np.minimum(worst_lat, 89.0)))
    lb = np.hypot(dlon * _M_PER_DEG_LON_EQ * cos_min, dlat * _M_PER_DEG_LAT_MIN)
    return np.maximum(lb * 0.98 - 50.0, 0.0)  # safety margin for the planar approximation


@dataclass(frozen=True)
class _Candidate:
    distance_m: float  # metres from the point to the route segment (>= 0)
    signed_cross_m: float
    along_m: float
    seg: int
    foot: GeoPoint


def _evaluate_segment(idx: _Index, i: int, point: GeoPoint) -> _Candidate:
    a, b = idx.points[i], idx.points[i + 1]
    seg_len = float(idx.seg_len[i])
    sph_len = haversine_m(a, b)
    xt = cross_track_distance_m(point, a, b)  # spherical first guess, signed, + = right
    along_sph = along_track_distance_m(point, a, b)
    frac = along_sph / sph_len if sph_len > 0.0 else 0.0
    if frac <= 0.0:
        foot, frac, dist = a, 0.0, geodesic_distance_m(point, a)
        sign = -1.0 if xt < 0.0 else 1.0
    elif frac >= 1.0:
        foot, frac, dist = b, 1.0, geodesic_distance_m(point, b)
        sign = -1.0 if xt < 0.0 else 1.0
    else:
        # Newton refinement on the ellipsoid: move the foot along the segment until the
        # geodesic to the point is perpendicular to the segment's azimuth at the foot.
        foot = interpolate_point(a, b, frac)
        dist, theta = 0.0, math.pi / 2.0
        for _ in range(4):
            dist = geodesic_distance_m(point, foot)
            if dist < 1e-6:
                theta = math.pi / 2.0
                break
            theta = math.radians(initial_bearing_deg(foot, point) - initial_bearing_deg(foot, b))
            correction = dist * math.cos(theta)
            if abs(correction) < 1e-4:
                break
            frac = min(max(frac + correction / seg_len, 0.0), 1.0)
            foot = interpolate_point(a, b, frac)
        dist = geodesic_distance_m(point, foot)
        sign = -1.0 if math.sin(theta) < 0.0 else 1.0
    return _Candidate(dist, sign * dist, float(idx.cum[i]) + frac * seg_len, i, foot)


def project_onto_route(
    route: Route, point: GeoPoint, *, tolerance_m: float = 500.0
) -> RouteProjection:
    """Map-match a position onto the route polyline (nearest-segment projection).

    Args:
        route: the route.
        point: the position to match (e.g. a GPS fix).
        tolerance_m: ``is_on_route`` is True when the perpendicular distance
            is at most this many metres. It does not restrict the search: the
            nearest point on the whole route is always returned.

    Returns:
        :class:`RouteProjection` with ``distance_along_m`` (route-cumulative
        metres of the foot point), ``cross_track_m`` (signed; positive = right
        of the direction of travel, negative = left; magnitude is the exact
        WGS84 geodesic distance to the foot point), the segment index, the
        foot point, and the segment heading.

    Method: for every segment a bounding-box lower bound on the distance to
    the point is computed vectorised (numpy); segments are then examined in
    increasing lower-bound order and the search stops as soon as the next
    lower bound exceeds the best distance found. Each examined segment is
    evaluated with the spherical cross/along-track formulas of
    :mod:`rwi.core.geo` for a first guess of the foot point, which is then
    refined by a few Newton steps on the WGS84 ellipsoid (so that the geodesic
    from the foot to the point is perpendicular to the segment), and the
    distance and sign are taken from that ellipsoidal solution. Beyond a segment end the nearest
    vertex is used (so a point past a corner is matched to the corner).

    Complexity: O(N) numpy work for the bounding-box pass plus O(N log N) for
    the ordering, and exact (pyproj) evaluation of typically < 10 segments,
    independent of N. A route of ~3500 vertices projects in well under a
    millisecond of numpy plus a few hundred microseconds of geodesy.

    Accuracy: sub-millimetre foot point and cross-track on a segment (the
    refinement converges to 1e-4 m); the route is treated as piecewise
    geodesic segments between vertices, so the accuracy of the *road* is
    that of the vertices. Not designed for the antimeridian.

    Ambiguity: for routes that return near themselves the global nearest
    segment wins; use :func:`point_at_distance` windows if a prior position is
    known (not implemented here).

    Raises:
        RouteError: if the route has no segment of non-zero length.
    """
    idx = _index(route)
    if not idx.valid.any():
        raise RouteError("route has no segment of non-zero length")
    lb = _lower_bound_m(idx, point.latitude_deg, point.longitude_deg)
    lb = np.where(idx.valid, lb, np.inf)
    order = np.argsort(lb, kind="stable")
    best: _Candidate | None = None
    for i in order:
        if not math.isfinite(lb[i]) or (best is not None and lb[i] > best.distance_m):
            break
        cand = _evaluate_segment(idx, int(i), point)
        if best is None or cand.distance_m < best.distance_m - 1e-9:
            best = cand
        elif abs(cand.distance_m - best.distance_m) <= 1e-9 and cand.seg < best.seg:
            best = cand  # tie at a shared vertex: prefer the earlier segment
    assert best is not None
    a, b = idx.points[best.seg], idx.points[best.seg + 1]
    at_vertex = best.foot is a or best.foot is b
    heading = initial_bearing_deg(a, b) if at_vertex else initial_bearing_deg(best.foot, b)
    return RouteProjection(
        distance_along_m=min(max(best.along_m, 0.0), route.total_distance_m),
        cross_track_m=best.signed_cross_m,
        segment_index=best.seg,
        point_on_route=best.foot,
        heading_deg=heading % 360.0,
        is_on_route=best.distance_m <= tolerance_m,
        tolerance_m=tolerance_m,
    )


def has_elevation(route: Route) -> bool:
    """True only if every waypoint of the route carries an elevation."""
    return all(w.point.elevation_m is not None for w in route.waypoints)


def elevation_profile(route: Route) -> list[tuple[float, float]]:
    """``(cumulative_distance_m, elevation_m)`` for every waypoint with a known elevation.

    Waypoints whose elevation is unknown are omitted (never filled with 0 m),
    so a route without elevation data returns an empty list. Elevation is metres
    above mean sea level as supplied by the route source (see
    ``Route.source_description`` for its origin and accuracy).
    """
    return [
        (w.cumulative_distance_m, w.point.elevation_m)
        for w in route.waypoints
        if w.point.elevation_m is not None
    ]


def elevation_at_distance(route: Route, distance_m: float) -> float | None:
    """Linearly interpolated elevation (m) at ``distance_m``, or None when unknown.

    None is returned if either end of the containing segment has no elevation.

    Raises:
        RouteError: if the distance is outside the route.
    """
    point, _ = point_at_distance(route, distance_m)
    return point.elevation_m


def grade_at_distance(route: Route, distance_m: float, *, window_m: float | None = None) -> float:
    """Road gradient (rise/run, dimensionless; +0.02 = 2 % uphill) at ``distance_m``.

    By default the grade of the route segment containing ``distance_m`` is
    returned (elevation difference of its endpoints over its along-route
    length). With ``window_m`` the grade is the secant over
    ``[d - window_m/2, d + window_m/2]`` (clamped to the route), which suppresses
    noise in dense elevation data.

    **Returns 0.0 when elevation is unknown** for the relevant waypoints. This
    is a deliberate "no information" value, not a measurement of flat terrain:
    callers that must distinguish should check :func:`has_elevation`. The
    platform never invents terrain.

    Raises:
        RouteError: if the distance is outside the route.
    """
    point_at_distance(route, distance_m)  # range check
    idx = _index(route)
    if window_m is None or window_m <= 0.0:
        i = _segment_for_distance(idx, distance_m)
        ea, eb = idx.points[i].elevation_m, idx.points[i + 1].elevation_m
        if ea is None or eb is None:
            return 0.0
        return (eb - ea) / float(idx.seg_len[i])
    lo = max(distance_m - window_m / 2.0, 0.0)
    hi = min(distance_m + window_m / 2.0, route.total_distance_m)
    if hi <= lo:
        return 0.0
    e_lo, e_hi = elevation_at_distance(route, lo), elevation_at_distance(route, hi)
    if e_lo is None or e_hi is None:
        return 0.0
    return (e_hi - e_lo) / (hi - lo)
