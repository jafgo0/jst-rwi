"""Trajectory/ETA model. Deterministic, no network."""

from __future__ import annotations

import math
from datetime import UTC, datetime, time, timedelta

import pytest

from rwi.contracts import Route, RouteWaypoint, WaypointKind
from rwi.core.errors import NaiveDatetimeError, RouteError
from rwi.core.geo import GeoPoint, destination_point, geodesic_distance_m
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import timezone_for_point
from rwi.regulations import SCHEDULE
from rwi.route.trajectory import build_trajectory

PROV = Provenance(
    provider="test",
    dataset="synthetic",
    kind=DataKind.SIMULATION,
    retrieved_at_utc=datetime(2026, 11, 1, tzinfo=UTC),
)
WINDOW = (
    SCHEDULE.daily_start,
    SCHEDULE.daily_stop,
)  # BWSC rule: 08:00-17:00, clauses 3.21 / 3.27.9


def straight_route(
    start: GeoPoint,
    length_m: float,
    *,
    bearing: float = 0.0,
    vertices: int = 11,
    stops: dict[int, float] | None = None,
    elevations: list[float] | None = None,
) -> Route:
    """Straight geodesic with ``vertices`` points; ``stops`` maps vertex index -> dwell (s)."""
    pts = [
        destination_point(start, bearing, length_m * i / (vertices - 1)) for i in range(vertices)
    ]
    wps = []
    for i, p in enumerate(pts):
        if elevations is not None:
            p = GeoPoint(
                latitude_deg=p.latitude_deg,
                longitude_deg=p.longitude_deg,
                elevation_m=elevations[i],
            )
        cum = (
            0.0
            if i == 0
            else wps[-1].cumulative_distance_m + geodesic_distance_m(pts[i - 1], pts[i])
        )
        stop = (stops or {}).get(i)
        wps.append(
            RouteWaypoint(
                point=p,
                cumulative_distance_m=cum,
                kind=WaypointKind.CONTROL_STOP if stop is not None else WaypointKind.WAYPOINT,
                name=f"CS{i}" if stop is not None else None,
                mandatory_dwell_s=stop,
            )
        )
    return Route(
        id="syn",
        name="syn",
        waypoints=wps,
        total_distance_m=wps[-1].cumulative_distance_m,
        provenance=PROV,
        source_description="synthetic",
    )


DARWIN_START = GeoPoint(latitude_deg=-12.5, longitude_deg=130.9)
PERTH_START = GeoPoint(latitude_deg=-31.9, longitude_deg=115.9)
T0 = datetime(2027, 8, 24, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- constant speed


def test_constant_speed_gives_arithmetic_eta() -> None:
    route = straight_route(DARWIN_START, 200_000.0)
    traj = build_trajectory(
        route, start_distance_m=0.0, start_time_utc=T0, speed_mps=20.0, sample_spacing_m=50_000.0
    )
    d = [p.distance_m for p in traj.points]
    assert d[0] == 0.0 and d[-1] == pytest.approx(route.total_distance_m)
    assert d[:5] == pytest.approx([0.0, 50_000.0, 100_000.0, 150_000.0, 200_000.0], abs=1.0)
    for p in traj.points:
        expected = T0 + timedelta(seconds=p.distance_m / 20.0)
        assert abs((p.eta_utc - expected).total_seconds()) < 1e-3
        assert p.speed_mps == 20.0
        assert p.heading_deg == pytest.approx(0.0, abs=0.01) or p.heading_deg == pytest.approx(
            360.0, abs=0.01
        )
    assert traj.arrival_utc == traj.points[-1].eta_utc
    assert traj.route_id == "syn"


def test_start_mid_route_and_default_spacing() -> None:
    route = straight_route(DARWIN_START, 200_000.0)
    traj = build_trajectory(route, start_distance_m=60_000.0, start_time_utc=T0, speed_mps=25.0)
    assert traj.points[0].distance_m == 60_000.0
    assert traj.points[0].eta_utc == T0
    assert [round(p.distance_m) for p in traj.points[:3]] == [60_000, 85_000, 110_000]
    assert traj.arrival_utc is not None
    expected = T0 + timedelta(seconds=(route.total_distance_m - 60_000.0) / 25.0)
    assert abs((traj.arrival_utc - expected).total_seconds()) < 1e-3


def test_callable_speed_profile() -> None:
    route = straight_route(DARWIN_START, 100_000.0)
    half = route.total_distance_m / 2.0

    def profile(distance_m: float) -> float:
        return 10.0 if distance_m < 50_000.0 else 20.0

    traj = build_trajectory(
        route, start_distance_m=0.0, start_time_utc=T0, speed_mps=profile, sample_spacing_m=25_000.0
    )
    expected_s = 50_000.0 / 10.0 + (route.total_distance_m - 50_000.0) / 20.0
    assert traj.arrival_utc is not None
    assert (traj.arrival_utc - T0).total_seconds() == pytest.approx(expected_s, abs=1e-3)
    assert half > 49_000.0
    assert traj.points[1].speed_mps == 10.0 and traj.points[-1].speed_mps == 20.0
    assert traj.assumptions["speed_model"] == "callable_profile"


# --------------------------------------------------------------------------- control stops


def test_control_stop_dwell_adds_exactly_the_dwell() -> None:
    plain = straight_route(DARWIN_START, 100_000.0)
    with_stop = straight_route(DARWIN_START, 100_000.0, stops={5: 1800.0})
    t_plain = build_trajectory(plain, start_distance_m=0.0, start_time_utc=T0, speed_mps=20.0)
    t_stop = build_trajectory(with_stop, start_distance_m=0.0, start_time_utc=T0, speed_mps=20.0)
    assert t_plain.arrival_utc is not None and t_stop.arrival_utc is not None
    assert (t_stop.arrival_utc - t_plain.arrival_utc).total_seconds() == pytest.approx(
        1800.0, abs=1e-6
    )
    stop_pt = next(p for p in t_stop.points if p.waypoint and p.waypoint.name == "CS5")
    # ETA of the stop is its arrival time (dwell is added after it)
    assert (stop_pt.eta_utc - T0).total_seconds() == pytest.approx(
        stop_pt.distance_m / 20.0, abs=1e-3
    )
    assert stop_pt.waypoint is not None and stop_pt.waypoint.kind is WaypointKind.CONTROL_STOP
    assert t_stop.assumptions["dwell_total_s"] == 1800.0
    assert t_stop.assumptions["control_stops_with_dwell"] == 1.0


def test_dwell_ignored_when_not_honoured_and_not_applied_at_start() -> None:
    route = straight_route(DARWIN_START, 100_000.0, stops={5: 1800.0, 0: 900.0})
    base = build_trajectory(
        straight_route(DARWIN_START, 100_000.0),
        start_distance_m=0.0,
        start_time_utc=T0,
        speed_mps=20.0,
    )
    off = build_trajectory(
        route, start_distance_m=0.0, start_time_utc=T0, speed_mps=20.0, honour_control_stops=False
    )
    assert off.arrival_utc == base.arrival_utc
    on = build_trajectory(route, start_distance_m=0.0, start_time_utc=T0, speed_mps=20.0)
    assert on.arrival_utc is not None and base.arrival_utc is not None
    # the stop at the starting distance (vertex 0) is treated as already departed
    assert (on.arrival_utc - base.arrival_utc).total_seconds() == pytest.approx(1800.0, abs=1e-6)
    started_after = build_trajectory(
        route, start_distance_m=70_000.0, start_time_utc=T0, speed_mps=20.0
    )
    assert started_after.assumptions["dwell_total_s"] == 0.0  # the stop is already behind


# ---------------------------------------------------- daily window / local time


def test_window_push_to_next_morning_uses_local_time_at_vehicle_position() -> None:
    """Same UTC instant, two timezones, two different outcomes.

    08:30Z on 2027-08-24 is 18:00 in Darwin (ACST, UTC+9:30: outside 08:00-17:00)
    but 16:30 in Perth (AWST, UTC+8: inside).
    """
    start = datetime(2027, 8, 24, 8, 30, tzinfo=UTC)
    darwin = straight_route(DARWIN_START, 30_000.0)
    perth = straight_route(PERTH_START, 30_000.0)
    assert timezone_for_point(darwin.waypoints[0].point).utcoffset(start) == timedelta(
        hours=9, minutes=30
    )
    assert timezone_for_point(perth.waypoints[0].point).utcoffset(start) == timedelta(hours=8)

    td = build_trajectory(
        darwin,
        start_distance_m=0.0,
        start_time_utc=start,
        speed_mps=20.0,
        daily_window_local=WINDOW,
        window_clock="position",
    )
    tp = build_trajectory(
        perth,
        start_distance_m=0.0,
        start_time_utc=start,
        speed_mps=20.0,
        daily_window_local=WINDOW,
        window_clock="position",
    )
    travel = timedelta(seconds=darwin.total_distance_m / 20.0)
    # Darwin: waits for 08:00 ACST on the 25th = 22:30Z on the 24th
    assert td.arrival_utc == datetime(2027, 8, 24, 22, 30, tzinfo=UTC) + travel
    # first sample keeps the given time: the car is simply parked there
    assert td.points[0].eta_utc == start
    assert td.assumptions["overnight_stops"] == 1.0
    assert td.assumptions["overnight_total_s"] == pytest.approx(14 * 3600.0)
    # Perth: drives immediately
    assert tp.arrival_utc == start + timedelta(seconds=perth.total_distance_m / 20.0)
    assert tp.assumptions["overnight_stops"] == 0.0
    assert tp.assumptions["daily_window_verified"] is False  # position clock is not the rule


def test_window_closing_mid_route_stops_overnight_then_resumes() -> None:
    # Perth 16:30 local, 30 min of driving left in the window at 20 m/s = 36 km, 100 km route.
    start = datetime(2027, 8, 24, 8, 30, tzinfo=UTC)
    route = straight_route(PERTH_START, 100_000.0, bearing=0.0)
    traj = build_trajectory(
        route,
        start_distance_m=0.0,
        start_time_utc=start,
        speed_mps=20.0,
        daily_window_local=WINDOW,
        window_clock="position",
        sample_spacing_m=10_000.0,
    )
    # drive 09:00Z-08:30Z = 1800 s (36 km), rest overnight until 08:00 AWST = 00:00Z next day
    remaining_s = (route.total_distance_m - 36_000.0) / 20.0
    assert traj.arrival_utc is not None
    assert traj.arrival_utc == datetime(2027, 8, 25, 0, 0, tzinfo=UTC) + timedelta(
        seconds=remaining_s
    )
    # no sample may fall inside the closed window
    for p in traj.points:
        local = p.eta_utc.astimezone(timezone_for_point(p.point))
        assert time(8, 0) <= local.time() <= time(17, 0), (p.distance_m, local)
    etas = [p.eta_utc for p in traj.points]
    assert etas == sorted(etas)


def test_window_boundaries_start_inclusive_end_exclusive() -> None:
    route = straight_route(PERTH_START, 10_000.0)
    at_open = datetime(2027, 8, 24, 0, 0, tzinfo=UTC)  # 08:00 AWST exactly
    at_close = datetime(2027, 8, 24, 9, 0, tzinfo=UTC)  # 17:00 AWST exactly
    a = build_trajectory(
        route,
        start_distance_m=0.0,
        start_time_utc=at_open,
        speed_mps=20.0,
        daily_window_local=WINDOW,
        window_clock="position",
    )
    b = build_trajectory(
        route,
        start_distance_m=0.0,
        start_time_utc=at_close,
        speed_mps=20.0,
        daily_window_local=WINDOW,
        window_clock="position",
    )
    assert a.assumptions["overnight_stops"] == 0.0
    assert b.assumptions["overnight_stops"] == 1.0
    assert b.arrival_utc == datetime(2027, 8, 25, 0, 0, tzinfo=UTC) + timedelta(
        seconds=route.total_distance_m / 20.0
    )


def test_window_follows_timezone_change_along_the_route_nt_sa_border_in_summer() -> None:
    """NT is UTC+9:30 all year; SA is UTC+10:30 in January (daylight saving).

    A southbound route crosses the border at 26 S. 06:45Z is 16:15 in the NT
    (window open until 07:30Z) but already 17:15 in SA (window closed 06:30Z).
    A fixed NT offset would let the car drive on; position-based local time must not.
    """
    start_point = GeoPoint(latitude_deg=-25.9, longitude_deg=133.2)
    route = straight_route(start_point, 22_000.0, bearing=180.0, vertices=23)
    nt, sa = route.waypoints[0].point, route.waypoints[-1].point
    when = datetime(2027, 1, 15, 6, 45, tzinfo=UTC)
    assert sa.latitude_deg < -26.0 < nt.latitude_deg  # route really crosses the border
    assert timezone_for_point(nt).utcoffset(when) == timedelta(hours=9, minutes=30)
    assert timezone_for_point(sa).utcoffset(when) == timedelta(hours=10, minutes=30)

    traj = build_trajectory(
        route,
        start_distance_m=0.0,
        start_time_utc=when,
        speed_mps=20.0,
        daily_window_local=WINDOW,
        window_clock="position",
        sample_spacing_m=5_000.0,
    )
    naive = build_trajectory(
        route, start_distance_m=0.0, start_time_utc=when, speed_mps=20.0, sample_spacing_m=5_000.0
    )
    assert naive.arrival_utc is not None and traj.arrival_utc is not None
    assert naive.arrival_utc < datetime(
        2027, 1, 15, 7, 30, tzinfo=UTC
    )  # a fixed +9:30 would allow this
    # With position-based local time the car parks once it is in SA: next SA 08:00 = 21:30Z (15th)
    assert traj.arrival_utc > datetime(2027, 1, 15, 21, 30, tzinfo=UTC)
    assert traj.assumptions["overnight_stops"] == 1.0
    first_sa = next(p for p in traj.points if p.point.latitude_deg < -26.0)
    assert first_sa.eta_utc >= datetime(2027, 1, 15, 21, 30, tzinfo=UTC)


def test_window_requires_ordered_times() -> None:
    route = straight_route(DARWIN_START, 10_000.0)
    with pytest.raises(ValueError, match="start < end"):
        build_trajectory(
            route,
            start_distance_m=0.0,
            start_time_utc=T0,
            speed_mps=10.0,
            daily_window_local=(time(17, 0), time(8, 0)),
        )


# --------------------------------------------------------------------------- guards


@pytest.mark.parametrize("bad", [0.0, -5.0, math.nan, math.inf * 0.0])
def test_non_positive_or_nan_speed_raises(bad: float) -> None:
    route = straight_route(DARWIN_START, 10_000.0)
    with pytest.raises(ValueError, match="speed"):
        build_trajectory(route, start_distance_m=0.0, start_time_utc=T0, speed_mps=bad)


def test_profile_returning_zero_midway_raises_instead_of_hanging() -> None:
    route = straight_route(DARWIN_START, 100_000.0)
    with pytest.raises(ValueError, match="speed"):
        build_trajectory(
            route,
            start_distance_m=0.0,
            start_time_utc=T0,
            speed_mps=lambda d: 10.0 if d < 30_000.0 else 0.0,
        )


def test_other_argument_validation() -> None:
    route = straight_route(DARWIN_START, 10_000.0)
    with pytest.raises(ValueError):
        build_trajectory(
            route, start_distance_m=0.0, start_time_utc=T0, speed_mps=10.0, sample_spacing_m=0.0
        )
    with pytest.raises(RouteError):
        build_trajectory(route, start_distance_m=-1.0, start_time_utc=T0, speed_mps=10.0)
    with pytest.raises(RouteError):
        build_trajectory(
            route, start_distance_m=route.total_distance_m + 1, start_time_utc=T0, speed_mps=10.0
        )
    with pytest.raises(NaiveDatetimeError):
        build_trajectory(
            route, start_distance_m=0.0, start_time_utc=datetime(2027, 8, 24, 0, 0), speed_mps=10.0
        )


# --------------------------------------------------------------------------- content


def test_assumptions_are_flat_scalars_and_cover_the_inputs() -> None:
    route = straight_route(DARWIN_START, 50_000.0)
    traj = build_trajectory(
        route,
        start_distance_m=0.0,
        start_time_utc=T0,
        speed_mps=22.0,
        sample_spacing_m=10_000.0,
        daily_window_local=WINDOW,
    )
    a = traj.assumptions
    assert all(isinstance(v, (str, float, bool)) for v in a.values())
    for key in (
        "speed_model",
        "speed_mps_at_start",
        "sample_spacing_m",
        "honour_control_stops",
        "dwell_total_s",
        "daily_window_applied",
        "daily_window_local",
        "daily_window_timezone",
        "daily_window_verified",
        "grade_known",
    ):
        assert key in a
    assert a["speed_mps_at_start"] == 22.0 and a["sample_spacing_m"] == 10_000.0
    assert a["daily_window_local"] == "08:00:00-17:00:00"
    assert a["grade_known"] is False
    assert traj.provenance.kind is DataKind.DERIVED


def test_grade_is_zero_without_elevation_and_real_with_it() -> None:
    flat = build_trajectory(
        straight_route(DARWIN_START, 10_000.0, vertices=3),
        start_distance_m=0.0,
        start_time_utc=T0,
        speed_mps=10.0,
    )
    assert all(p.grade == 0.0 for p in flat.points)
    assert flat.assumptions["grade_known"] is False
    hilly_route = straight_route(DARWIN_START, 10_000.0, vertices=3, elevations=[10.0, 60.0, 60.0])
    hilly = build_trajectory(
        hilly_route,
        start_distance_m=0.0,
        start_time_utc=T0,
        speed_mps=10.0,
        sample_spacing_m=2_000.0,
    )
    assert hilly.assumptions["grade_known"] is True
    assert hilly.points[0].grade == pytest.approx(50.0 / 5_000.0, rel=1e-3)
    assert hilly.points[-1].grade == pytest.approx(0.0, abs=1e-9)


def test_points_carry_positions_on_the_route() -> None:
    route = straight_route(DARWIN_START, 100_000.0)
    traj = build_trajectory(
        route, start_distance_m=0.0, start_time_utc=T0, speed_mps=10.0, sample_spacing_m=25_000.0
    )
    for p in traj.points:
        expected = destination_point(DARWIN_START, 0.0, p.distance_m)
        assert geodesic_distance_m(p.point, expected) < 0.05


# ------------------------------------------------ regulation clock (event time, ACST)


def test_default_window_clock_is_acst_event_time_independent_of_position() -> None:
    """Clause 1.3.1: regulation times are ACST UTC+9:30 fixed.

    08:30Z is 18:00 ACST: closed everywhere, even in Perth where the *local* time is 16:30.
    """
    start = datetime(2027, 8, 24, 8, 30, tzinfo=UTC)
    for origin in (DARWIN_START, PERTH_START):
        route = straight_route(origin, 30_000.0)
        traj = build_trajectory(
            route,
            start_distance_m=0.0,
            start_time_utc=start,
            speed_mps=20.0,
            daily_window_local=WINDOW,
        )
        travel = timedelta(seconds=route.total_distance_m / 20.0)
        # next 08:00 ACST = 22:30Z
        assert traj.arrival_utc == datetime(2027, 8, 24, 22, 30, tzinfo=UTC) + travel
        assert traj.assumptions["overnight_stops"] == 1.0
        assert traj.assumptions["daily_window_clock"] == "event"
        assert "ACST" in str(traj.assumptions["daily_window_timezone"])


def test_event_clock_does_not_shift_with_south_australian_daylight_saving() -> None:
    """In January SA local time is UTC+10:30, but the event clock stays ACST.

    Same route and instant as the position-clock border test: with the event clock the car
    is still inside 08:00-17:00 ACST (16:15) the whole way, so there is no overnight stop.
    """
    route = straight_route(
        GeoPoint(latitude_deg=-25.9, longitude_deg=133.2), 22_000.0, bearing=180.0, vertices=23
    )
    when = datetime(2027, 1, 15, 6, 45, tzinfo=UTC)
    traj = build_trajectory(
        route, start_distance_m=0.0, start_time_utc=when, speed_mps=20.0, daily_window_local=WINDOW
    )
    assert traj.assumptions["overnight_stops"] == 0.0
    assert traj.arrival_utc == when + timedelta(seconds=route.total_distance_m / 20.0)


def test_window_and_dwell_values_trace_back_to_the_regulations_module() -> None:
    from rwi.route import bwsc

    assert (SCHEDULE.daily_start, SCHEDULE.daily_stop) == bwsc.BWSC_DAILY_WINDOW_EVENT_TIME
    assert SCHEDULE.control_stop_duration_s == bwsc.BWSC_CONTROL_STOP_DWELL_S
    assert WINDOW == bwsc.BWSC_DAILY_WINDOW_EVENT_TIME
    # sanity on the cited values themselves (clauses 3.21.1-2, 3.27.9, 3.27.8)
    assert (time(8, 0), time(17, 0)) == WINDOW
    assert SCHEDULE.control_stop_duration_s == 1800.0


def test_assumptions_cite_the_clauses_and_flag_verification_truthfully() -> None:
    route = straight_route(DARWIN_START, 50_000.0)
    kw = {"start_distance_m": 0.0, "start_time_utc": T0, "speed_mps": 22.0}
    rule = build_trajectory(route, daily_window_local=WINDOW, **kw)
    a = rule.assumptions
    assert a["daily_window_verified"] is True
    for clause in ("3.21.1", "3.27.9", "1.3.1"):
        assert clause in str(a["daily_window_source"])
    assert "2027 BWSC Event Regulations" in str(a["daily_window_source"])
    assert "3.27.8" in str(a["dwell_source"])
    # night driving is a separate rule that this function does not enforce
    assert "3.24.8" in str(a["night_driving_clause"])
    assert a["night_driving_enforced"] is False
    assert "is_within_driving_window" in str(a["night_driving_note"])
    # not the rule's window, or not on the event clock => not "verified"
    other = build_trajectory(route, daily_window_local=(time(9, 0), time(16, 0)), **kw)
    assert other.assumptions["daily_window_verified"] is False
    assert "caller-supplied" in str(other.assumptions["daily_window_source"])
    pos = build_trajectory(route, daily_window_local=WINDOW, window_clock="position", **kw)
    assert pos.assumptions["daily_window_verified"] is False
    none = build_trajectory(route, **kw)
    assert none.assumptions["daily_window_verified"] is False
    assert none.assumptions["daily_window_source"] == "none"
