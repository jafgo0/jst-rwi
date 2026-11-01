"""Geometry: point_at_distance, project_onto_route, elevation/grade. No network."""

from __future__ import annotations

import itertools
import math
import random
from datetime import UTC, datetime

import pytest

from rwi.contracts import Route, RouteWaypoint
from rwi.core.errors import RouteError
from rwi.core.geo import GeoPoint, destination_point, geodesic_distance_m
from rwi.core.provenance import DataKind, Provenance
from rwi.route.geometry import (
    _evaluate_segment,
    _index,
    elevation_at_distance,
    elevation_profile,
    grade_at_distance,
    has_elevation,
    point_at_distance,
    project_onto_route,
)

PROV = Provenance(
    provider="test",
    dataset="synthetic",
    kind=DataKind.SIMULATION,
    retrieved_at_utc=datetime(2026, 11, 1, tzinfo=UTC),
)


def make_route(points: list[GeoPoint], route_id: str = "t") -> Route:
    wps: list[RouteWaypoint] = []
    cum = 0.0
    for i, p in enumerate(points):
        if i:
            cum += geodesic_distance_m(points[i - 1], p)
        wps.append(RouteWaypoint(point=p, cumulative_distance_m=cum))
    return Route(
        id=route_id,
        name=route_id,
        waypoints=wps,
        total_distance_m=cum,
        provenance=PROV,
        source_description="synthetic test route",
    )


def pt(lat: float, lon: float, ele: float | None = None) -> GeoPoint:
    return GeoPoint(latitude_deg=lat, longitude_deg=lon, elevation_m=ele)


@pytest.fixture
def ns_route() -> Route:
    """Straight northbound geodesic of 100 km along 130E, from 20S."""
    a = pt(-20.0, 130.0)
    return make_route([a, destination_point(a, 0.0, 100_000.0)], "ns")


@pytest.fixture
def l_route() -> Route:
    """North 50 km then east 50 km."""
    a = pt(-20.0, 130.0)
    b = destination_point(a, 0.0, 50_000.0)
    c = destination_point(b, 90.0, 50_000.0)
    return make_route([a, b, c], "L")


@pytest.fixture
def elev_route() -> Route:
    a = pt(-20.0, 130.0, 100.0)
    b = destination_point(a, 0.0, 10_000.0)
    b = pt(b.latitude_deg, b.longitude_deg, 200.0)
    c = destination_point(b, 0.0, 10_000.0)
    c = pt(c.latitude_deg, c.longitude_deg, 150.0)
    return make_route([a, b, c], "elev")


# --------------------------------------------------------------------------- point_at_distance


def test_point_at_distance_start_mid_end(ns_route: Route) -> None:
    p0, h0 = point_at_distance(ns_route, 0.0)
    assert p0 == ns_route.waypoints[0].point
    assert h0 == pytest.approx(0.0, abs=1e-6) or h0 == pytest.approx(360.0, abs=1e-6)
    mid, hm = point_at_distance(ns_route, 40_000.0)
    assert geodesic_distance_m(ns_route.waypoints[0].point, mid) == pytest.approx(
        40_000.0, abs=0.01
    )
    assert mid.longitude_deg == pytest.approx(130.0, abs=1e-9)
    assert hm == pytest.approx(0.0, abs=1e-6) or hm == pytest.approx(360.0, abs=1e-6)
    end, _ = point_at_distance(ns_route, ns_route.total_distance_m)
    assert end == ns_route.waypoints[-1].point


def test_point_at_distance_on_vertex_uses_next_segment_heading(l_route: Route) -> None:
    corner, heading = point_at_distance(l_route, l_route.waypoints[1].cumulative_distance_m)
    assert corner == l_route.waypoints[1].point
    assert heading == pytest.approx(90.0, abs=0.01)  # eastbound leg starts here
    _, before = point_at_distance(l_route, l_route.waypoints[1].cumulative_distance_m - 1.0)
    assert before == pytest.approx(0.0, abs=0.01) or before == pytest.approx(360.0, abs=0.01)
    # end of route reports the last segment's heading, not an undefined one
    _, last = point_at_distance(l_route, l_route.total_distance_m)
    assert 85.0 < last < 95.0


@pytest.mark.parametrize("bad", [-0.001, 100_000.5, math.nan, math.inf])
def test_point_at_distance_out_of_range_raises(ns_route: Route, bad: float) -> None:
    with pytest.raises(RouteError):
        point_at_distance(ns_route, bad if bad != 100_000.5 else ns_route.total_distance_m + 1.0)


def test_point_at_distance_interpolates_elevation(elev_route: Route) -> None:
    p, _ = point_at_distance(elev_route, 5_000.0)
    assert p.elevation_m is not None
    assert p.elevation_m == pytest.approx(150.0, abs=0.01)


def test_zero_length_segment_is_skipped() -> None:
    a = pt(-20.0, 130.0)
    b = destination_point(a, 0.0, 10_000.0)
    c = destination_point(b, 0.0, 10_000.0)
    # duplicate vertex b gives a zero-length segment (a loader would drop it; geometry must cope)
    route = make_route([a, b, b, c])
    p, h = point_at_distance(route, 10_000.0)
    assert geodesic_distance_m(p, b) < 0.01
    assert h == pytest.approx(0.0, abs=0.01) or h == pytest.approx(360.0, abs=0.01)
    proj = project_onto_route(route, destination_point(b, 90.0, 100.0))
    assert proj.distance_along_m == pytest.approx(10_000.0, abs=1.0)


# --------------------------------------------------------------------------- projection


def test_projection_of_point_on_line_has_zero_cross_track(ns_route: Route) -> None:
    on_line, _ = point_at_distance(ns_route, 33_333.0)
    proj = project_onto_route(ns_route, on_line)
    assert abs(proj.cross_track_m) < 0.01
    assert proj.distance_along_m == pytest.approx(33_333.0, abs=0.01)
    assert proj.is_on_route
    assert proj.segment_index == 0


def test_projection_cross_track_sign_east_of_northbound_is_positive(ns_route: Route) -> None:
    on_line, _ = point_at_distance(ns_route, 50_000.0)
    east = destination_point(on_line, 90.0, 1000.0)
    west = destination_point(on_line, 270.0, 1000.0)
    pe = project_onto_route(ns_route, east)
    pw = project_onto_route(ns_route, west)
    assert pe.cross_track_m == pytest.approx(+1000.0, abs=0.01)  # right of travel
    assert pw.cross_track_m == pytest.approx(-1000.0, abs=0.01)  # left of travel
    assert pe.distance_along_m == pytest.approx(50_000.0, abs=2.0)
    assert pe.heading_deg == pytest.approx(0.0, abs=0.1) or pe.heading_deg == pytest.approx(
        360.0, abs=0.1
    )


def test_projection_tolerance_controls_is_on_route(ns_route: Route) -> None:
    on_line, _ = point_at_distance(ns_route, 50_000.0)
    east = destination_point(on_line, 90.0, 1000.0)
    assert not project_onto_route(ns_route, east).is_on_route  # default 500 m
    assert project_onto_route(ns_route, east, tolerance_m=1500.0).is_on_route
    assert project_onto_route(ns_route, east, tolerance_m=2000.0).tolerance_m == 2000.0


def test_projection_far_point_is_off_route(ns_route: Route) -> None:
    far = destination_point(ns_route.waypoints[0].point, 90.0, 200_000.0)
    proj = project_onto_route(ns_route, far)
    assert not proj.is_on_route
    assert proj.cross_track_m == pytest.approx(200_000.0, rel=0.01)


def test_projection_beyond_end_clamps_to_route_end(ns_route: Route) -> None:
    beyond = destination_point(ns_route.waypoints[-1].point, 0.0, 10_000.0)
    proj = project_onto_route(ns_route, beyond)
    assert proj.distance_along_m == pytest.approx(ns_route.total_distance_m, abs=0.01)
    assert not proj.is_on_route
    before = destination_point(ns_route.waypoints[0].point, 180.0, 10_000.0)
    assert project_onto_route(ns_route, before).distance_along_m == pytest.approx(0.0, abs=0.01)


def test_projection_on_l_route_picks_correct_leg(l_route: Route) -> None:
    corner = l_route.waypoints[1]
    on_second, _ = point_at_distance(l_route, corner.cumulative_distance_m + 20_000.0)
    south_of_second = destination_point(on_second, 180.0, 300.0)  # right of an eastbound leg
    proj = project_onto_route(l_route, south_of_second)
    assert proj.segment_index == 1
    assert proj.cross_track_m == pytest.approx(+300.0, abs=1.0)
    assert proj.distance_along_m == pytest.approx(corner.cumulative_distance_m + 20_000.0, abs=2.0)
    # a point on the inside of the elbow is nearest leg 2 and lies left of travel
    inside = destination_point(destination_point(corner.point, 90.0, 5_000.0), 0.0, 100.0)
    p2 = project_onto_route(l_route, inside)
    assert p2.segment_index == 1
    assert p2.cross_track_m == pytest.approx(-100.0, abs=1.0)


def test_projection_beyond_corner_matches_vertex(l_route: Route) -> None:
    corner = l_route.waypoints[1]
    outside_corner = destination_point(corner.point, 0.0, 700.0)  # north of the elbow
    proj = project_onto_route(l_route, outside_corner)
    assert proj.distance_along_m == pytest.approx(corner.cumulative_distance_m, abs=1.0)
    assert abs(proj.cross_track_m) == pytest.approx(700.0, abs=1.0)


def test_distance_along_is_monotonic_along_a_track(l_route: Route) -> None:
    track = []
    for d in range(0, int(l_route.total_distance_m), 2_500):
        p, h = point_at_distance(l_route, float(d))
        track.append(destination_point(p, (h + 90.0) % 360.0, 150.0))  # 150 m right of route
    along = [project_onto_route(l_route, p).distance_along_m for p in track]
    assert all(b >= a for a, b in itertools.pairwise(along))
    assert along[-1] > along[0]


def test_round_trip_point_at_distance_then_project(l_route: Route) -> None:
    """Stated tolerance: 1 cm (geodesic interpolation vs ellipsoidal foot-point refinement)."""
    for d in [0.0, 1.0, 12_345.6, 49_999.0, 50_000.0, 50_001.0, 75_000.0, l_route.total_distance_m]:
        p, _ = point_at_distance(l_route, d)
        proj = project_onto_route(l_route, p)
        assert proj.distance_along_m == pytest.approx(d, abs=0.01)
        assert abs(proj.cross_track_m) < 0.01


def test_prefilter_matches_brute_force_on_a_dense_wiggly_route() -> None:
    rng = random.Random(7)
    p = pt(-30.0, 135.0)
    pts = [p]
    heading = 20.0
    for _ in range(3000):  # ~3000 vertices, ~ 3 km apart, meandering
        heading += rng.uniform(-25.0, 25.0)
        p = destination_point(p, heading % 360.0, rng.uniform(500.0, 3_000.0))
        pts.append(p)
    route = make_route(pts, "wiggly")
    idx = _index(route)
    for _ in range(40):
        i = rng.randrange(len(pts) - 1)
        base, _ = point_at_distance(route, route.waypoints[i].cumulative_distance_m + 200.0)
        q = destination_point(base, rng.uniform(0, 360), rng.uniform(0.0, 20_000.0))
        brute = min(
            (_evaluate_segment(idx, s, q) for s in range(len(pts) - 1)),
            key=lambda c: c.distance_m,
        )
        got = project_onto_route(route, q, tolerance_m=1e9)
        assert abs(got.cross_track_m) == pytest.approx(brute.distance_m, abs=1e-6)
        assert got.distance_along_m == pytest.approx(brute.along_m, abs=1e-6)


def test_projection_is_fast_on_thousands_of_vertices() -> None:
    import time as _time

    a = pt(-20.0, 130.0)
    pts = [destination_point(a, 10.0, 1_000.0 * k) for k in range(5000)]
    route = make_route(pts, "long")
    q = destination_point(pts[2500], 90.0, 100.0)
    project_onto_route(route, q)  # build the index
    t0 = _time.perf_counter()
    for _ in range(200):
        project_onto_route(route, q)
    per_call = (_time.perf_counter() - t0) / 200
    assert (
        per_call < 0.02
    )  # generous: typical is well under 1 ms; a full scan with pyproj would be ~50 ms


# --------------------------------------------------------------------------- elevation / grade


def test_elevation_profile_and_grade(elev_route: Route) -> None:
    prof = elevation_profile(elev_route)
    assert [round(e) for _, e in prof] == [100, 200, 150]
    assert prof[0][0] == 0.0
    assert has_elevation(elev_route)
    # leg 1: +100 m over ~10 km; leg 2: -50 m over ~10 km
    assert grade_at_distance(elev_route, 2_000.0) == pytest.approx(0.01, rel=0.01)
    assert grade_at_distance(elev_route, 15_000.0) == pytest.approx(-0.005, rel=0.01)
    assert elevation_at_distance(elev_route, 5_000.0) == pytest.approx(150.0, abs=0.1)
    # secant over a window straddling the peak (5 km to 15 km)
    w = grade_at_distance(elev_route, 10_000.0, window_m=10_000.0)
    expected = (
        elevation_at_distance(elev_route, 15_000.0) - elevation_at_distance(elev_route, 5_000.0)
    ) / 10_000.0
    assert w == pytest.approx(expected, abs=1e-9)


def test_grade_is_zero_and_profile_empty_when_elevation_unknown(ns_route: Route) -> None:
    assert not has_elevation(ns_route)
    assert elevation_profile(ns_route) == []
    assert grade_at_distance(ns_route, 1_000.0) == 0.0
    assert grade_at_distance(ns_route, 1_000.0, window_m=500.0) == 0.0
    assert elevation_at_distance(ns_route, 1_000.0) is None


def test_grade_out_of_range_raises(elev_route: Route) -> None:
    with pytest.raises(RouteError):
        grade_at_distance(elev_route, elev_route.total_distance_m + 1.0)
