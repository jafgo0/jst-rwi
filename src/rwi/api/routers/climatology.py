"""Race-week climatology per route segment for pre-race planning (ADR 0016).

INTERNAL tier: no prefix in `rwi.api.product` matches ``/api/climatology``. The
route only serves the file ``scripts/build_climatology.py`` wrote; it never
fetches or fabricates data on request.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import ValidationError

from rwi.api.deps import RouteRepository, get_route_repository
from rwi.climatology.build import climatology_path
from rwi.climatology.raceweek import RaceWeekClimatology
from rwi.config import Settings, get_settings

router = APIRouter(prefix="/api/climatology", tags=["climatology"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
RepoDep = Annotated[RouteRepository, Depends(get_route_repository)]


@router.get("/race-week", response_model=RaceWeekClimatology)
def race_week(
    settings: SettingsDep,
    repo: RepoDep,
    route_id: Annotated[str, Query(description="route id, e.g. bwsc-2027-stuart-highway")],
    distance_km: Annotated[
        float | None, Query(ge=0.0, description="return only the segment containing this km")
    ] = None,
) -> RaceWeekClimatology:
    """The built ERA5 22-29 Aug climatology (``DataKind.CLIMATOLOGY``), or one segment of it."""
    route = repo.get(route_id)  # 404 for unknown ids; also keeps the path inside the dir
    path = climatology_path(settings.climatology_dir, route.id)
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"race-week climatology for {route.id!r} not built; "
            "run scripts/build_climatology.py",
        )
    try:
        clim = RaceWeekClimatology.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"climatology file {path.name} is invalid; rebuild it",
        ) from exc
    if clim.route_id != route.id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"climatology file {path.name} is for route {clim.route_id!r}",
        )
    if distance_km is None:
        return clim
    if distance_km > route.total_distance_m / 1000.0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"distance_km {distance_km} is beyond the route end",
        )
    last = len(clim.segments) - 1
    hits = [
        s
        for i, s in enumerate(clim.segments)
        if s.segment.start_km <= distance_km < s.segment.end_km
        or (i == last and distance_km == s.segment.end_km)
    ]
    if not hits:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no climatology segment covers km {distance_km}",
        )
    return clim.model_copy(update={"segments": hits[:1]})
