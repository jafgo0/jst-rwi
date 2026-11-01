"""Loaders, validation and control-stop annotation. Files are written by the tests."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from rwi.contracts import WaypointKind
from rwi.core.errors import RouteError
from rwi.core.geo import GeoPoint, destination_point, geodesic_distance_m
from rwi.route.geometry import point_at_distance, project_onto_route
from rwi.route.loader import (
    NamedStop,
    annotate_control_stops,
    load_route_geojson,
    load_route_gpx,
    read_named_stops_geojson,
)

A = GeoPoint(latitude_deg=-20.0, longitude_deg=130.0)
B = destination_point(A, 0.0, 30_000.0)
C = destination_point(B, 90.0, 30_000.0)


def lonlat(p: GeoPoint, ele: float | None = None) -> list[float]:
    return [p.longitude_deg, p.latitude_deg] + ([ele] if ele is not None else [])


def write_json(path: Path, doc: object) -> Path:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def line_doc(coords: list[list[float]]) -> dict:
    return {"type": "LineString", "coordinates": coords}


# --------------------------------------------------------------------------- GeoJSON


def test_geojson_linestring_round_trip_with_elevation(tmp_path: Path) -> None:
    f = write_json(
        tmp_path / "r.geojson", line_doc([lonlat(A, 100), lonlat(B, 250), lonlat(C, 200)])
    )
    route = load_route_geojson(f, route_id="r1", name="Route one")
    assert route.id == "r1" and route.name == "Route one"
    assert [w.point.elevation_m for w in route.waypoints] == [100.0, 250.0, 200.0]
    cum = [w.cumulative_distance_m for w in route.waypoints]
    assert cum[0] == 0.0
    assert all(b >= a for a, b in itertools.pairwise(cum))
    assert cum[1] == pytest.approx(geodesic_distance_m(A, B), abs=1e-6)
    assert route.total_distance_m == pytest.approx(
        geodesic_distance_m(A, B) + geodesic_distance_m(B, C), abs=1e-6
    )
    assert route.waypoints[0].kind is WaypointKind.START
    assert route.waypoints[-1].kind is WaypointKind.FINISH
    assert route.waypoints[1].kind is WaypointKind.WAYPOINT


def test_geojson_2d_has_unknown_elevation_not_zero(tmp_path: Path) -> None:
    f = write_json(tmp_path / "r.geojson", line_doc([lonlat(A), lonlat(B)]))
    route = load_route_geojson(f, route_id="r", name="r")
    assert all(w.point.elevation_m is None for w in route.waypoints)
    assert "NOT available" in route.source_description


def test_geojson_feature_collection_provenance_and_points_ignored(tmp_path: Path) -> None:
    doc = {
        "type": "FeatureCollection",
        "rwi_provenance": {
            "provider": "Some Mapper",
            "dataset": "ds",
            "license": "ODbL-1.0",
            "accuracy": "about 5 m",
            "is_official_race_route": False,
            "retrieved_at_utc": "2026-11-01T22:30:00Z",
        },
        "features": [
            {"type": "Feature", "properties": {}, "geometry": line_doc([lonlat(A), lonlat(B)])},
            {
                "type": "Feature",
                "properties": {"name": "x"},
                "geometry": {"type": "Point", "coordinates": lonlat(A)},
            },
        ],
    }
    route = load_route_geojson(write_json(tmp_path / "fc.geojson", doc), route_id="r", name="r")
    assert route.provenance.license == "ODbL-1.0"
    assert route.provenance.provider == "Some Mapper"
    assert "about 5 m" in route.source_description
    assert "NOT the official race route" in route.source_description
    assert route.provenance.retrieved_at_utc.year == 2026


def test_geojson_without_provenance_says_unknown(tmp_path: Path) -> None:
    f = write_json(tmp_path / "r.geojson", line_doc([lonlat(A), lonlat(B)]))
    route = load_route_geojson(f, route_id="r", name="r")
    assert "unknown" in route.source_description
    assert "not stated" in route.source_description


def test_geojson_multilinestring_is_joined_and_gap_reported(tmp_path: Path) -> None:
    gap_start = destination_point(B, 90.0, 2_000.0)
    doc = {
        "type": "MultiLineString",
        "coordinates": [[lonlat(A), lonlat(B)], [lonlat(gap_start), lonlat(C)]],
    }
    route = load_route_geojson(write_json(tmp_path / "m.geojson", doc), route_id="r", name="r")
    assert len(route.waypoints) == 4
    assert "MultiLineString of 2 parts" in route.source_description
    assert "2000 m in total" in route.source_description


def test_geojson_drops_consecutive_duplicates(tmp_path: Path) -> None:
    f = write_json(tmp_path / "d.geojson", line_doc([lonlat(A), lonlat(A), lonlat(B), lonlat(B)]))
    route = load_route_geojson(f, route_id="r", name="r")
    assert len(route.waypoints) == 2
    assert "duplicate" in route.source_description


@pytest.mark.parametrize(
    "doc",
    [
        line_doc([]),
        line_doc([lonlat(A)]),
        line_doc([lonlat(A), lonlat(A)]),  # distinct-point count is 1
        line_doc([[130.0, float("nan")], lonlat(B)]),
        line_doc([[130.0, float("inf")], lonlat(B)]),
        line_doc([[130.0, 95.0], lonlat(B)]),
        line_doc([[190.0, -20.0], lonlat(B)]),
        line_doc([["a", "b"], lonlat(B)]),
        line_doc([[130.0], lonlat(B)]),
        line_doc([lonlat(A, 99999.0), lonlat(B)]),  # implausible elevation
        {"type": "Point", "coordinates": [130.0, -20.0]},
        {"type": "FeatureCollection", "features": []},
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "properties": {}, "geometry": line_doc([lonlat(A), lonlat(B)])},
                {"type": "Feature", "properties": {}, "geometry": line_doc([lonlat(B), lonlat(C)])},
            ],
        },
        [1, 2, 3],
        {"type": "LineString", "coordinates": "nope"},
    ],
)
def test_geojson_invalid_geometry_raises_route_error(tmp_path: Path, doc: object) -> None:
    with pytest.raises(RouteError):
        load_route_geojson(write_json(tmp_path / "bad.geojson", doc), route_id="r", name="r")


def test_corrupt_and_missing_geojson_raise_route_error(tmp_path: Path) -> None:
    bad = tmp_path / "corrupt.geojson"
    bad.write_text('{"type": "LineString", "coordinates": [[130.0, -20.0], [', encoding="utf-8")
    with pytest.raises(RouteError):
        load_route_geojson(bad, route_id="r", name="r")
    binary = tmp_path / "bin.geojson"
    binary.write_bytes(b"\xff\xfe\x00\x01garbage")
    with pytest.raises(RouteError):
        load_route_geojson(binary, route_id="r", name="r")
    with pytest.raises(RouteError):
        load_route_geojson(tmp_path / "missing.geojson", route_id="r", name="r")


def test_absurd_jump_rejected_and_threshold_configurable(tmp_path: Path) -> None:
    far = destination_point(A, 45.0, 400_000.0)
    f = write_json(tmp_path / "jump.geojson", line_doc([lonlat(A), lonlat(far)]))
    with pytest.raises(RouteError, match="jump"):
        load_route_geojson(f, route_id="r", name="r")
    ok = load_route_geojson(f, route_id="r", name="r", max_jump_m=500_000.0)
    assert ok.total_distance_m == pytest.approx(400_000.0, abs=1.0)
    with pytest.raises(RouteError):
        load_route_geojson(f, route_id="r", name="r", max_jump_m=-1.0)


# --------------------------------------------------------------------------- GPX


def gpx_text(points: list[tuple[float, float, float | None]], *, kind: str = "trk") -> str:
    if kind == "trk":
        body = (
            "<trk><trkseg>"
            + "".join(
                f'<trkpt lat="{la}" lon="{lo}">'
                + (f"<ele>{e}</ele>" if e is not None else "")
                + "</trkpt>"
                for la, lo, e in points
            )
            + "</trkseg></trk>"
        )
    else:
        body = (
            "<rte>" + "".join(f'<rtept lat="{la}" lon="{lo}"/>' for la, lo, _ in points) + "</rte>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<gpx version="1.1" creator="unit-test" xmlns="http://www.topografix.com/GPX/1/1">'
        f"{body}</gpx>"
    )


def test_gpx_round_trip_track_with_elevation(tmp_path: Path) -> None:
    f = tmp_path / "t.gpx"
    f.write_text(
        gpx_text(
            [
                (A.latitude_deg, A.longitude_deg, 10.0),
                (B.latitude_deg, B.longitude_deg, 30.0),
                (C.latitude_deg, C.longitude_deg, 20.0),
            ]
        ),
        encoding="utf-8",
    )
    route = load_route_gpx(f, route_id="g", name="gpx")
    assert [w.point.elevation_m for w in route.waypoints] == [10.0, 30.0, 20.0]
    cum = [w.cumulative_distance_m for w in route.waypoints]
    assert all(b >= a for a, b in itertools.pairwise(cum))
    assert route.total_distance_m == pytest.approx(
        geodesic_distance_m(A, B) + geodesic_distance_m(B, C), abs=0.01
    )
    assert "GPX" in route.source_description


def test_gpx_route_points_and_2d(tmp_path: Path) -> None:
    f = tmp_path / "r.gpx"
    f.write_text(
        gpx_text(
            [(A.latitude_deg, A.longitude_deg, None), (B.latitude_deg, B.longitude_deg, None)],
            kind="rte",
        ),
        encoding="utf-8",
    )
    route = load_route_gpx(f, route_id="g", name="gpx")
    assert len(route.waypoints) == 2
    assert route.waypoints[0].point.elevation_m is None


def test_corrupt_gpx_raises_route_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.gpx"
    bad.write_text("<gpx><trk><trkseg><trkpt lat='1' lon='2'>", encoding="utf-8")
    with pytest.raises(RouteError):
        load_route_gpx(bad, route_id="g", name="g")
    empty = tmp_path / "empty.gpx"
    empty.write_text(gpx_text([]), encoding="utf-8")
    with pytest.raises(RouteError):
        load_route_gpx(empty, route_id="g", name="g")
    single = tmp_path / "single.gpx"
    single.write_text(gpx_text([(1.0, 2.0, None)]), encoding="utf-8")
    with pytest.raises(RouteError):
        load_route_gpx(single, route_id="g", name="g")
    with pytest.raises(RouteError):
        load_route_gpx(tmp_path / "nope.gpx", route_id="g", name="g")


# --------------------------------------------------------------------------- annotation


@pytest.fixture
def plain_route(tmp_path: Path):
    f = write_json(tmp_path / "r.geojson", line_doc([lonlat(A), lonlat(B), lonlat(C)]))
    return load_route_geojson(f, route_id="r", name="r")


def test_annotate_inserts_control_stop_with_dwell(plain_route) -> None:
    mid, _ = point_at_distance(plain_route, 15_000.0)
    reference = destination_point(mid, 90.0, 300.0)  # 300 m off the road
    stop = NamedStop(
        name="Midtown", point=reference, mandatory_dwell_s=1800.0, official_distance_m=15_100.0
    )
    out = annotate_control_stops(plain_route, [stop])
    assert len(out.waypoints) == len(plain_route.waypoints) + 1
    assert out.total_distance_m == plain_route.total_distance_m
    (cs,) = out.control_stops
    assert cs.name == "Midtown" and cs.mandatory_dwell_s == 1800.0
    assert cs.cumulative_distance_m == pytest.approx(15_000.0, abs=0.01)
    assert "snapped 300 m" in (cs.notes or "")
    assert "published km 15.1" in (cs.notes or "")
    cum = [w.cumulative_distance_m for w in out.waypoints]
    assert cum == sorted(cum)
    # the new vertex lies on the road: projecting it back gives ~0 cross-track
    assert abs(project_onto_route(out, cs.point).cross_track_m) < 0.01
    assert "Annotated 1 named stops" in out.source_description


def test_annotate_relabels_near_vertex_instead_of_inserting(plain_route) -> None:
    near_b = destination_point(B, 270.0, 20.0)
    out = annotate_control_stops(
        plain_route, [NamedStop(name="Corner", point=near_b, mandatory_dwell_s=60.0)]
    )
    assert len(out.waypoints) == len(plain_route.waypoints)
    assert out.waypoints[1].kind is WaypointKind.CONTROL_STOP
    assert out.waypoints[1].point == plain_route.waypoints[1].point


def test_annotate_far_stop_raises_or_is_skipped(plain_route) -> None:
    far = destination_point(A, 270.0, 50_000.0)
    stop = NamedStop(name="Nowhere", point=far)
    with pytest.raises(RouteError, match="Nowhere"):
        annotate_control_stops(plain_route, [stop], max_snap_m=2_000.0)
    out = annotate_control_stops(plain_route, [stop], max_snap_m=2_000.0, strict=False)
    assert out.control_stops == []
    assert "Skipped" in out.source_description


def test_annotate_two_stops_on_same_vertex_raises(plain_route) -> None:
    s1 = NamedStop(name="One", point=destination_point(B, 270.0, 10.0))
    s2 = NamedStop(name="Two", point=destination_point(B, 270.0, 30.0))
    with pytest.raises(RouteError, match="same vertex"):
        annotate_control_stops(plain_route, [s1, s2])


def test_read_named_stops_geojson(tmp_path: Path) -> None:
    doc = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {}, "geometry": line_doc([lonlat(A), lonlat(B)])},
            {
                "type": "Feature",
                "properties": {"rwi_kind": "control_stop", "name": "CS1", "official_km_2025": 12.5},
                "geometry": {"type": "Point", "coordinates": lonlat(A)},
            },
            {
                "type": "Feature",
                "properties": {"name": "ignored"},
                "geometry": {"type": "Point", "coordinates": lonlat(B)},
            },
        ],
    }
    stops = read_named_stops_geojson(write_json(tmp_path / "s.geojson", doc), default_dwell_s=900.0)
    assert len(stops) == 1
    assert stops[0].mandatory_dwell_s == 900.0
    assert stops[0].official_distance_m == 12_500.0
