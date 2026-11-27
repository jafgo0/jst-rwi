"""FIRMS hotspots near the route and the smoke/fire alert rule (ADR 0014).

Each hotspot is projected onto the route polyline (`project_onto_route`) and kept
when its perpendicular distance is at most the configured buffer. The alert rule
then keeps hotspots within ``radius_km`` of the route, at or above a confidence
class and a fire radiative power, and groups them into clusters along the route:
two kept hotspots belong to one cluster when their along-route positions are at
most ``radius_km`` apart. One WATCH alert per cluster.

There is no FIRE alert category in the contract; the alert uses VISIBILITY,
because smoke over the road is the race hazard a hotspot signals. A hotspot is a
thermal anomaly seen from orbit hours ago, not a fire front or a road closure:
the alert asks the team to check, it does not say the road is blocked.
"""

import math
from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from rwi.alerts.rules import _Builder
from rwi.contracts import Alert, AlertCategory, AlertSeverity, Route
from rwi.providers.firms import CONFIDENCE_RANK, FireHotspot
from rwi.route.geometry import project_onto_route

__all__ = ["RouteFire", "fire_alerts", "fires_near_route", "route_bbox_margin_deg"]

_KM_PER_DEG_LAT = 111.32


class RouteFire(BaseModel):
    """A hotspot with its place relative to the route."""

    model_config = ConfigDict(frozen=True)

    hotspot: FireHotspot
    route_id: str
    distance_along_m: float
    offset_m: float
    """Perpendicular distance from the route [m], unsigned."""


def route_bbox_margin_deg(route: Route, buffer_km: float) -> float:
    """Bbox margin [deg] that contains every point within ``buffer_km`` of the route."""
    if not math.isfinite(buffer_km) or buffer_km <= 0.0:
        raise ValueError(f"buffer_km must be positive, got {buffer_km!r}")
    max_lat = max(abs(w.point.latitude_deg) for w in route.waypoints)
    # Longitude degrees shrink with cos(lat); the widest margin is at the highest |lat|.
    return buffer_km / (_KM_PER_DEG_LAT * math.cos(math.radians(min(max_lat, 89.0))))


def fires_near_route(
    route: Route, hotspots: Sequence[FireHotspot], buffer_km: float
) -> list[RouteFire]:
    """Hotspots within ``buffer_km`` of the route, ordered along it."""
    if not math.isfinite(buffer_km) or buffer_km <= 0.0:
        raise ValueError(f"buffer_km must be positive, got {buffer_km!r}")
    out = []
    for h in hotspots:
        p = project_onto_route(route, h.point, tolerance_m=buffer_km * 1000.0)
        if p.is_on_route:
            out.append(
                RouteFire(
                    hotspot=h,
                    route_id=route.id,
                    distance_along_m=p.distance_along_m,
                    offset_m=abs(p.cross_track_m),
                )
            )
    return sorted(out, key=lambda f: f.distance_along_m)


def _check(radius_km: float, min_confidence: str, min_frp_mw: float) -> None:
    if not math.isfinite(radius_km) or radius_km <= 0.0:
        raise ValueError(f"fire_alert_radius_km must be positive, got {radius_km!r}")
    if min_confidence not in CONFIDENCE_RANK:
        raise ValueError(
            f"fire_alert_min_confidence must be one of {sorted(CONFIDENCE_RANK)}, "
            f"got {min_confidence!r}"
        )
    if not math.isfinite(min_frp_mw) or min_frp_mw < 0.0:
        raise ValueError(f"fire_alert_min_frp_mw must be >= 0, got {min_frp_mw!r}")


def fire_alerts(
    fires: Sequence[RouteFire],
    *,
    radius_km: float | None,
    min_confidence: str | None,
    min_frp_mw: float | None,
    now: datetime,
) -> list[Alert]:
    """One WATCH per along-route cluster of qualifying hotspots; [] when unconfigured."""
    if radius_km is None or min_confidence is None or min_frp_mw is None:
        return []
    _check(radius_km, min_confidence, min_frp_mw)
    rank = CONFIDENCE_RANK[min_confidence]
    radius_m = radius_km * 1000.0
    kept = sorted(
        (
            f
            for f in fires
            if f.offset_m <= radius_m
            and CONFIDENCE_RANK[f.hotspot.confidence] >= rank
            and f.hotspot.frp_mw is not None
            and f.hotspot.frp_mw >= min_frp_mw
        ),
        key=lambda f: f.distance_along_m,
    )
    clusters: list[list[RouteFire]] = []
    for f in kept:
        if clusters and f.distance_along_m - clusters[-1][-1].distance_along_m <= radius_m:
            clusters[-1].append(f)
        else:
            clusters.append([f])

    b = _Builder(now=now)
    for c in clusters:
        nearest = min(c, key=lambda f: f.offset_m)
        start_km = c[0].distance_along_m / 1000.0
        end_km = c[-1].distance_along_m / 1000.0
        latest = max(c, key=lambda f: f.hotspot.acquired_utc).hotspot
        frp = max(f.hotspot.frp_mw or 0.0 for f in c)
        span = (
            f"{start_km:.0f} km" if end_km - start_km < 1.0 else f"{start_km:.0f}-{end_km:.0f} km"
        )
        b.add(
            alert_id=f"fire-{c[0].route_id}-{start_km:.0f}",
            severity=AlertSeverity.WATCH,
            category=AlertCategory.VISIBILITY,
            headline=f"{len(c)} satellite fire hotspot(s) near the route at {span}",
            detail=(
                f"NASA FIRMS detected {len(c)} hotspot(s) within {radius_km:g} km of the route "
                f"at {span}; nearest {nearest.offset_m / 1000.0:.1f} km off the road. Smoke may "
                "reduce visibility and irradiance. Hotspots are unvalidated near-real-time "
                "detections, not fire perimeters or road closures."
            ),
            evidence=[
                f"hotspots: {len(c)} (confidence >= {min_confidence}, FRP >= {min_frp_mw:g} MW)",
                f"nearest offset: {nearest.offset_m / 1000.0:.1f} km",
                f"max FRP: {frp:.1f} MW",
                f"latest detection: {latest.acquired_utc.isoformat()} ({latest.satellite})",
            ],
            recommended_action=(
                "Check NT/SA fire services and road reports for this section; "
                "the platform recommends, humans decide."
            ),
            distance_along_route_m=nearest.distance_along_m,
            location=nearest.hotspot.point,
        )
    return b.items
