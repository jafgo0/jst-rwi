"""Enrich a Route with elevation from the team's own distance table.

JU Solar Team's drive-system repo contains ``wsc_race_data.csv``, a 3,030 km
polyline of the Darwin -> Adelaide route sampled every ~15 m with columns
``Distance (km), Latitude, Longitude, Bearing, Elevation (m)``. Our own route
has 3,532 vertices and no elevation; the team's has 204,878 and does. This
module reads theirs and attaches elevation to ours, by projecting each of our
waypoints onto their polyline and copying the elevation of the nearest point.

Why not just use their route instead of ours? Our route carries the official
2025 control-stop annotations and the race's waypoint ownership; theirs is a
pure geometric distance table. Attaching their elevations to our waypoints
keeps the semantics (our control stops, our provenance) and gains the
measurement they already have.

The file lives in the TEAM's repository, not here. The loader takes a path -
the team or operator points it at their local checkout, or an exported copy.
We never bundle their data; the attribution and licensing stay with them.
"""

from __future__ import annotations

import csv
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from rwi.contracts import Route, RouteWaypoint
from rwi.core.errors import RouteError
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import utc_now

log = logging.getLogger(__name__)

#: Each of our waypoints is attached to the nearest row in the team's table.
#: A match further than this is suspicious - either a route disagreement or a
#: wildly mis-georeferenced file - and refused loudly rather than silently
#: pulling the wrong elevation. 2 km corresponds to half a satellite tile and
#: is well above any routing-approximation disagreement we expect.
DEFAULT_MAX_MATCH_M = 2000.0


@dataclass(frozen=True)
class ElevationRow:
    """One row of the team's wsc_race_data.csv."""

    distance_km: float
    latitude_deg: float
    longitude_deg: float
    bearing_deg: float
    elevation_m: float


def read_team_elevation_csv(path: Path) -> list[ElevationRow]:
    """Read the team's wsc_race_data.csv into memory as a list.

    The file is 18 MB; keeping it in memory is cheaper than re-reading it per
    waypoint. The reader is strict about the header: a column rename on the
    team's side should fail loudly here, not silently produce zero-elevation
    rows.
    """
    if not path.is_file():
        raise RouteError(f"team elevation CSV not found at {path}")
    rows: list[ElevationRow] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        expected = {"Distance", "Latitude", "Longitude", "Bearing", "Elevation (m)"}
        missing = expected - set(reader.fieldnames or ())
        if missing:
            raise RouteError(
                f"{path.name}: missing expected column(s) {sorted(missing)}; "
                "team may have changed the schema - do not guess at a mapping"
            )
        for raw in reader:
            try:
                rows.append(
                    ElevationRow(
                        distance_km=float(raw["Distance"]),
                        latitude_deg=float(raw["Latitude"]),
                        longitude_deg=float(raw["Longitude"]),
                        bearing_deg=float(raw["Bearing"]),
                        elevation_m=float(raw["Elevation (m)"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
    if not rows:
        raise RouteError(f"{path.name}: no decodable rows")
    return rows


class _NearestIndex:
    """KD-tree spatial index over the team's elevation table.

    Linear scan was 23 s per route; a KD-tree is ~30x faster and makes the
    enrichment cheap enough to run on every test import. The underlying
    geometry is a flat-earth projection around the Australian corridor, which
    is well within tolerance for the few-km match we are checking.
    """

    __slots__ = ("_cos_lat0", "_lat0", "_tree", "rows")

    def __init__(self, rows: list[ElevationRow]) -> None:
        from scipy.spatial import KDTree

        self.rows = rows
        # Reference latitude for the local projection: the median of the route
        # table. Any point in the corridor is within a few degrees of it.
        self._lat0 = rows[len(rows) // 2].latitude_deg
        self._cos_lat0 = math.cos(math.radians(self._lat0))
        points = [[row.latitude_deg, row.longitude_deg * self._cos_lat0] for row in rows]
        self._tree = KDTree(points)

    def nearest(self, target_lat: float, target_lon: float) -> tuple[ElevationRow, float]:
        d_deg, idx = self._tree.query([target_lat, target_lon * self._cos_lat0])
        row = self.rows[int(idx)]
        distance_m = float(d_deg) * math.radians(1.0) * 6_371_000.0
        return row, distance_m


def _nearest(
    rows: list[ElevationRow], target_lat: float, target_lon: float
) -> tuple[ElevationRow, float]:
    """Linear-scan nearest neighbour. Kept for small inputs and testing.

    For enrichment at scale use :class:`_NearestIndex`.
    """
    cos_lat = math.cos(math.radians(target_lat))
    best_row = rows[0]
    best_d2 = math.inf
    for row in rows:
        dlat = row.latitude_deg - target_lat
        dlon = (row.longitude_deg - target_lon) * cos_lat
        d2 = dlat * dlat + dlon * dlon
        if d2 < best_d2:
            best_d2 = d2
            best_row = row
    distance_m = math.sqrt(best_d2) * math.radians(1.0) * 6_371_000.0
    return best_row, distance_m


def enrich_with_team_elevation(
    route: Route,
    team_csv: Path,
    *,
    max_match_m: float = DEFAULT_MAX_MATCH_M,
) -> Route:
    """Return a new Route whose waypoints carry elevations from the team's CSV.

    Waypoints whose nearest neighbour is further than ``max_match_m`` are
    REFUSED - not silently copied with a bad elevation. A nearest-neighbour
    distance that large means our polyline disagrees with the team's in a way
    we must look at, not paper over.
    """
    rows = read_team_elevation_csv(team_csv)
    index = _NearestIndex(rows)
    enriched: list[RouteWaypoint] = []
    worst_match_m = 0.0
    sum_match_m = 0.0
    for wp in route.waypoints:
        match, distance_m = index.nearest(wp.point.latitude_deg, wp.point.longitude_deg)
        if distance_m > max_match_m:
            raise RouteError(
                f"waypoint at {wp.cumulative_distance_m / 1000:.1f} km has no match "
                f"within {max_match_m:.0f} m in {team_csv.name} (nearest is "
                f"{distance_m:.0f} m away). Routes disagree - investigate before "
                "attaching elevations."
            )
        worst_match_m = max(worst_match_m, distance_m)
        sum_match_m += distance_m
        enriched.append(
            wp.model_copy(
                update={
                    "point": GeoPoint(
                        latitude_deg=wp.point.latitude_deg,
                        longitude_deg=wp.point.longitude_deg,
                        elevation_m=match.elevation_m,
                    )
                }
            )
        )

    mean_match_m = sum_match_m / len(route.waypoints)
    log.info(
        "enriched %d waypoints from %s: mean match %.0f m, worst %.0f m",
        len(route.waypoints), team_csv.name, mean_match_m, worst_match_m,
    )
    note = (
        f"Elevation attached from JU Solar Team's wsc_race_data.csv "
        f"(nearest-neighbour match, mean {mean_match_m:.0f} m, worst "
        f"{worst_match_m:.0f} m, limit {max_match_m:.0f} m)."
    )
    source = (
        f"{route.source_description} {note}".strip()
        if route.source_description
        else note
    )
    provenance = route.provenance.model_copy(
        update={
            "retrieved_at_utc": utc_now(),
            "kind": DataKind.DERIVED,
            "notes": _append_note(route.provenance.notes, note),
        }
    )
    return route.model_copy(
        update={
            "waypoints": enriched,
            "source_description": source,
            "provenance": provenance,
        }
    )


def _append_note(existing: str | None, extra: str) -> str:
    if not existing:
        return extra
    return f"{existing} | {extra}"


# Keep the import above happy for mypy without a top-level pydantic import.
_ = Provenance
