"""Retrospective-validation lookups.

The one endpoint here is a point-wise GHI bias chip for the dashboard:
given a position, return what the quadrangulation (ECMWF-IFS, NASA POWER,
MERRA-2 against BoM-anchored SILO) at the nearest tabulated route point
said about which direction the models tend to miss. See
``rwi.validation.ghi_bias`` for what the numbers mean and do not mean.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from rwi.api.deps import LatQuery, LonQuery, resolve_point
from rwi.core.state import StateStore, get_state_store
from rwi.validation.ghi_bias import load_default_table

router = APIRouter(prefix="/api/validation", tags=["validation"])

StoreDep = Annotated[StateStore, Depends(get_state_store)]


#: Beyond this radius from the nearest tabulated route point there is no
#: local bias evidence; the chip must say so rather than extrapolate a
#: Darwin bias figure to a point a thousand kilometres away.
BIAS_RELEVANT_RADIUS_KM = 100.0


@router.get("/ghi-bias")
async def read_ghi_bias(
    store: StoreDep,
    lat: Annotated[float | None, LatQuery] = None,
    lon: Annotated[float | None, LonQuery] = None,
    elev: Annotated[float | None, Query()] = None,
) -> dict[str, Any]:
    point = resolve_point(lat, lon, elev, store)
    table = load_default_table()
    nearest, distance_km = table.nearest(point.latitude_deg, point.longitude_deg)
    relevant = distance_km <= BIAS_RELEVANT_RADIUS_KM
    return {
        "nearest_point": nearest.name,
        "nearest_lat": nearest.lat,
        "nearest_lon": nearest.lon,
        "distance_km": round(distance_km, 1),
        "relevant": relevant,
        "relevant_radius_km": BIAS_RELEVANT_RADIUS_KM,
        "window_utc": {"start": table.window_utc[0], "end": table.window_utc[1]},
        "truth": table.truth,
        "unit": table.unit,
        "bias_vs_truth": {
            "ecmwf": nearest.bias_ecmwf,
            "power": nearest.bias_power,
            "merra2": nearest.bias_merra2,
            "median": nearest.bias_median,
            "mean": round(nearest.bias_mean, 1),
        },
        "note": table.note,
        "point_note": nearest.extra,
        "caveat": (
            "Historical signal from BWSC-2025 race week (6 points, 7 days) "
            "comparing gridded reanalyses against BoM-anchored SILO. NOT a "
            "measured error in today's forecast or clear-sky model: forecast "
            "error depends on run, lead time and local weather, and no "
            "station-measured bias has been attached to this dashboard. Use "
            "as awareness of a known 2025 disagreement, not as a correction."
        ),
    }
