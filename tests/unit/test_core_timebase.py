"""Time framework: UTC internally, local time from the active position."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from rwi.core.errors import NaiveDatetimeError
from rwi.core.geo import GeoPoint
from rwi.core.timebase import (
    TimeWindow,
    ensure_utc,
    floor_to_seconds,
    local_utc_offset_hours,
    timezone_name_for,
    to_local,
    utc_now,
)


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(NaiveDatetimeError):
        ensure_utc(datetime(2027, 8, 24, 12, 0))


def test_ensure_utc_converts_rather_than_relabels() -> None:
    stockholm_summer = timezone(timedelta(hours=2))
    local = datetime(2027, 8, 24, 14, 0, tzinfo=stockholm_summer)
    assert ensure_utc(local) == datetime(2027, 8, 24, 12, 0, tzinfo=UTC)


def test_utc_now_is_aware_and_utc() -> None:
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_timezone_comes_from_the_position(darwin: GeoPoint, jonkoping: GeoPoint) -> None:
    assert timezone_name_for(darwin.latitude_deg, darwin.longitude_deg) == "Australia/Darwin"
    assert timezone_name_for(jonkoping.latitude_deg, jonkoping.longitude_deg) == "Europe/Stockholm"


def test_race_route_does_not_share_one_australian_offset(
    darwin: GeoPoint, adelaide: GeoPoint
) -> None:
    """The route crosses NT/SA. In January, South Australia observes daylight saving
    and the Northern Territory does not, so a single hardcoded Australian offset is
    wrong by an hour. This test exists to make that regression impossible."""
    january = datetime(2027, 1, 15, 3, 0, tzinfo=UTC)
    assert local_utc_offset_hours(january, darwin) == pytest.approx(9.5)
    assert local_utc_offset_hours(january, adelaide) == pytest.approx(10.5)
    assert to_local(january, darwin).hour == 12
    assert to_local(january, adelaide).hour == 13


def test_during_the_race_week_nt_and_sa_agree(darwin: GeoPoint, adelaide: GeoPoint) -> None:
    """In August both are UTC+9:30 - which is exactly why testing only race week
    would hide the bug above."""
    august = datetime(2027, 8, 24, 3, 0, tzinfo=UTC)
    assert local_utc_offset_hours(august, darwin) == pytest.approx(9.5)
    assert local_utc_offset_hours(august, adelaide) == pytest.approx(9.5)


def test_local_time_is_independent_of_the_machine_timezone(darwin: GeoPoint) -> None:
    instant = datetime(2027, 8, 24, 3, 0, tzinfo=UTC)
    local = to_local(instant, darwin)
    assert local.hour == 12 and local.minute == 30
    assert local.utcoffset() == timedelta(hours=9, minutes=30)


def test_floor_to_seconds() -> None:
    when = datetime(2027, 8, 24, 3, 47, 13, tzinfo=UTC)
    assert floor_to_seconds(when, 3600) == datetime(2027, 8, 24, 3, 0, tzinfo=UTC)
    assert floor_to_seconds(when, 900) == datetime(2027, 8, 24, 3, 45, tzinfo=UTC)
    with pytest.raises(ValueError, match="positive"):
        floor_to_seconds(when, 0)


def test_time_window_validates_and_iterates() -> None:
    start = datetime(2027, 8, 24, 0, 0, tzinfo=UTC)
    window = TimeWindow(start_utc=start, end_utc=start + timedelta(hours=3))
    assert window.duration == timedelta(hours=3)
    assert window.contains(start)
    assert not window.contains(window.end_utc)
    assert list(window.steps(3600)) == [start + timedelta(hours=h) for h in range(3)]
    with pytest.raises(ValueError, match="precede"):
        TimeWindow(start_utc=window.end_utc, end_utc=start)
