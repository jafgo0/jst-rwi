"""BWSC-specific reference data and the default route loader.

Everything here is labelled by provenance:

* ``OFFICIAL_2025_*`` are transcribed from the **2025** BWSC Route Notes
  (V1.0 Print, published 13 July 2025):
  https://assets.worldsolarchallenge.org/app/uploads/2025/07/13131527/2025-BWSC-Route-Notes-V1-FINAL-PRINT-Published-13072025.pdf
  They are the best available stand-in for 2027; the 2027 route notes were not
  published as of 2026-11-01 and the 2027 route may differ.
* ``BWSC_*`` rule constants come from :mod:`rwi.regulations`, i.e. the **2027 BWSC Event
  Regulations V1.0** (published 7 May 2026, read 2026-11-01); they are imported, not
  re-declared, so the clause references live in one place.
* Still **unverified for 2027**: the control-stop list, locations, distances and opening hours
  (the 2027 route notes are not published; the 2025 figures above stand in).
"""

from __future__ import annotations

import os
from datetime import time
from pathlib import Path
from typing import NamedTuple

from rwi.contracts import Route
from rwi.core.errors import RouteError
from rwi.regulations import SCHEDULE
from rwi.route.loader import annotate_control_stops, load_route_geojson, read_named_stops_geojson

__all__ = [
    "BWSC_CONTROL_STOP_DWELL_S",
    "BWSC_DAILY_WINDOW_EVENT_TIME",
    "DEFAULT_ROUTE_FILE",
    "OFFICIAL_2025_CONTROL_STOPS",
    "OFFICIAL_2025_TOTAL_KM",
    "ControlStopRef",
    "default_routes_dir",
    "load_bwsc_route",
]

OFFICIAL_2025_TOTAL_KM = 3026.92
"""Cumulative distance, State Square (Darwin) to Victoria Park finish staging, 2025 Route Notes
(PDF p.48, entry "3026.92 Follow event official direction into final control stop")."""

BWSC_CONTROL_STOP_DWELL_S: float = SCHEDULE.control_stop_duration_s
"""Mandatory control-stop duration, 1800 s. VERIFIED: 2027 Event Regulations V1.0 clause 3.27.8
("The duration of each control stop will be 30 minutes, plus any penalty time imposed...").
Penalty time is not modelled. Value is imported from :data:`rwi.regulations.SCHEDULE`."""

BWSC_DAILY_WINDOW_EVENT_TIME: tuple[time, time] = (SCHEDULE.daily_start, SCHEDULE.daily_stop)
"""Daily 08:00-17:00 window. VERIFIED: 2027 Event Regulations V1.0 clauses 3.21.1-3.21.2
(official start 08:00 each day) and 3.27.9 (control-stop timing pauses at 17:00, resumes 08:00).
These are **event time, ACST = UTC+9:30 fixed** (clause 1.3.1), not position-local time: pass
this with ``build_trajectory(..., window_clock="event")`` (the default). The separate
sunset-to-sunrise driving ban (3.24.8) is NOT part of this window. Imported from
:data:`rwi.regulations.SCHEDULE`."""

DEFAULT_ROUTE_FILE = "bwsc-2027-stuart-highway.geojson"


class ControlStopRef(NamedTuple):
    number: int
    name: str
    km: float
    opens_2025: str
    closes_2025: str


# 2025 Route Notes, control-stop boxes (times are the 2025 event's, local; Both NT and SA are
# UTC+9:30 in August, the notes state there is no time difference in August).
OFFICIAL_2025_CONTROL_STOPS: tuple[ControlStopRef, ...] = (
    ControlStopRef(1, "Katherine", 322.0, "Sun 24 Aug 11:00", "Sun 24 Aug 17:00"),
    ControlStopRef(2, "Dunmarra", 631.0, "Sun 24 Aug 15:00", "Mon 25 Aug 14:00"),
    ControlStopRef(3, "Tennant Creek", 988.0, "Mon 25 Aug 10:00", "Tue 26 Aug 11:00"),
    ControlStopRef(4, "Barrow Creek", 1212.3, "Mon 25 Aug 13:00", "Tue 26 Aug 15:00"),
    ControlStopRef(5, "Alice Springs", 1496.0, "Tue 26 Aug 08:00", "Wed 27 Aug 10:30"),
    ControlStopRef(6, "Erldunda", 1694.0, "Tue 26 Aug 11:00", "Wed 27 Aug 14:00"),
    ControlStopRef(7, "Coober Pedy", 2181.0, "Tue 26 Aug 16:00", "Thu 28 Aug 13:00"),
    ControlStopRef(8, "Glendambo", 2434.0, "Wed 27 Aug 10:00", "Thu 28 Aug 17:00"),
    ControlStopRef(9, "Port Augusta", 2720.5, "Wed 27 Aug 14:00", "Fri 29 Aug 13:00"),
)


def default_routes_dir() -> Path:
    """``data/routes`` of this checkout, or ``$RWI_ROUTES_DIR`` when set."""
    env = os.environ.get("RWI_ROUTES_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3] / "data" / "routes"


def load_bwsc_route(
    path: Path | None = None,
    *,
    route_id: str = "bwsc-2027-stuart-highway",
    name: str = "Darwin to Adelaide via Stuart Highway (OSM geometry, 2025 control stops)",
    dwell_s: float | None = BWSC_CONTROL_STOP_DWELL_S,
    max_snap_m: float = 5000.0,
) -> Route:
    """Load the bundled BWSC route with control stops promoted to waypoints.

    The control-stop *dwell* defaults to :data:`BWSC_CONTROL_STOP_DWELL_S`
    (clause 3.27.8). ``max_snap_m`` is generous (5 km) because the stop positions
    are town reference points, not surveyed entrances (Coober Pedy and Alice Springs
    town centres are the furthest from the highway).

    Raises:
        RouteError: file missing or invalid.
    """
    file = Path(path) if path is not None else default_routes_dir() / DEFAULT_ROUTE_FILE
    if not file.is_file():
        raise RouteError(f"route file not found: {file}")
    route = load_route_geojson(file, route_id=route_id, name=name, max_jump_m=200_000.0)
    stops = read_named_stops_geojson(file, default_dwell_s=dwell_s)
    return annotate_control_stops(route, stops, max_snap_m=max_snap_m)
