"""Space-time trajectory: where will the vehicle be, and when.

This is the route-aware half of "weather = f(lat, lon, elevation, future time)":
given a start distance/time and a speed (constant or profile) it produces
sample points with the predicted UTC arrival time at each.

Model (kinematic, no energy/strategy feedback):
  * distance advances in internal integration sub-steps of at most
    ``INTEGRATION_STEP_M`` (1 km); travel time of a sub-step is
    ``length / speed(midpoint distance)`` (midpoint rule);
  * mandatory control-stop dwell (``RouteWaypoint.mandatory_dwell_s``) is added
    after arriving at each control stop strictly ahead of the start, when
    ``honour_control_stops``; the reported ETA of a stop is its *arrival* time;
  * optional daily driving window. Which clock it is read on is explicit
    (``window_clock``): ``"event"`` (default) is the regulation clock, ACST
    UTC+9:30 fixed (2027 Event Regulations clause 1.3.1; the 08:00/17:00 rule
    times are event time and do not shift with position or daylight saving);
    ``"position"`` is local civil time at the vehicle's own position from
    :func:`rwi.core.timebase.timezone_for_point` (IANA zone, DST aware, never
    the machine's zone), re-evaluated per sub-step so crossing a timezone
    boundary is honoured. Outside the window the vehicle is stationary and
    resumes at the next window start at its current position.

BWSC rules (verified, 2027 Event Regulations V1.0, read 2026-11-01; see
:mod:`rwi.regulations` and :data:`rwi.route.bwsc.BWSC_DAILY_WINDOW_EVENT_TIME`):
08:00-17:00 event time (3.21.1-2, 3.27.9), 30 min control stop (3.27.8). NOT enforced
here: the ban on driving between sunset and sunrise (3.24.8) - this function has no
sunrise/sunset input and applies the clock window only; check results with
:func:`rwi.regulations.is_within_driving_window`. This module has no hidden default
window: callers pass it explicitly.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from datetime import tzinfo as TzInfo
from typing import Literal

from rwi.contracts import (
    Route,
    RouteWaypoint,
    Trajectory,
    TrajectoryPoint,
    WaypointKind,
)
from rwi.core.errors import RouteError
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import ensure_utc, timezone_for_point, utc_now
from rwi.regulations import EVENT_TIMEZONE, REGULATIONS_VERSION, SCHEDULE
from rwi.route.geometry import (
    grade_at_distance,
    has_elevation,
    point_at_distance,
)

__all__ = ["INTEGRATION_STEP_M", "build_trajectory"]

INTEGRATION_STEP_M = 1000.0
_EPS_M = 1e-6
_MATCH_M = 1e-3  # samples closer than this to a waypoint are that waypoint
_MAX_SUBSTEPS = 2_000_000  # hard stop against pathological inputs


def _combine(day: date, t: time, tz: TzInfo) -> datetime:
    """Aware UTC instant of local civil ``t`` on local date ``day`` in ``tz``."""
    return datetime.combine(day, t, tzinfo=tz).astimezone(UTC)


def _window_state(
    now_utc: datetime, point: GeoPoint, window: tuple[time, time], clock: str = "event"
) -> tuple[bool, datetime, datetime]:
    """(inside, window_end_utc_for_today, next_window_start_utc) at this position.

    ``window_end`` is meaningful when inside; ``next_start`` when outside.
    """
    tz = EVENT_TIMEZONE if clock == "event" else timezone_for_point(point)
    local = now_utc.astimezone(tz)
    start, end = window
    today_start = _combine(local.date(), start, tz)
    today_end = _combine(local.date(), end, tz)
    if today_start <= now_utc < today_end:
        return True, today_end, today_start
    if now_utc < today_start:
        return False, today_end, today_start
    return False, today_end, _combine(local.date() + timedelta(days=1), start, tz)


def build_trajectory(
    route: Route,
    *,
    start_distance_m: float,
    start_time_utc: datetime,
    speed_mps: float | Callable[[float], float],
    sample_spacing_m: float = 25_000.0,
    honour_control_stops: bool = True,
    daily_window_local: tuple[time, time] | None = None,
    window_clock: Literal["event", "position"] = "event",
) -> Trajectory:
    """Predict the vehicle's position and arrival time along a route.

    Args:
        route: route with cumulative distances (m).
        start_distance_m: where the vehicle is now, metres along the route.
        start_time_utc: aware datetime; the instant the vehicle is at
            ``start_distance_m``. Naive datetimes are rejected.
        speed_mps: constant speed (m/s) or ``f(distance_along_route_m) -> m/s``.
            Must be finite and strictly positive everywhere it is evaluated.
        sample_spacing_m: spacing of the output samples (m). Output also
            includes the start, every control stop ahead, and the route end.
        honour_control_stops: add each control stop's ``mandatory_dwell_s``
            (a stop located exactly at the start distance is treated as already
            departed, no dwell).
        daily_window_local: ``(start, end)`` clock times, ``start < end``
            on one day (start inclusive, end exclusive); outside it the vehicle
            is stopped until the next ``start``. ``None`` = drives around the
            clock. The BWSC window is
            :data:`rwi.route.bwsc.BWSC_DAILY_WINDOW_EVENT_TIME` (08:00-17:00,
            verified, clauses 3.21.1-2 / 3.27.9).
        window_clock: ``"event"`` (default) reads the window in ACST UTC+9:30,
            the regulation clock (clause 1.3.1); ``"position"`` reads it as
            local civil time at the vehicle's own position (use for generic,
            non-BWSC windows, or to see the effect of the zone).

    Returns:
        :class:`Trajectory` whose points carry distance, position, ETA (UTC),
        heading, speed, grade (0.0 where elevation is unknown) and the waypoint
        when the sample is a typed waypoint (control stop, start, finish...).
        ``assumptions`` records every input assumption as flat scalars.

    Raises:
        ValueError: non-positive/non-finite speed (constant or from the
            profile), non-positive spacing, a degenerate window.
        RouteError: ``start_distance_m`` outside the route.
        NaiveDatetimeError: naive ``start_time_utc``.

    Complexity: O(route_length / 1 km) sub-steps, each O(log N) plus one
    timezone lookup when a window is used.
    """
    start_time = ensure_utc(start_time_utc)
    total = route.total_distance_m
    if not math.isfinite(start_distance_m) or not 0.0 <= start_distance_m <= total:
        raise RouteError(f"start_distance_m {start_distance_m!r} outside route [0, {total:.1f}] m")
    if not math.isfinite(sample_spacing_m) or sample_spacing_m <= 0.0:
        raise ValueError(f"sample_spacing_m must be positive and finite, got {sample_spacing_m!r}")
    if daily_window_local is not None and not daily_window_local[0] < daily_window_local[1]:
        raise ValueError(
            "daily_window_local must have start < end on the same day; "
            f"got {daily_window_local[0]}..{daily_window_local[1]}"
        )

    def speed_at(distance_m: float) -> float:
        v = speed_mps(distance_m) if callable(speed_mps) else float(speed_mps)
        if not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0.0:
            raise ValueError(
                f"speed must be finite and > 0 m/s (got {v!r} at {distance_m:.0f} m); "
                "a zero or negative speed would never reach the destination"
            )
        return float(v)

    speed_at(start_distance_m)  # fail fast for constants / a bad profile start

    # --- sample distances ------------------------------------------------
    targets: set[float] = {start_distance_m, total}
    k = 1
    while start_distance_m + k * sample_spacing_m < total - _EPS_M:
        targets.add(start_distance_m + k * sample_spacing_m)
        k += 1
    dwell_at: dict[float, RouteWaypoint] = {}
    if honour_control_stops:
        for w in route.waypoints:
            if (
                w.kind is WaypointKind.CONTROL_STOP
                and w.cumulative_distance_m > start_distance_m + _EPS_M
            ):
                dwell_at[w.cumulative_distance_m] = w
                targets.add(w.cumulative_distance_m)
    wp_by_distance = {
        w.cumulative_distance_m: w for w in route.waypoints if w.kind is not WaypointKind.WAYPOINT
    }
    ordered: list[float] = []
    for d in sorted(targets):
        if ordered and d - ordered[-1] <= _MATCH_M:
            if d in dwell_at and len(ordered) > 1:  # a control stop wins over a plain sample
                ordered[-1] = d
            continue
        ordered.append(d)

    # --- integrate ---------------------------------------------------------
    now = start_time
    dwell_total = 0.0
    overnight_total = 0.0
    overnight_count = 0
    moving_s = 0.0
    substeps = 0
    pos_d = start_distance_m
    points: list[TrajectoryPoint] = []
    grade_known = has_elevation(route)

    def emit(d: float, t: datetime) -> None:
        pt, heading = point_at_distance(route, d)
        wp = wp_by_distance.get(d)
        if wp is None:
            wp = next((w for dd, w in wp_by_distance.items() if abs(dd - d) <= _MATCH_M), None)
        points.append(
            TrajectoryPoint(
                distance_m=d,
                point=pt,
                eta_utc=t,
                heading_deg=heading % 360.0,
                speed_mps=speed_at(d),
                grade=grade_at_distance(route, d),
                waypoint=wp,
            )
        )

    emit(pos_d, now)
    for target in ordered[1:]:
        while target - pos_d > _EPS_M:
            substeps += 1
            if substeps > _MAX_SUBSTEPS:
                raise RouteError("trajectory integration exceeded the sub-step limit")
            step = min(INTEGRATION_STEP_M, target - pos_d)
            v = speed_at(pos_d + step / 2.0)
            remaining_m = step
            here, _ = point_at_distance(route, pos_d)
            while remaining_m > _EPS_M:
                if daily_window_local is not None:
                    inside, end_utc, next_start = _window_state(
                        now, here, daily_window_local, window_clock
                    )
                    if not inside:
                        overnight_total += (next_start - now).total_seconds()
                        overnight_count += 1
                        now = next_start
                        continue
                    avail_s = (end_utc - now).total_seconds()
                    need_s = remaining_m / v
                    if need_s > avail_s:  # window closes mid-step: drive until it does
                        now = end_utc
                        moving_s += avail_s
                        remaining_m -= avail_s * v
                        continue
                dt = remaining_m / v
                now += timedelta(seconds=dt)
                moving_s += dt
                remaining_m = 0.0
            pos_d += step
        pos_d = target
        emit(target, now)
        stop = dwell_at.get(target)
        if stop is not None and stop.mandatory_dwell_s:
            now += timedelta(seconds=stop.mandatory_dwell_s)
            dwell_total += stop.mandatory_dwell_s

    # --- assumptions and provenance --------------------------------------
    first_v = speed_at(start_distance_m)
    window_verified = (
        daily_window_local is not None
        and window_clock == "event"
        and daily_window_local == (SCHEDULE.daily_start, SCHEDULE.daily_stop)
    )
    travelled = total - start_distance_m
    assumptions: dict[str, str | float | bool] = {
        "speed_model": "callable_profile" if callable(speed_mps) else "constant",
        "speed_mps_at_start": first_v,
        "mean_moving_speed_mps": travelled / moving_s if moving_s > 0 else first_v,
        "sample_spacing_m": float(sample_spacing_m),
        "integration_step_m": INTEGRATION_STEP_M,
        "eta_semantics": "arrival time at the sample; control-stop dwell is added after it",
        "honour_control_stops": honour_control_stops,
        "control_stops_with_dwell": float(sum(1 for w in dwell_at.values() if w.mandatory_dwell_s)),
        "dwell_total_s": dwell_total,
        "dwell_source": (
            "RouteWaypoint.mandatory_dwell_s; BWSC 1800 s = 2027 Event Regulations "
            "clause 3.27.8 (penalty time not modelled)"
        ),
        "daily_window_applied": daily_window_local is not None,
        "daily_window_local": (
            f"{daily_window_local[0].isoformat()}-{daily_window_local[1].isoformat()}"
            if daily_window_local
            else "none (drives continuously)"
        ),
        "daily_window_timezone": (
            "n/a"
            if daily_window_local is None
            else "ACST UTC+9:30 fixed, event time (clause 1.3.1)"
            if window_clock == "event"
            else "IANA zone at the vehicle's own position (rwi.core.timebase.timezone_for_point)"
        ),
        "daily_window_clock": window_clock if daily_window_local else "n/a",
        "daily_window_verified": window_verified,
        "daily_window_source": (
            "none"
            if daily_window_local is None
            else f"{REGULATIONS_VERSION}: clauses 3.21.1-3.21.2 (08:00 start), "
            "3.27.9 (17:00 pause), 1.3.1 (ACST event time)"
            if window_verified
            else "caller-supplied window; not the BWSC regulation window or not on the event clock"
        ),
        "night_driving_clause": "3.24.8: no driving between sunset and sunrise",
        "night_driving_enforced": False,
        "night_driving_note": (
            "build_trajectory applies the clock window only; it takes no sunrise/sunset input. "
            "Check with rwi.regulations.is_within_driving_window(when, sunrise_utc=, sunset_utc=)"
        ),
        "overnight_stops": float(overnight_count),
        "overnight_total_s": overnight_total,
        "grade_known": grade_known,
        "grade_unknown_value": "n/a" if grade_known else "0.0 (no information, not flat terrain)",
        "start_distance_m": float(start_distance_m),
        "start_time_utc": start_time.isoformat(),
        "traffic_and_road_works": "not modelled",
    }
    provenance = Provenance(
        provider="rwi",
        dataset="route.trajectory",
        kind=DataKind.DERIVED,
        retrieved_at_utc=utc_now(),
        model_name="kinematic-eta (speed profile, control-stop dwell, daily window)",
        processing=f"route '{route.id}'; {route.provenance.label()}",
        license=route.provenance.license,
        notes=("Predicted, not observed. Route geometry source: " + route.source_description[:300]),
    )
    return Trajectory(
        route_id=route.id,
        generated_at_utc=utc_now(),
        points=points,
        assumptions=assumptions,
        provenance=provenance,
    )
