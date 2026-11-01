"""Geodesy: ellipsoidal distances, bearings and route-matching primitives."""

from __future__ import annotations

import pytest

from rwi.core.errors import InvalidCoordinateError
from rwi.core.geo import (
    GeoPoint,
    along_track_distance_m,
    cross_track_distance_m,
    destination_point,
    geodesic_distance_m,
    haversine_m,
    initial_bearing_deg,
    interpolate_point,
)


def test_coordinate_validation() -> None:
    with pytest.raises(Exception, match=r"lat|validation"):
        GeoPoint(latitude_deg=91.0, longitude_deg=0.0)
    with pytest.raises(Exception, match=r"lon|validation"):
        GeoPoint(latitude_deg=0.0, longitude_deg=181.0)
    with pytest.raises(InvalidCoordinateError):
        GeoPoint(latitude_deg=float("nan"), longitude_deg=0.0)
    with pytest.raises(InvalidCoordinateError):
        GeoPoint(latitude_deg=0.0, longitude_deg=float("inf"))
    with pytest.raises(InvalidCoordinateError):
        GeoPoint(latitude_deg=0.0, longitude_deg=0.0, elevation_m=float("nan"))


def test_unknown_elevation_is_not_zero() -> None:
    point = GeoPoint(latitude_deg=-12.0, longitude_deg=131.0)
    assert point.elevation_m is None
    assert point.elevation_or_zero_m == 0.0


def test_darwin_adelaide_great_circle_distance(darwin: GeoPoint, adelaide: GeoPoint) -> None:
    """Geodesic (straight-line) distance, NOT the road distance.

    The Stuart Highway route is about 3000 km; the geodesic is ~2609 km. Keeping
    these apart matters: route progress must come from the route polyline, never
    from a point-to-point distance.
    """
    km = geodesic_distance_m(darwin, adelaide) / 1000.0
    assert 2600.0 < km < 2615.0


def test_haversine_agrees_with_the_ellipsoid_within_half_a_percent(
    darwin: GeoPoint, adelaide: GeoPoint
) -> None:
    ellipsoidal = geodesic_distance_m(darwin, adelaide)
    spherical = haversine_m(darwin, adelaide)
    assert abs(ellipsoidal - spherical) / ellipsoidal < 0.005


def test_distance_is_symmetric_and_zero_to_self(darwin: GeoPoint) -> None:
    assert geodesic_distance_m(darwin, darwin) == pytest.approx(0.0, abs=1e-6)
    other = GeoPoint(latitude_deg=-23.7, longitude_deg=133.9)
    assert geodesic_distance_m(darwin, other) == pytest.approx(
        geodesic_distance_m(other, darwin), rel=1e-9
    )


def test_bearings_on_the_meridian_and_the_equator() -> None:
    origin = GeoPoint(latitude_deg=0.0, longitude_deg=0.0)
    assert initial_bearing_deg(origin, GeoPoint(latitude_deg=1.0, longitude_deg=0.0)) == (
        pytest.approx(0.0, abs=1e-6)
    )
    assert initial_bearing_deg(origin, GeoPoint(latitude_deg=0.0, longitude_deg=1.0)) == (
        pytest.approx(90.0, abs=1e-6)
    )
    assert initial_bearing_deg(origin, GeoPoint(latitude_deg=-1.0, longitude_deg=0.0)) == (
        pytest.approx(180.0, abs=1e-6)
    )


def test_destination_point_round_trip(alice_springs: GeoPoint) -> None:
    moved = destination_point(alice_springs, 215.0, 150_000.0)
    assert geodesic_distance_m(alice_springs, moved) == pytest.approx(150_000.0, rel=1e-6)
    assert initial_bearing_deg(alice_springs, moved) == pytest.approx(215.0, abs=1e-6)


def test_interpolate_point_midway_and_elevation() -> None:
    a = GeoPoint(latitude_deg=-12.0, longitude_deg=131.0, elevation_m=30.0)
    b = GeoPoint(latitude_deg=-13.0, longitude_deg=131.0, elevation_m=130.0)
    mid = interpolate_point(a, b, 0.5)
    assert mid.latitude_deg == pytest.approx(-12.5, abs=1e-3)
    assert mid.elevation_m == pytest.approx(80.0)
    assert interpolate_point(a, b, 0.0).latitude_deg == pytest.approx(a.latitude_deg)
    assert interpolate_point(a, b, 1.0).latitude_deg == pytest.approx(b.latitude_deg)


def test_cross_track_sign_is_right_of_travel() -> None:
    """Travelling north along the 131st meridian, a point to the east is to the
    vehicle's right and must give a positive cross-track distance."""
    start = GeoPoint(latitude_deg=-13.0, longitude_deg=131.0)
    end = GeoPoint(latitude_deg=-12.0, longitude_deg=131.0)
    east = GeoPoint(latitude_deg=-12.5, longitude_deg=131.1)
    west = GeoPoint(latitude_deg=-12.5, longitude_deg=130.9)
    assert cross_track_distance_m(east, start, end) > 0
    assert cross_track_distance_m(west, start, end) < 0
    on_line = GeoPoint(latitude_deg=-12.5, longitude_deg=131.0)
    assert abs(cross_track_distance_m(on_line, start, end)) < 1.0


def test_cross_track_magnitude_is_about_right() -> None:
    start = GeoPoint(latitude_deg=0.0, longitude_deg=0.0)
    end = GeoPoint(latitude_deg=1.0, longitude_deg=0.0)
    offset = GeoPoint(latitude_deg=0.5, longitude_deg=0.01)
    expected = haversine_m(
        GeoPoint(latitude_deg=0.5, longitude_deg=0.0), offset
    )
    assert cross_track_distance_m(offset, start, end) == pytest.approx(expected, rel=0.01)


def test_along_track_distance_progresses_and_can_be_negative() -> None:
    start = GeoPoint(latitude_deg=-13.0, longitude_deg=131.0)
    end = GeoPoint(latitude_deg=-12.0, longitude_deg=131.0)
    quarter = GeoPoint(latitude_deg=-12.75, longitude_deg=131.0)
    three_quarters = GeoPoint(latitude_deg=-12.25, longitude_deg=131.0)
    behind = GeoPoint(latitude_deg=-13.25, longitude_deg=131.0)
    d_q = along_track_distance_m(quarter, start, end)
    d_tq = along_track_distance_m(three_quarters, start, end)
    assert 0 < d_q < d_tq
    assert along_track_distance_m(behind, start, end) < 0
