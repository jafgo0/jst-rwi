"""Route loading, validation and control-stop annotation.

A route file is a WGS84 polyline (GeoJSON ``[lon, lat(, elevation_m)]`` or GPX).
Cumulative distances are computed here with :func:`rwi.core.geo.geodesic_distance_m`
(WGS84 geodesic), not read from the file. The loader never invents data:
unknown elevation stays ``None``, and provenance/accuracy are taken from the
file's own ``rwi_provenance`` member when present, otherwise declared unknown.

GeoJSON provenance member (optional, top level of a FeatureCollection)::

    "rwi_provenance": {"provider", "dataset", "license", "source_url",
                       "retrieved_at_utc", "accuracy", "description", "notes",
                       "is_official_race_route"}
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from rwi.contracts import Route, RouteWaypoint, WaypointKind
from rwi.core.errors import RouteError
from rwi.core.geo import GeoPoint, geodesic_distance_m
from rwi.core.provenance import DataKind, Provenance
from rwi.route.geometry import point_at_distance, project_onto_route

__all__ = [
    "DEFAULT_MAX_JUMP_M",
    "NamedStop",
    "annotate_control_stops",
    "load_route_geojson",
    "load_route_gpx",
    "read_named_stops_geojson",
]

DEFAULT_MAX_JUMP_M = 100_000.0
"""Default largest accepted point-to-point distance. Real GPS tracks and simplified
road polylines have jumps of metres to a few tens of km; 100 km is a data error
unless the file is a coarse waypoint route (then raise ``max_jump_m``)."""

_ELEVATION_MIN_M, _ELEVATION_MAX_M = -500.0, 9000.0  # GeoPoint's own bounds


class NamedStop(BaseModel):
    """A named place to promote to a waypoint on a route (e.g. a BWSC control stop)."""

    model_config = ConfigDict(frozen=True)

    name: str
    point: GeoPoint = Field(description="reference coordinate; need not lie exactly on the route")
    kind: WaypointKind = WaypointKind.CONTROL_STOP
    mandatory_dwell_s: float | None = Field(default=None, ge=0.0)
    official_distance_m: float | None = Field(
        default=None, ge=0.0, description="published cumulative distance, kept for comparison only"
    )
    notes: str | None = None


# ---------------------------------------------------------------------------
# Coordinate parsing and validation
# ---------------------------------------------------------------------------


def _make_point(lat: Any, lon: Any, ele: Any, where: str) -> GeoPoint:
    for label, v in (("latitude", lat), ("longitude", lon)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise RouteError(f"{where}: {label} is not a finite number: {v!r}")
    if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
        raise RouteError(f"{where}: coordinate out of range (lat={lat}, lon={lon})")
    elevation: float | None = None
    if ele is not None:
        if isinstance(ele, bool) or not isinstance(ele, (int, float)) or not math.isfinite(ele):
            raise RouteError(f"{where}: elevation is not a finite number: {ele!r}")
        if not _ELEVATION_MIN_M <= ele <= _ELEVATION_MAX_M:
            raise RouteError(f"{where}: implausible elevation {ele} m")
        elevation = float(ele)
    return GeoPoint(latitude_deg=float(lat), longitude_deg=float(lon), elevation_m=elevation)


def _position(coord: Any, where: str) -> GeoPoint:
    if not isinstance(coord, (list, tuple)) or len(coord) < 2:
        raise RouteError(f"{where}: a position needs [lon, lat(, elevation)], got {coord!r}")
    ele = coord[2] if len(coord) >= 3 else None
    return _make_point(coord[1], coord[0], ele, where)


def _build_waypoints(
    points: Sequence[GeoPoint], max_jump_m: float
) -> tuple[list[RouteWaypoint], int]:
    """Drop consecutive duplicates, validate jumps, compute geodesic cumulative distances."""
    if max_jump_m <= 0.0 or not math.isfinite(max_jump_m):
        raise RouteError(f"max_jump_m must be positive and finite, got {max_jump_m!r}")
    kept: list[GeoPoint] = []
    dropped = 0
    for p in points:
        if kept and (
            p.latitude_deg == kept[-1].latitude_deg and p.longitude_deg == kept[-1].longitude_deg
        ):
            dropped += 1
            continue
        kept.append(p)
    if len(kept) < 2:
        raise RouteError(
            f"route geometry needs at least two distinct points, found {len(kept)} "
            f"({len(points)} supplied)"
        )
    waypoints: list[RouteWaypoint] = []
    cumulative = 0.0
    for i, p in enumerate(kept):
        if i > 0:
            jump = geodesic_distance_m(kept[i - 1], p)
            if jump > max_jump_m:
                raise RouteError(
                    f"implausible jump of {jump / 1000:.1f} km between points {i - 1} and {i} "
                    f"(limit {max_jump_m / 1000:.1f} km); raise max_jump_m if the file is a "
                    "coarse waypoint route"
                )
            cumulative += jump
        kind = (
            WaypointKind.START
            if i == 0
            else WaypointKind.FINISH
            if i == len(kept) - 1
            else WaypointKind.WAYPOINT
        )
        waypoints.append(RouteWaypoint(point=p, cumulative_distance_m=cumulative, kind=kind))
    return waypoints, dropped


def _provenance_and_description(
    path: Path,
    fmt: str,
    meta: dict[str, Any],
    n_points: int,
    total_m: float,
    has_elev: bool,
    notes: list[str],
) -> tuple[Provenance, str]:
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        mtime = datetime.now(tz=UTC)
    retrieved = mtime
    raw_time = meta.get("retrieved_at_utc")
    if isinstance(raw_time, str):
        try:
            retrieved = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            if retrieved.tzinfo is None:
                retrieved = retrieved.replace(tzinfo=UTC)
        except ValueError:
            retrieved = mtime
    provider = str(meta.get("provider") or "unknown (no provenance recorded in file)")
    dataset = str(meta.get("dataset") or f"{fmt} file {path.name}")
    accuracy = str(
        meta.get("accuracy") or "not stated in the file; horizontal accuracy unknown"
    ).rstrip(".")
    licence = meta.get("license")
    official = meta.get("is_official_race_route")
    parts = [
        f"Loaded from {fmt} file '{path.name}': {n_points} vertices, "
        f"{total_m / 1000:.3f} km (WGS84 geodesic sum of segments).",
        f"Source: {provider}; dataset: {dataset}.",
        f"Accuracy: {accuracy}.",
        "Elevation: "
        + (
            "present in file (source accuracy not assessed)."
            if has_elev
            else "NOT available - unknown, not zero."
        ),
    ]
    if official is False:
        parts.append("This geometry is NOT the official race route.")
    elif official is None:
        parts.append("Whether this is the official race route is not stated in the file.")
    if meta.get("description"):
        parts.append(str(meta["description"]))
    parts.extend(notes)
    prov_notes = " ".join(parts)
    provenance = Provenance(
        provider=provider,
        dataset=dataset,
        kind=DataKind.DERIVED,
        retrieved_at_utc=retrieved,
        processing=f"{fmt} polyline, geodesic cumulative distance (rwi.route.loader)",
        license=str(licence) if licence else None,
        source_url=str(meta["source_url"]) if meta.get("source_url") else None,
        notes=prov_notes,
    )
    return provenance, prov_notes


# ---------------------------------------------------------------------------
# GeoJSON
# ---------------------------------------------------------------------------


def _line_coordinates(geometry: Any, where: str) -> list[list[Any]]:
    if not isinstance(geometry, dict):
        raise RouteError(f"{where}: geometry missing or not an object")
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "LineString":
        if not isinstance(coords, list):
            raise RouteError(f"{where}: LineString without coordinates")
        return [coords]
    if gtype == "MultiLineString":
        if not isinstance(coords, list) or not all(isinstance(c, list) for c in coords):
            raise RouteError(f"{where}: malformed MultiLineString")
        return list(coords)
    raise RouteError(
        f"{where}: unsupported geometry type {gtype!r} (need LineString/MultiLineString)"
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RouteError(f"cannot read GeoJSON '{path}': {exc}") from exc


def load_route_geojson(
    path: Path,
    *,
    route_id: str,
    name: str,
    max_jump_m: float = DEFAULT_MAX_JUMP_M,
) -> Route:
    """Load a route from a GeoJSON LineString, MultiLineString or FeatureCollection.

    A FeatureCollection must contain exactly one LineString/MultiLineString
    feature (Point features are ignored here; see :func:`read_named_stops_geojson`).
    Coordinates are ``[lon, lat]`` or ``[lon, lat, elevation_m]`` in WGS84.
    MultiLineString parts are joined in order with straight geodesic links and the
    total linked gap is reported in ``source_description``.

    Cumulative distances are the sum of WGS84 geodesic segment lengths (metres).
    The first and last vertices become START and FINISH waypoints.

    Args:
        max_jump_m: largest accepted distance between consecutive points.

    Raises:
        RouteError: unreadable/invalid JSON, wrong geometry type, empty or
            single-point geometry, non-finite or out-of-range coordinates,
            implausible elevation, or a jump larger than ``max_jump_m``.
    """
    path = Path(path)
    doc = _read_json(path)
    if not isinstance(doc, dict):
        raise RouteError(f"'{path.name}': top-level GeoJSON value must be an object")
    meta: dict[str, Any] = {}
    gtype = doc.get("type")
    parts: list[list[Any]]
    if gtype == "FeatureCollection":
        raw_meta = doc.get("rwi_provenance")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        lines = [
            f
            for f in doc.get("features", [])
            if isinstance(f, dict)
            and isinstance(f.get("geometry"), dict)
            and f["geometry"].get("type") in ("LineString", "MultiLineString")
        ]
        if len(lines) != 1:
            raise RouteError(
                f"'{path.name}': expected exactly one LineString/MultiLineString feature, "
                f"found {len(lines)}"
            )
        parts = _line_coordinates(lines[0]["geometry"], f"'{path.name}' feature")
    elif gtype == "Feature":
        raw_meta = doc.get("rwi_provenance")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        parts = _line_coordinates(doc.get("geometry"), f"'{path.name}'")
    elif gtype in ("LineString", "MultiLineString"):
        parts = _line_coordinates(doc, f"'{path.name}'")
    else:
        raise RouteError(f"'{path.name}': not a GeoJSON line (type={gtype!r})")

    points: list[GeoPoint] = []
    gap_total = 0.0
    gaps = 0
    for pi, part in enumerate(parts):
        part_pts = [
            _position(c, f"'{path.name}' part {pi} vertex {vi}") for vi, c in enumerate(part)
        ]
        if points and part_pts:
            link = geodesic_distance_m(points[-1], part_pts[0])
            if link > 1.0:
                gaps += 1
                gap_total += link
        points.extend(part_pts)
    waypoints, dropped = _build_waypoints(points, max_jump_m)
    notes: list[str] = []
    if len(parts) > 1:
        notes.append(
            f"MultiLineString of {len(parts)} parts joined by straight links "
            f"({gaps} gaps over 1 m, {gap_total:.0f} m in total)."
        )
    if dropped:
        notes.append(f"{dropped} consecutive duplicate vertices removed.")
    total = waypoints[-1].cumulative_distance_m
    provenance, desc = _provenance_and_description(
        path,
        "GeoJSON",
        meta,
        len(waypoints),
        total,
        all(w.point.elevation_m is not None for w in waypoints),
        notes,
    )
    return Route(
        id=route_id,
        name=name,
        waypoints=waypoints,
        total_distance_m=total,
        provenance=provenance,
        source_description=desc,
    )


def read_named_stops_geojson(
    path: Path, *, default_dwell_s: float | None = None
) -> list[NamedStop]:
    """Read ``Point`` features with ``properties.rwi_kind`` of ``control_stop`` (or start/finish).

    Properties used: ``name``, ``official_km_2025`` (kilometres, optional),
    ``reference_source`` / ``notes``, ``mandatory_dwell_s`` (overrides
    ``default_dwell_s``). Features without ``rwi_kind`` are ignored.
    """
    path = Path(path)
    doc = _read_json(path)
    if not isinstance(doc, dict) or doc.get("type") != "FeatureCollection":
        return []
    kinds = {
        "control_stop": WaypointKind.CONTROL_STOP,
        "start": WaypointKind.START,
        "finish": WaypointKind.FINISH,
        "town": WaypointKind.TOWN,
    }
    stops: list[NamedStop] = []
    for f in doc.get("features", []):
        if not isinstance(f, dict):
            continue
        props = f.get("properties") or {}
        geom = f.get("geometry") or {}
        kind = kinds.get(props.get("rwi_kind"))
        if kind is None or geom.get("type") != "Point":
            continue
        pt = _position(geom.get("coordinates"), f"'{path.name}' stop {props.get('name')!r}")
        fallback = default_dwell_s if kind is WaypointKind.CONTROL_STOP else None
        dwell = props.get("mandatory_dwell_s", fallback)
        km = props.get("official_km_2025")
        stops.append(
            NamedStop(
                name=str(props.get("name") or "unnamed"),
                point=pt,
                kind=kind,
                mandatory_dwell_s=dwell,
                official_distance_m=float(km) * 1000.0 if km is not None else None,
                notes=props.get("reference_source") or props.get("notes"),
            )
        )
    return stops


# ---------------------------------------------------------------------------
# GPX
# ---------------------------------------------------------------------------


def load_route_gpx(
    path: Path,
    *,
    route_id: str,
    name: str,
    max_jump_m: float = DEFAULT_MAX_JUMP_M,
) -> Route:
    """Load a route from a GPX file (all track segments in order, else the first route).

    Track points (``trkpt``) are preferred; if the file has no tracks the route
    points (``rtept``) of its routes are used; bare waypoints (``wpt``) are not a
    route and are rejected. Elevation comes from ``<ele>`` when present.
    GPX carries no provenance block, so source and accuracy are recorded as
    unknown apart from the GPX ``metadata`` name/author/copyright when given.

    Raises:
        RouteError: file unreadable or not valid GPX, no usable points,
            non-finite/out-of-range coordinates, or a jump above ``max_jump_m``.
    """
    import gpxpy
    import gpxpy.gpx

    path = Path(path)
    try:
        gpx = gpxpy.parse(path.read_text(encoding="utf-8"))
    except Exception as exc:  # gpxpy raises several exception types for bad input
        raise RouteError(f"cannot parse GPX '{path}': {exc}") from exc

    raw: list[Any] = [p for trk in gpx.tracks for seg in trk.segments for p in seg.points]
    source = "track"
    if not raw:
        raw = [p for rte in gpx.routes for p in rte.points]
        source = "route"
    if not raw:
        raise RouteError(f"'{path.name}': GPX has no track or route points")
    points = [
        _make_point(p.latitude, p.longitude, p.elevation, f"'{path.name}' point {i}")
        for i, p in enumerate(raw)
    ]
    waypoints, dropped = _build_waypoints(points, max_jump_m)
    notes = [f"GPX {source} points used."]
    if dropped:
        notes.append(f"{dropped} consecutive duplicate points removed.")
    meta: dict[str, Any] = {}
    author = getattr(gpx, "author_name", None)
    if author or gpx.copyright_author or gpx.creator:
        meta["provider"] = str(author or gpx.copyright_author or gpx.creator)
    if gpx.name:
        meta["dataset"] = str(gpx.name)
    total = waypoints[-1].cumulative_distance_m
    provenance, desc = _provenance_and_description(
        path,
        "GPX",
        meta,
        len(waypoints),
        total,
        all(w.point.elevation_m is not None for w in waypoints),
        notes,
    )
    return Route(
        id=route_id,
        name=name,
        waypoints=waypoints,
        total_distance_m=total,
        provenance=provenance,
        source_description=desc,
    )


# ---------------------------------------------------------------------------
# Annotation
# ---------------------------------------------------------------------------


def annotate_control_stops(
    route: Route,
    stops: Sequence[NamedStop],
    *,
    max_snap_m: float = 2000.0,
    vertex_snap_m: float = 50.0,
    strict: bool = True,
) -> Route:
    """Promote points near named stops to typed waypoints (CONTROL_STOP by default).

    Each stop's reference coordinate is map-matched onto the route
    (:func:`rwi.route.geometry.project_onto_route`). If the foot point is within
    ``vertex_snap_m`` (along the route) of an existing vertex that vertex is
    re-labelled; otherwise a new vertex is inserted on the segment at the foot
    point. The stop's ``kind``, ``name`` and ``mandatory_dwell_s`` are applied and
    the snap distance (and the official published distance, if given) are written
    to ``RouteWaypoint.notes``. Total length and all other distances are
    unchanged. The reference coordinate itself is not on the road; the waypoint
    is the nearest point on the route.

    Args:
        max_snap_m: stops farther than this from the route are rejected.
        strict: when False such stops are skipped and recorded in
            ``source_description`` instead of raising.

    Raises:
        RouteError: a stop is farther than ``max_snap_m`` (strict), or two stops
            resolve to the same vertex.
    """
    # (distance_along, vertex index to relabel | None, stop, snap_m, foot)
    plan: list[tuple[float, int | None, NamedStop, float, GeoPoint]] = []
    skipped: list[str] = []
    cum = [w.cumulative_distance_m for w in route.waypoints]
    used_vertices: dict[int, str] = {}
    for stop in stops:
        proj = project_onto_route(route, stop.point, tolerance_m=max_snap_m)
        if not proj.is_on_route:
            msg = (
                f"stop '{stop.name}' is {abs(proj.cross_track_m):.0f} m from the route "
                f"(limit {max_snap_m:.0f} m)"
            )
            if strict:
                raise RouteError(msg)
            skipped.append(msg)
            continue
        d = proj.distance_along_m
        i = proj.segment_index
        vertex: int | None = None
        if d - cum[i] <= vertex_snap_m:
            vertex = i
        elif cum[i + 1] - d <= vertex_snap_m:
            vertex = i + 1
        if vertex is not None:
            if vertex in used_vertices:
                raise RouteError(
                    f"stops '{used_vertices[vertex]}' and '{stop.name}' resolve to the same vertex"
                )
            used_vertices[vertex] = stop.name
            vertex_point = route.waypoints[vertex].point
            plan.append((cum[vertex], vertex, stop, abs(proj.cross_track_m), vertex_point))
        else:
            foot, _ = point_at_distance(route, d)
            plan.append((d, None, stop, abs(proj.cross_track_m), foot))

    def note_for(stop: NamedStop, snap_m: float, at_m: float) -> str:
        text = f"snapped {snap_m:.0f} m from reference coordinate; polyline km {at_m / 1000:.1f}"
        if stop.official_distance_m is not None:
            diff = at_m - stop.official_distance_m
            text += (
                f"; published km {stop.official_distance_m / 1000:.1f} "
                f"(polyline minus published {diff / 1000:+.1f} km)"
            )
        if stop.notes:
            text += f"; reference: {stop.notes}"
        return text

    relabel = {v: (s, snap, at) for at, v, s, snap, _ in plan if v is not None}
    inserts = [(at, s, snap, foot) for at, v, s, snap, foot in plan if v is None]
    new: list[RouteWaypoint] = []
    for k, w in enumerate(route.waypoints):
        if k in relabel:
            s, snap, at = relabel[k]
            new.append(
                w.model_copy(
                    update={
                        "kind": s.kind,
                        "name": s.name,
                        "mandatory_dwell_s": s.mandatory_dwell_s,
                        "notes": note_for(s, snap, at),
                    }
                )
            )
        else:
            new.append(w)
    for at, s, snap, foot in inserts:
        new.append(
            RouteWaypoint(
                point=foot,
                cumulative_distance_m=at,
                kind=s.kind,
                name=s.name,
                mandatory_dwell_s=s.mandatory_dwell_s,
                notes=note_for(s, snap, at),
            )
        )
    new.sort(key=lambda w: w.cumulative_distance_m)  # stable: relabelled vertices keep order
    n_cs = sum(1 for _, _, s, _, _ in plan if s.kind is WaypointKind.CONTROL_STOP)
    desc = route.source_description + (
        f" Annotated {len(plan)} named stops ({n_cs} control stops), each snapped to the nearest "
        f"point on this polyline (max snap {max((p[3] for p in plan), default=0.0):.0f} m)."
    )
    if skipped:
        desc += " Skipped: " + "; ".join(skipped) + "."
    return Route(
        id=route.id,
        name=route.name,
        waypoints=new,
        total_distance_m=route.total_distance_m,
        provenance=route.provenance,
        source_description=desc,
    )
