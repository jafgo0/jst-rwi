"""The bundled BWSC route against the 2025 official figures. No network."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from rwi.contracts import WaypointKind
from rwi.core.geo import destination_point
from rwi.regulations import SCHEDULE
from rwi.route import build_trajectory, point_at_distance, project_onto_route
from rwi.route.bwsc import (
    BWSC_CONTROL_STOP_DWELL_S,
    BWSC_DAILY_WINDOW_EVENT_TIME,
    DEFAULT_ROUTE_FILE,
    OFFICIAL_2025_CONTROL_STOPS,
    OFFICIAL_2025_TOTAL_KM,
    default_routes_dir,
    load_bwsc_route,
)


@pytest.fixture(scope="module")
def route():
    return load_bwsc_route()


@pytest.mark.science
def test_total_distance_against_official_2025_distance_chart(route) -> None:
    """Reference: 2025 BWSC Route Notes V1.0 (published 13 Jul 2025), final entry
    "3026.92 ... final control stop into Victoria Park", cumulative km from State Square.

    Our geometry is an OpenStreetMap shortest path, NOT the official route. Measured on
    2026-11-01: 3021.17 km, i.e. -5.75 km (-0.19 %). Sources of the difference: the shortest
    OSM path cuts the official urban detours in Darwin and Adelaide (the notes route through
    the CBD ring roads), odometer-style rounding in the official figure ("approx km"), and
    the straight-line links at both ends. Tolerance 0.5 % (15 km) guards against a wrong or
    truncated file while admitting that difference; it is NOT a claim that the distances agree.
    """
    official_m = OFFICIAL_2025_TOTAL_KM * 1000.0
    assert route.total_distance_m == pytest.approx(official_m, rel=0.005)
    assert route.total_distance_m < official_m  # shortest-path geometry is not longer here


@pytest.mark.science
def test_control_stops_match_official_list_and_order(route) -> None:
    """Nine control stops, in the order and with the names of the 2025 Route Notes.

    Polyline positions are town/roadhouse reference points snapped to our polyline, so they
    are compared with the published km at a coarse 8 km tolerance. Katherine is the worst
    case (-6.2 km): the notes put the control stop on the southern edge of town (km 322),
    while our reference coordinate is the town centre (~km 316). The remaining differences
    (-1.4 .. +2.1 km up to Barrow Creek, -2.5 .. -5.9 km further south) mix reference-point
    offset with the cumulative route-length difference.
    """
    stops = route.control_stops
    assert [s.name for s in stops] == [
        f"{c.name} Control Stop" for c in OFFICIAL_2025_CONTROL_STOPS
    ]
    assert len(stops) == 9
    cum = [s.cumulative_distance_m for s in stops]
    assert cum == sorted(cum)
    for stop, official in zip(stops, OFFICIAL_2025_CONTROL_STOPS, strict=True):
        assert stop.cumulative_distance_m / 1000.0 == pytest.approx(official.km, abs=8.0)
        assert stop.mandatory_dwell_s == BWSC_CONTROL_STOP_DWELL_S
        assert f"published km {official.km:.1f}" in (stop.notes or "")


@pytest.mark.science
def test_official_2025_table_is_internally_consistent() -> None:
    kms = [c.km for c in OFFICIAL_2025_CONTROL_STOPS]
    assert kms == sorted(kms)
    assert kms[0] == 322.0 and kms[-1] == 2720.5
    assert kms[-1] < OFFICIAL_2025_TOTAL_KM


def test_start_and_finish_waypoints_and_state_square(route) -> None:
    first, last = route.waypoints[0], route.waypoints[-1]
    assert first.kind is WaypointKind.START and "State Square" in (first.name or "")
    assert last.kind is WaypointKind.FINISH and "Victoria Park" in (last.name or "")
    # start coordinate is the one printed in the 2025 notes (S12.46306 E130.83781)
    assert first.point.latitude_deg == pytest.approx(-12.46306, abs=1e-5)
    assert first.point.longitude_deg == pytest.approx(130.83781, abs=1e-5)
    assert last.cumulative_distance_m == route.total_distance_m


def test_provenance_is_honest_about_what_this_is(route) -> None:
    desc = route.source_description
    assert "NOT the official" in desc
    assert "OpenStreetMap" in desc
    assert route.provenance.license is not None and "ODbL" in route.provenance.license
    assert "2027 route notes are not yet published" in desc
    assert "Elevation: NOT available" in desc
    assert all(w.point.elevation_m is None for w in route.waypoints)


def test_geojson_file_declares_non_official_geometry() -> None:
    doc = json.loads((default_routes_dir() / DEFAULT_ROUTE_FILE).read_text(encoding="utf-8"))
    meta = doc["rwi_provenance"]
    assert meta["is_official_race_route"] is False
    assert "ODbL" in meta["license"]


def test_route_is_a_few_thousand_vertices_and_projection_round_trips(route) -> None:
    assert 1_000 < len(route.waypoints) < 6_000
    for d in range(0, int(route.total_distance_m), 97_000):
        p, h = point_at_distance(route, float(d))
        right = destination_point(p, (h + 90.0) % 360.0, 50.0)
        proj = project_onto_route(route, right)
        assert proj.is_on_route
        assert proj.distance_along_m == pytest.approx(d, abs=60.0)  # 50 m offset on bends
        assert proj.cross_track_m == pytest.approx(50.0, abs=15.0)  # right of travel (+)


def test_darwin_to_adelaide_trajectory_with_assumed_window(route) -> None:
    start = datetime(2027, 8, 22, 22, 30, tzinfo=UTC)  # 08:00 ACST on 23 Aug
    traj = build_trajectory(
        route,
        start_distance_m=0.0,
        start_time_utc=start,
        speed_mps=90.0 / 3.6,
        daily_window_local=BWSC_DAILY_WINDOW_EVENT_TIME,
    )
    assert traj.assumptions["control_stops_with_dwell"] == 9.0
    assert traj.assumptions["dwell_total_s"] == 9 * BWSC_CONTROL_STOP_DWELL_S
    assert traj.assumptions["daily_window_verified"] is True  # clauses 3.21.1-2 / 3.27.9
    assert traj.assumptions["night_driving_enforced"] is False  # clause 3.24.8 not enforced
    # 3021 km at 25 m/s = 33.6 h of driving plus 4.5 h of stops: four overnight stops at 9 h/day
    assert traj.assumptions["overnight_stops"] == 4.0
    assert traj.arrival_utc is not None
    assert (
        datetime(2027, 8, 26, 12, 0, tzinfo=UTC)
        < traj.arrival_utc
        < datetime(2027, 8, 27, 6, 0, tzinfo=UTC)
    )
    names = [
        p.waypoint.name
        for p in traj.points
        if p.waypoint and p.waypoint.kind is WaypointKind.CONTROL_STOP
    ]
    assert len(names) == 9


def test_bwsc_constants_are_imported_from_regulations_not_redeclared() -> None:
    assert SCHEDULE.control_stop_duration_s == BWSC_CONTROL_STOP_DWELL_S  # clause 3.27.8
    assert (
        SCHEDULE.daily_start,
        SCHEDULE.daily_stop,
    ) == BWSC_DAILY_WINDOW_EVENT_TIME  # 3.21 / 3.27.9
