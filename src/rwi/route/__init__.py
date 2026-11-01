"""Route geometry, map matching and route-aware trajectory (ETA) building.

See docs/science/route.md for sources, models, assumptions and validation status.
"""

from rwi.route.geometry import (
    elevation_at_distance,
    elevation_profile,
    grade_at_distance,
    has_elevation,
    point_at_distance,
    project_onto_route,
)
from rwi.route.loader import (
    NamedStop,
    annotate_control_stops,
    load_route_geojson,
    load_route_gpx,
    read_named_stops_geojson,
)
from rwi.route.trajectory import build_trajectory

__all__ = [
    "NamedStop",
    "annotate_control_stops",
    "build_trajectory",
    "elevation_at_distance",
    "elevation_profile",
    "grade_at_distance",
    "has_elevation",
    "load_route_geojson",
    "load_route_gpx",
    "point_at_distance",
    "project_onto_route",
    "read_named_stops_geojson",
]
