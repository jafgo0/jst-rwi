"""Shared API dependencies: settings, state, route repository, providers."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Query, status

from rwi.api.capability import require_attr
from rwi.config import Settings, get_settings
from rwi.contracts import Route
from rwi.core.errors import RouteError
from rwi.core.geo import GeoPoint
from rwi.core.state import StateStore, get_state_store
from rwi.core.timebase import ensure_utc, utc_now

__all__ = [
    "RouteRepository",
    "get_route_repository",
    "get_settings",
    "get_state_store",
    "resolve_point",
    "resolve_time",
]


def resolve_point(
    latitude_deg: float | None,
    longitude_deg: float | None,
    elevation_m: float | None,
    store: StateStore,
) -> GeoPoint:
    """Explicit coordinates win; otherwise the active fix. Never a default location."""
    if latitude_deg is not None and longitude_deg is not None:
        return GeoPoint(
            latitude_deg=latitude_deg, longitude_deg=longitude_deg, elevation_m=elevation_m
        )
    if latitude_deg is not None or longitude_deg is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="supply both lat and lon, or neither to use the active position",
        )
    return store.require_fix().point


def resolve_time(when: datetime | None, store: StateStore) -> datetime:
    """Explicit time wins; otherwise the active state's effective time."""
    if when is not None:
        return ensure_utc(when)
    return store.state.effective_time(utc_now())


# No default here: FastAPI requires the default on the parameter itself when the
# Query lives inside Annotated.
LatQuery = Query(ge=-90.0, le=90.0, description="degrees north, WGS84")
LonQuery = Query(ge=-180.0, le=180.0, description="degrees east, WGS84")


class RouteRepository:
    """Loads route files from disk once, through the route module.

    When ``team_elevation_csv`` is set (via Settings), every loaded route is
    enriched with the team's elevation data. Enrichment failures (file missing,
    schema changed, a waypoint beyond the match tolerance) are logged and the
    original un-enriched route is served - a bad path to the team CSV must not
    stop the dashboard from loading, but it must not silently claim elevation
    either.
    """

    def __init__(
        self,
        directory: Path,
        *,
        team_elevation_csv: Path | None = None,
        team_elevation_max_match_m: float = 3000.0,
    ) -> None:
        self.directory = directory
        self._team_elevation_csv = team_elevation_csv
        self._team_elevation_max_match_m = team_elevation_max_match_m
        self._routes: dict[str, Route] | None = None
        self._elevation_status: str = "not attempted"

    def _load(self) -> dict[str, Route]:
        """Load every route file, preferring the BWSC loader where it applies.

        ``rwi.route.bwsc.load_bwsc_route`` promotes the published control stops
        to waypoints with their mandatory dwell; the plain GeoJSON loader does
        not, and a route without control stops would silently drop the race's
        most important timing constraint.
        """
        loader: Any = require_attr(
            "rwi.route.loader", "load_route_geojson", capability="route loading"
        )
        bwsc_loader: Any | None = None
        try:
            bwsc_loader = require_attr(
                "rwi.route.bwsc", "load_bwsc_route", capability="BWSC route"
            )
        except Exception:
            bwsc_loader = None

        routes: dict[str, Route] = {}
        if not self.directory.is_dir():
            return routes
        for path in sorted(self.directory.glob("*.geojson")):
            route_id = path.stem
            try:
                if bwsc_loader is not None and route_id.startswith("bwsc-"):
                    routes[route_id] = bwsc_loader(path, route_id=route_id)
                else:
                    routes[route_id] = loader(
                        path, route_id=route_id, name=route_id.replace("-", " ").title()
                    )
            except (RouteError, OSError, ValueError):
                continue
        return self._enrich_all(routes)

    def _enrich_all(self, routes: dict[str, Route]) -> dict[str, Route]:
        """Attach team elevation to every loaded route, honestly.

        A missing path means the operator hasn't configured one and we leave
        routes as they are. A broken enrichment means the file was configured
        and is bad - that is worth a log line and a status, but it must not
        stop the dashboard. The un-enriched route is served and
        ``_elevation_status`` records what happened.
        """
        if self._team_elevation_csv is None:
            self._elevation_status = "not configured"
            return routes
        try:
            from rwi.route.elevation import enrich_with_team_elevation
        except ImportError as exc:
            self._elevation_status = f"import failed: {exc}"
            return routes
        enriched: dict[str, Route] = {}
        failures: list[str] = []
        for route_id, route in routes.items():
            try:
                enriched[route_id] = enrich_with_team_elevation(
                    route,
                    self._team_elevation_csv,
                    max_match_m=self._team_elevation_max_match_m,
                )
            except (RouteError, OSError, ValueError) as exc:
                failures.append(f"{route_id}: {type(exc).__name__}: {exc}")
                enriched[route_id] = route
        if failures:
            okays = len(routes) - len(failures)
            self._elevation_status = (
                f"partial: {okays}/{len(routes)} enriched; " + "; ".join(failures)
            )
        else:
            self._elevation_status = f"ok: {len(enriched)}/{len(routes)} enriched"
        return enriched

    @property
    def elevation_status(self) -> str:
        """Human-readable status of the last enrichment attempt."""
        if self._routes is None:
            self.all()  # trigger lazy load
        return self._elevation_status

    def all(self) -> dict[str, Route]:
        if self._routes is None:
            self._routes = self._load()
        return self._routes

    def get(self, route_id: str) -> Route:
        try:
            return self.all()[route_id]
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown route {route_id!r}"
            ) from exc

    def reload(self) -> None:
        self._routes = None


@lru_cache(maxsize=1)
def get_route_repository() -> RouteRepository:
    settings: Settings = get_settings()
    base = Path("data/routes")
    return RouteRepository(
        base,
        team_elevation_csv=settings.team_elevation_csv,
        team_elevation_max_match_m=settings.team_elevation_max_match_m,
    )
