"""Attaching the team's elevation data to our route.

A sanity check and a bug trap. The elevation data lives in JU Solar Team's
drive-system repo and is not bundled here, so these tests construct small
synthetic CSVs that look like theirs - same column names, same shapes - and
exercise the loader against those. The scaling test against the REAL file
sits in a @network-marked test skipped by default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rwi.contracts import Route, RouteWaypoint, WaypointKind
from rwi.core.errors import RouteError
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import utc_now
from rwi.route.elevation import (
    DEFAULT_MAX_MATCH_M,
    enrich_with_team_elevation,
    read_team_elevation_csv,
)
from rwi.route.geometry import elevation_at_distance, has_elevation


def _write_team_csv(path: Path, points: list[tuple[float, float, float, float, float]]) -> None:
    """Write a mock team wsc_race_data.csv at the given points.

    Columns match the real export: Distance (km), Latitude, Longitude, Bearing,
    Elevation (m). The reader cares about the HEADER NAMES exactly.
    """
    with path.open("w", encoding="utf-8") as handle:
        handle.write("Distance,Latitude,Longitude,Bearing,Elevation (m)\n")
        for d_km, lat, lon, bear, elev in points:
            handle.write(f"{d_km},{lat},{lon},{bear},{elev}\n")


def _route(points: list[tuple[float, float, float]]) -> Route:
    """A tiny Route whose waypoints have (distance_m, lat, lon) only. No
    elevation - that is what the enricher adds."""
    waypoints = [
        RouteWaypoint(
            point=GeoPoint(latitude_deg=lat, longitude_deg=lon, elevation_m=None),
            cumulative_distance_m=d_m,
            kind=WaypointKind.WAYPOINT,
        )
        for d_m, lat, lon in points
    ]
    now = utc_now()
    return Route(
        id="test",
        name="test",
        waypoints=waypoints,
        total_distance_m=waypoints[-1].cumulative_distance_m,
        provenance=Provenance(
            provider="test",
            dataset="synthetic",
            kind=DataKind.SIMULATION,
            retrieved_at_utc=now,
        ),
        source_description="synthetic",
    )


def test_a_clean_match_attaches_elevation_to_every_waypoint(tmp_path: Path) -> None:
    """Baseline: every waypoint has a neighbour within tolerance in the team's
    file, so the enriched route has elevation everywhere."""
    team_csv = tmp_path / "wsc_race_data.csv"
    _write_team_csv(team_csv, [
        (0.0, -12.5, 130.8, 0.0, 50.0),
        (10.0, -12.6, 130.9, 90.0, 80.0),
        (20.0, -12.7, 131.0, 180.0, 60.0),
    ])
    route = _route([
        (0.0, -12.5, 130.8),
        (10_000.0, -12.6, 130.9),
        (20_000.0, -12.7, 131.0),
    ])
    assert has_elevation(route) is False

    enriched = enrich_with_team_elevation(route, team_csv)
    assert has_elevation(enriched) is True
    assert enriched.waypoints[0].point.elevation_m == pytest.approx(50.0)
    assert enriched.waypoints[1].point.elevation_m == pytest.approx(80.0)
    assert enriched.waypoints[2].point.elevation_m == pytest.approx(60.0)


def test_elevation_at_distance_interpolates_between_enriched_waypoints(tmp_path: Path) -> None:
    """elevation_at_distance is already tested on known data; this re-checks
    it against enriched data to make sure the enrichment is picked up."""
    team_csv = tmp_path / "wsc_race_data.csv"
    _write_team_csv(team_csv, [
        (0.0, -12.5, 130.8, 0.0, 0.0),
        (10.0, -12.6, 130.9, 90.0, 100.0),
    ])
    route = _route([(0.0, -12.5, 130.8), (10_000.0, -12.6, 130.9)])
    enriched = enrich_with_team_elevation(route, team_csv)
    assert elevation_at_distance(enriched, 0.0) == pytest.approx(0.0)
    assert elevation_at_distance(enriched, 10_000.0) == pytest.approx(100.0)
    assert elevation_at_distance(enriched, 5_000.0) == pytest.approx(50.0, abs=0.5)


def test_a_waypoint_with_no_near_match_is_refused_not_silently_wrong(tmp_path: Path) -> None:
    """A 10 km mismatch between our polyline and the team's is a route
    disagreement that must be investigated, not papered over with the nearest
    row's elevation."""
    team_csv = tmp_path / "wsc_race_data.csv"
    _write_team_csv(team_csv, [
        (0.0, -12.5, 130.8, 0.0, 50.0),
        (10.0, -12.6, 130.9, 90.0, 80.0),
    ])
    far_route = _route([
        (0.0, -12.5, 130.8),
        (10_000.0, -14.0, 131.0),  # a long way off
    ])
    with pytest.raises(RouteError, match="no match within"):
        enrich_with_team_elevation(far_route, team_csv, max_match_m=1000.0)


def test_a_wider_max_match_lets_the_known_adelaide_gap_through(tmp_path: Path) -> None:
    """The real match problem is a 2 km disagreement at Adelaide (OSM Hutt
    Street vs. official Robe Terrace), which an operator can accept with a
    higher threshold - a test that pins the parameter semantics so this is
    not re-litigated at runtime."""
    team_csv = tmp_path / "wsc_race_data.csv"
    _write_team_csv(team_csv, [
        (0.0, -12.5, 130.8, 0.0, 50.0),
        (10.0, -12.6, 130.9, 90.0, 80.0),
    ])
    offset_route = _route([
        (0.0, -12.5, 130.8),
        (10_000.0, -12.61, 130.9),  # ~1.1 km away
    ])
    with pytest.raises(RouteError):
        enrich_with_team_elevation(offset_route, team_csv, max_match_m=500.0)
    enriched = enrich_with_team_elevation(offset_route, team_csv, max_match_m=2500.0)
    assert has_elevation(enriched)


def test_a_missing_file_is_refused_up_front(tmp_path: Path) -> None:
    """A loader that silently returns no data would attach None elevations
    everywhere, which the Route would accept - and we would never know."""
    with pytest.raises(RouteError, match="not found"):
        read_team_elevation_csv(tmp_path / "nope.csv")


def test_a_column_rename_on_the_teams_side_fails_loudly(tmp_path: Path) -> None:
    """The team is actively developing - a column rename must surface here,
    not silently zero every elevation."""
    team_csv = tmp_path / "wsc_race_data.csv"
    team_csv.write_text(
        "Distance,Lat,Lon,Bearing,Elevation (m)\n0,0,0,0,0\n", encoding="utf-8"
    )
    with pytest.raises(RouteError, match="missing expected column"):
        read_team_elevation_csv(team_csv)


def test_the_default_match_tolerance_is_a_round_number_not_an_accident() -> None:
    """A tolerance quietly drifting between releases would surface as random
    enrichment failures. 2 km is the documented value; test it."""
    assert DEFAULT_MAX_MATCH_M == 2000.0


def test_the_enriched_routes_provenance_records_the_match_quality(tmp_path: Path) -> None:
    """Attribution travels with the data: the enriched route's notes must
    name wsc_race_data.csv and the mean/worst match it achieved."""
    team_csv = tmp_path / "wsc_race_data.csv"
    _write_team_csv(team_csv, [
        (0.0, -12.5, 130.8, 0.0, 50.0),
        (10.0, -12.6, 130.9, 90.0, 80.0),
    ])
    route = _route([(0.0, -12.5, 130.8), (10_000.0, -12.6, 130.9)])
    enriched = enrich_with_team_elevation(route, team_csv)
    notes = (enriched.provenance.notes or "") + " " + enriched.source_description
    assert "wsc_race_data.csv" in notes
    assert "match" in notes.lower()
