"""BWSC 2027 regulatory limits, as verified in the official regulations.

Source: *2027 BWSC Event Regulations V1.0*, published 7 May 2026
(https://worldsolarchallenge.org/2027-event/regulations). Every constant below
carries the clause it comes from and was read out of the published PDF text, not
from memory or from an earlier event year. Facts that are only in the *Team
Manager's Guide V1* are marked ``TMG``; facts that are not published yet are
absent rather than guessed.

Why this module exists: the strategy engine must never recommend an action that
breaks a regulation, and the platform must be able to say *which clause* a
candidate plan would violate. Keeping the limits in one audited place is what
makes that checkable.

IMPORTANT distinction between two kinds of "local time":
  * regulation clock times (start 08:00, control-stop pause 17:00, charging
    lock-out 19:00-06:30) are in **event time, ACST = UTC+9:30 fixed**, per
    clause 1.3.1 - they do not shift with the vehicle's position or with
    daylight saving;
  * solar quantities (sunrise, sunset, solar noon) are **geographic** and come
    from the active position via :mod:`rwi.core.timebase`.
Mixing these up would move the race day by half an hour.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import StrEnum

from rwi.core.timebase import ensure_utc
from rwi.core.units import ms_to_kmh

REGULATIONS_VERSION = "2027 BWSC Event Regulations V1.0 (published 2026-05-07)"
REGULATIONS_URL = "https://worldsolarchallenge.org/2027-event/regulations"
REGULATIONS_READ_ON = "2026-11-01"

EVENT_UTC_OFFSET = timedelta(hours=9, minutes=30)
"""ACST, clause 1.3.1: "The time zone for the event is Australian Central
Standard Time (ACST = UTC + 9.5)." Fixed; not position-derived."""

EVENT_TIMEZONE = timezone(EVENT_UTC_OFFSET, name="ACST")

EVENT_START_DATE = "2027-08-22"
EVENT_END_DATE = "2027-08-29"


class EventClass(StrEnum):
    """Clause 2.1.1."""

    HISTORIC = "historic"
    CHALLENGER = "challenger"
    CRUISER = "cruiser"
    INNOVATION = "innovation"


@dataclass(frozen=True)
class ClassLimits:
    """Hard technical limits for one class."""

    event_class: EventClass
    max_collector_area_m2: float | None
    """None = unrestricted (Cruiser, clause 2.4.3)."""
    max_energy_storage_mj: float | None
    clauses: str

    @property
    def max_energy_storage_wh(self) -> float | None:
        """Storage limit expressed in Wh (1 MJ = 1e6 J = 277.78 Wh)."""
        if self.max_energy_storage_mj is None:
            return None
        return self.max_energy_storage_mj * 1e6 / 3600.0


CLASS_LIMITS: dict[EventClass, ClassLimits] = {
    EventClass.CHALLENGER: ClassLimits(
        event_class=EventClass.CHALLENGER,
        max_collector_area_m2=6.0,
        max_energy_storage_mj=11.0,
        clauses="2.4.2 (area, no tolerance, applies to all configurations), 2.5.2 (storage)",
    ),
    EventClass.CRUISER: ClassLimits(
        event_class=EventClass.CRUISER,
        max_collector_area_m2=None,
        max_energy_storage_mj=55.0,
        clauses="2.4.3 (area unrestricted), 2.5.2 (storage)",
    ),
}
"""Historic and Innovation limits were not extracted and are therefore absent."""

PROHIBITED_CELL_MATERIALS = ("GaAs", "CdTe", "CuInSe2")
"""Clause 2.4.4: materials with high environmental toxicity, including GaAs,
CdTe and CuInSe2, must not be used for solar conversion. A PV model configured
with one of these is not race-legal, whatever its efficiency."""


@dataclass(frozen=True)
class RaceSchedule:
    """Daily timings, all in event time (ACST)."""

    daily_start: time = time(8, 0)
    """Clause 3.21.1-2: official start 08:00 each day."""
    daily_stop: time = time(17, 0)
    """Clause 3.27.9: control-stop timing pauses at 17:00 and resumes 08:00."""
    control_stop_duration_s: float = 1800.0
    """Clause 3.27.8: each control stop lasts 30 minutes, plus any penalty time."""
    driver_change_stop_s: float = 300.0
    """Clause 3.23.4: 5 minutes at designated driver-change points."""
    charging_lockout_start: time = time(19, 0)
    charging_lockout_end: time = time(6, 30)
    """Clause 3.17.2: Challenger energy storage must not be chargeable between
    19:00 and 06:30. Note clauses 3.28.8-9 give 19:15/06:15 for pack sealing -
    the two clause groups disagree and the organisers should be asked."""
    night_driving_prohibited: bool = True
    """Clause 3.24.8: competition vehicles must not be driven on any public road
    between sunset and sunrise."""


SCHEDULE = RaceSchedule()

MIN_OPEN_ROAD_SPEED_KMH = 60.0
"""Clause 3.29.1/3.31.6: a team unable to maintain 60 km/h on the open road may
be required to trailer. This is an operational threshold, not a legal minimum."""

SLOW_PENALTY_SPEED_KMH = 50.0
SLOW_PENALTY_LIMIT_KMH = 100.0
"""Clause 3.31.6: driving below 50 km/h where the posted limit is 100 km/h or
more is penalised."""

MAX_POSTED_SPEED_KMH_TMG = 130.0
"""TMG: some Stuart Highway sections are posted at 130 km/h. This is the highest
posted limit the route is known to contain; it is NOT a permission to drive at
130 anywhere. The posted limit of the current segment always governs."""

GUST_REFERENCE_KMH = 100.0
"""Clause 2.19.3 note: gusts above 100 km/h are possible on the route. Used as a
documented reference level for the alert thresholds, not as a vehicle limit."""

ROUTE_DISTANCE_KM_TMG = 3020.0
"""TMG, approximate. The official 2027 route notes and distance chart were not
published as of 2026-11-01, so no official per-segment distance exists yet."""


@dataclass(frozen=True)
class RegulatoryFinding:
    """One regulatory problem with a candidate plan or configuration."""

    clause: str
    detail: str
    blocking: bool


def check_collector_area(
    area_m2: float, event_class: EventClass = EventClass.CHALLENGER
) -> RegulatoryFinding | None:
    limits = CLASS_LIMITS.get(event_class)
    if limits is None or limits.max_collector_area_m2 is None:
        return None
    if area_m2 > limits.max_collector_area_m2:
        return RegulatoryFinding(
            clause="2.4.2",
            detail=(
                f"collector area {area_m2:.3f} m2 exceeds the "
                f"{limits.max_collector_area_m2:.1f} m2 {event_class.value} limit "
                "(no tolerance, applies to every configuration)"
            ),
            blocking=True,
        )
    return None


def check_energy_storage(
    capacity_wh: float, event_class: EventClass = EventClass.CHALLENGER
) -> RegulatoryFinding | None:
    limits = CLASS_LIMITS.get(event_class)
    if limits is None or limits.max_energy_storage_wh is None:
        return None
    allowed = limits.max_energy_storage_wh
    if capacity_wh > allowed:
        return RegulatoryFinding(
            clause="2.5.2",
            detail=(
                f"energy storage {capacity_wh:.0f} Wh exceeds the {event_class.value} limit of "
                f"{limits.max_energy_storage_mj:.0f} MJ ({allowed:.0f} Wh)"
            ),
            blocking=True,
        )
    return None


def check_cell_material(material: str | None) -> RegulatoryFinding | None:
    if material is None:
        return None
    normalised = material.replace(" ", "").lower()
    for banned in PROHIBITED_CELL_MATERIALS:
        if banned.lower() in normalised:
            return RegulatoryFinding(
                clause="2.4.4",
                detail=(
                    f"cell material {material!r} contains {banned}, which is prohibited for "
                    "solar conversion on environmental-toxicity grounds"
                ),
                blocking=True,
            )
    return None


def check_speed(
    speed_mps: float, *, posted_limit_kmh: float | None = None
) -> list[RegulatoryFinding]:
    """Regulatory findings for a candidate cruising speed.

    The posted limit of the current segment governs. When it is unknown, no
    upper-bound finding is produced - the platform says it does not know rather
    than assuming a limit.
    """
    speed_kmh = ms_to_kmh(speed_mps)
    findings: list[RegulatoryFinding] = []
    if posted_limit_kmh is not None and speed_kmh > posted_limit_kmh:
        findings.append(
            RegulatoryFinding(
                clause="3.31.6 / Australian road law",
                detail=(
                    f"target {speed_kmh:.1f} km/h exceeds the posted limit "
                    f"{posted_limit_kmh:.0f} km/h"
                ),
                blocking=True,
            )
        )
    if speed_kmh < MIN_OPEN_ROAD_SPEED_KMH:
        findings.append(
            RegulatoryFinding(
                clause="3.29.1",
                detail=(
                    f"target {speed_kmh:.1f} km/h is below the {MIN_OPEN_ROAD_SPEED_KMH:.0f} km/h "
                    "open-road minimum a team must be able to maintain"
                ),
                blocking=False,
            )
        )
    if (
        speed_kmh < SLOW_PENALTY_SPEED_KMH
        and posted_limit_kmh is not None
        and posted_limit_kmh >= SLOW_PENALTY_LIMIT_KMH
    ):
        findings.append(
            RegulatoryFinding(
                clause="3.31.6",
                detail=(
                    f"driving at {speed_kmh:.1f} km/h where the limit is "
                    f"{posted_limit_kmh:.0f} km/h is a penalisable obstruction"
                ),
                blocking=True,
            )
        )
    return findings


def event_local_time(when: datetime) -> datetime:
    """Convert a UTC instant to event time (ACST), for regulation clock times."""
    return ensure_utc(when).astimezone(EVENT_TIMEZONE)


def is_within_driving_window(
    when: datetime,
    *,
    sunrise_utc: datetime | None = None,
    sunset_utc: datetime | None = None,
    schedule: RaceSchedule = SCHEDULE,
) -> bool:
    """Whether driving is permitted at this instant.

    Two independent constraints, both of which must hold:
      * the 08:00-17:00 event-time window (3.21, 3.27.9);
      * daylight only (3.24.8), when sunrise/sunset are supplied from the
        active position's solar geometry.
    """
    local = event_local_time(when).time()
    in_window = schedule.daily_start <= local < schedule.daily_stop
    if not in_window:
        return False
    if schedule.night_driving_prohibited and sunrise_utc is not None and sunset_utc is not None:
        instant = ensure_utc(when)
        return sunrise_utc <= instant <= sunset_utc
    return in_window


def is_charging_locked_out(when: datetime, schedule: RaceSchedule = SCHEDULE) -> bool:
    """Whether Challenger storage must be incapable of charging (3.17.2).

    The window wraps midnight, so the comparison is a union of two intervals.
    """
    local = event_local_time(when).time()
    return local >= schedule.charging_lockout_start or local < schedule.charging_lockout_end


def summary() -> dict[str, object]:
    """Machine-readable snapshot for the API, so the UI can cite clauses."""
    return {
        "version": REGULATIONS_VERSION,
        "url": REGULATIONS_URL,
        "read_on": REGULATIONS_READ_ON,
        "event_timezone": "ACST (UTC+9:30, fixed, clause 1.3.1)",
        "event_dates": [EVENT_START_DATE, EVENT_END_DATE],
        "classes": [c.value for c in EventClass],
        "class_limits": {
            key.value: {
                "max_collector_area_m2": value.max_collector_area_m2,
                "max_energy_storage_mj": value.max_energy_storage_mj,
                "max_energy_storage_wh": value.max_energy_storage_wh,
                "clauses": value.clauses,
            }
            for key, value in CLASS_LIMITS.items()
        },
        "prohibited_cell_materials": list(PROHIBITED_CELL_MATERIALS),
        "daily_start_acst": SCHEDULE.daily_start.isoformat(),
        "daily_stop_acst": SCHEDULE.daily_stop.isoformat(),
        "control_stop_duration_s": SCHEDULE.control_stop_duration_s,
        "driver_change_stop_s": SCHEDULE.driver_change_stop_s,
        "charging_lockout_acst": [
            SCHEDULE.charging_lockout_start.isoformat(),
            SCHEDULE.charging_lockout_end.isoformat(),
        ],
        "night_driving_prohibited": SCHEDULE.night_driving_prohibited,
        "min_open_road_speed_kmh": MIN_OPEN_ROAD_SPEED_KMH,
        "not_yet_published": [
            "official route notes and distance chart (Team Manager's Guide: after the "
            "June 2027 route survey)",
            "control stop list, locations and opening hours",
            "per-segment posted speed limits",
        ],
        "silent_in_regulations": [
            "use of weather forecasts, meteorological services or off-vehicle computation",
            "what a support crew may tell the driver",
            "whether teams receive the organiser's tracker feed",
        ],
    }
