"""Regulatory limits. These numbers were read out of the official 2027 PDF."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from rwi.core.units import kmh_to_ms
from rwi.regulations import (
    CLASS_LIMITS,
    MIN_OPEN_ROAD_SPEED_KMH,
    PROHIBITED_CELL_MATERIALS,
    EventClass,
    check_cell_material,
    check_collector_area,
    check_energy_storage,
    check_speed,
    event_local_time,
    is_charging_locked_out,
    is_within_driving_window,
    summary,
)


@pytest.mark.science
def test_challenger_limits_match_the_published_clauses() -> None:
    """Clause 2.4.2: 6 m2 collector area. Clause 2.5.2: 11 MJ energy storage."""
    limits = CLASS_LIMITS[EventClass.CHALLENGER]
    assert limits.max_collector_area_m2 == 6.0
    assert limits.max_energy_storage_mj == 11.0
    # 11 MJ = 11e6 J / 3600 = 3055.55... Wh, i.e. just over 3 kWh.
    assert limits.max_energy_storage_wh == pytest.approx(3055.5555, abs=0.001)


def test_cruiser_area_is_unrestricted_but_storage_is_not() -> None:
    limits = CLASS_LIMITS[EventClass.CRUISER]
    assert limits.max_collector_area_m2 is None
    assert limits.max_energy_storage_mj == 55.0
    assert check_collector_area(25.0, EventClass.CRUISER) is None


def test_oversized_array_is_a_blocking_finding() -> None:
    assert check_collector_area(5.9) is None
    finding = check_collector_area(6.2)
    assert finding is not None
    assert finding.clause == "2.4.2"
    assert finding.blocking


def test_oversized_battery_is_a_blocking_finding() -> None:
    assert check_energy_storage(3000.0) is None
    finding = check_energy_storage(5000.0)
    assert finding is not None
    assert finding.clause == "2.5.2"
    assert "11 MJ" in finding.detail


def test_prohibited_cell_materials_are_rejected() -> None:
    """Clause 2.4.4 bans GaAs, CdTe and CuInSe2 outright."""
    assert PROHIBITED_CELL_MATERIALS == ("GaAs", "CdTe", "CuInSe2")
    for banned in ("GaAs", "gaas triple junction", "CdTe thin film", "CuInSe2"):
        finding = check_cell_material(banned)
        assert finding is not None, banned
        assert finding.clause == "2.4.4"
        assert finding.blocking
    assert check_cell_material("monocrystalline silicon") is None
    assert check_cell_material(None) is None


def test_speed_above_the_posted_limit_is_always_blocking() -> None:
    findings = check_speed(kmh_to_ms(115.0), posted_limit_kmh=110.0)
    assert any(f.blocking and "posted limit" in f.detail for f in findings)


def test_an_unknown_posted_limit_produces_no_invented_limit() -> None:
    """The platform must not assume a speed limit it has not been told."""
    findings = check_speed(kmh_to_ms(125.0), posted_limit_kmh=None)
    assert not any("posted limit" in f.detail for f in findings)


def test_slow_speeds_are_flagged_at_two_levels() -> None:
    gentle = check_speed(kmh_to_ms(55.0), posted_limit_kmh=80.0)
    assert [f.clause for f in gentle] == ["3.29.1"]
    assert not gentle[0].blocking
    obstructive = check_speed(kmh_to_ms(45.0), posted_limit_kmh=130.0)
    assert any(f.clause == "3.31.6" and f.blocking for f in obstructive)


def test_normal_cruising_speed_is_clean() -> None:
    assert check_speed(kmh_to_ms(85.0), posted_limit_kmh=130.0) == []
    assert MIN_OPEN_ROAD_SPEED_KMH == 60.0


@pytest.mark.science
def test_regulation_clock_times_use_fixed_acst_not_the_vehicle_position() -> None:
    """Clause 1.3.1 fixes event time at ACST = UTC+9:30, so a regulation time
    does not shift when the vehicle crosses into South Australia or when
    daylight saving starts somewhere along the route."""
    instant = datetime(2027, 8, 24, 3, 0, tzinfo=UTC)
    local = event_local_time(instant)
    assert (local.hour, local.minute) == (12, 30)
    # And in January, when South Australia would be on daylight saving, the
    # event clock is unchanged.
    january = event_local_time(datetime(2027, 1, 15, 3, 0, tzinfo=UTC))
    assert (january.hour, january.minute) == (12, 30)


def test_driving_window_respects_the_eight_to_five_rule() -> None:
    # 22:00 UTC = 07:30 ACST: before the official start.
    assert not is_within_driving_window(datetime(2027, 8, 23, 22, 0, tzinfo=UTC))
    # 23:00 UTC = 08:30 ACST: inside the window.
    assert is_within_driving_window(datetime(2027, 8, 23, 23, 0, tzinfo=UTC))
    # 07:30 UTC = 17:00 ACST exactly: the window has closed.
    assert not is_within_driving_window(datetime(2027, 8, 24, 7, 30, tzinfo=UTC))


def test_night_driving_is_prohibited_even_inside_the_window() -> None:
    """Clause 3.24.8. A winter sunset before 17:00 ACST binds before the window
    does, so both constraints are checked."""
    instant = datetime(2027, 8, 24, 7, 0, tzinfo=UTC)  # 16:30 ACST
    sunrise = datetime(2027, 8, 23, 21, 30, tzinfo=UTC)
    early_sunset = datetime(2027, 8, 24, 6, 30, tzinfo=UTC)  # 16:00 ACST
    late_sunset = datetime(2027, 8, 24, 9, 0, tzinfo=UTC)  # 18:30 ACST
    assert not is_within_driving_window(instant, sunrise_utc=sunrise, sunset_utc=early_sunset)
    assert is_within_driving_window(instant, sunrise_utc=sunrise, sunset_utc=late_sunset)


def test_charging_lockout_wraps_midnight() -> None:
    """Clause 3.17.2: no charging capability between 19:00 and 06:30 ACST."""
    # 10:00 UTC = 19:30 ACST -> locked out.
    assert is_charging_locked_out(datetime(2027, 8, 24, 10, 0, tzinfo=UTC))
    # 20:00 UTC = 05:30 ACST next day -> still locked out.
    assert is_charging_locked_out(datetime(2027, 8, 24, 20, 0, tzinfo=UTC))
    # 22:00 UTC = 07:30 ACST -> allowed.
    assert not is_charging_locked_out(datetime(2027, 8, 24, 22, 0, tzinfo=UTC))
    # 08:00 UTC = 17:30 ACST -> allowed.
    assert not is_charging_locked_out(datetime(2027, 8, 24, 8, 0, tzinfo=UTC))


def test_summary_is_honest_about_what_is_not_published() -> None:
    body = summary()
    assert "V1.0" in str(body["version"])
    unpublished = " ".join(str(x) for x in body["not_yet_published"])  # type: ignore[union-attr]
    assert "route notes" in unpublished
    assert "control stop" in unpublished
    silent = " ".join(str(x) for x in body["silent_in_regulations"])  # type: ignore[union-attr]
    assert "forecast" in silent
