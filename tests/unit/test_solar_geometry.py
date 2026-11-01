"""Tests for rwi.solar.geometry (NREL SPA via pvlib). No network access."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rwi.core.errors import NaiveDatetimeError
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind
from rwi.core.timebase import timezone_for_point
from rwi.solar.geometry import solar_day_events, solar_position, solar_positions

JONKOPING = GeoPoint(latitude_deg=57.7826, longitude_deg=14.1618, elevation_m=227.0)
DARWIN = GeoPoint(latitude_deg=-12.4634, longitude_deg=130.8456, elevation_m=30.0)
ALICE = GeoPoint(latitude_deg=-23.6980, longitude_deg=133.8807, elevation_m=545.0)


def _angular_distance(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


# --- THE CRITICAL NIGHT TEST ------------------------------------------------


def test_jonkoping_local_midnight_is_night() -> None:
    # 2026-01-15 23:00Z = 00:00 CET (UTC+1) on 16 January.
    pos = solar_position(JONKOPING, datetime(2026, 1, 15, 23, 0, tzinfo=UTC))
    assert pos.apparent_elevation_deg < 0.0
    assert pos.is_daylight is False
    assert pos.air_mass is None
    # Midwinter midnight at 57.8 N: Sun roughly 53 deg below the horizon.
    assert -55.0 < pos.apparent_elevation_deg < -51.0


def test_darwin_local_midnight_is_night() -> None:
    # Darwin is UTC+9:30 all year; local midnight = 14:30Z the day before.
    when = datetime(2027, 8, 23, 14, 30, tzinfo=UTC)
    pos = solar_position(DARWIN, when)
    assert pos.apparent_elevation_deg < 0.0
    assert pos.is_daylight is False
    assert pos.air_mass is None


# --- AUSTRALIA TEST ---------------------------------------------------------


def test_alice_springs_race_day_sun_is_high_and_north() -> None:
    when = datetime(2027, 8, 24, 3, 0, tzinfo=UTC)  # 12:30 ACST, within ~10 min of solar noon
    pos = solar_position(ALICE, when)
    assert pos.elevation_deg > 40.0
    # Alice Springs is at 23.7 S. In the southern-hemisphere winter (August) the Sun
    # culminates to the NORTH of the zenith, so near local noon its azimuth is close
    # to 0/360 deg (due north): in [270, 360] or [0, 90]. Expected here: ~3 deg.
    assert pos.azimuth_deg >= 270.0 or pos.azimuth_deg <= 90.0
    assert _angular_distance(pos.azimuth_deg, 0.0) < 15.0
    # Declination in late August is about +11 deg.
    assert pos.declination_deg is not None
    assert 10.0 < pos.declination_deg < 12.5
    assert pos.air_mass is not None and 1.0 < pos.air_mass < 1.6
    assert pos.provenance.kind is DataKind.PHYSICAL_MODEL


def test_result_depends_on_coordinates() -> None:
    when = datetime(2027, 8, 24, 3, 0, tzinfo=UTC)
    alice = solar_position(ALICE, when)
    jonk = solar_position(JONKOPING, when)
    # Same instant: 05:00 local in Sweden is before sunrise, mid-day in Australia.
    assert abs(alice.apparent_elevation_deg - jonk.apparent_elevation_deg) > 30.0
    assert alice.azimuth_deg != jonk.azimuth_deg
    assert jonk.is_daylight is False
    assert alice.is_daylight is True


# --- Science reference tests -------------------------------------------------


@pytest.mark.science
def test_nrel_spa_published_reference_case() -> None:
    """Reda & Andreas (2004) / NREL SPA example, as used in pvlib's own test-suite.

    Input: 2003-10-17 12:30:30 MST (UTC-7) = 19:30:30Z, lat 39.742476 N,
    lon -105.1786 E, elevation 1830.14 m, pressure 820 mbar, temperature 11 degC,
    delta_t = 67 s (pvlib default).
    Published: topocentric zenith (refracted) 50.11162 deg, topocentric azimuth
    194.34024 deg (clockwise from north), topocentric declination -9.316179 deg.
    Tolerance: 0.001 deg for zenith/azimuth (algorithm uncertainty is 0.0003 deg;
    margin for the pvlib delta_t convention), 0.01 deg for declination (we derive it
    from SPA elevation/azimuth, so the parallax-level difference is allowed).
    """
    point = GeoPoint(latitude_deg=39.742476, longitude_deg=-105.1786, elevation_m=1830.14)
    pos = solar_position(
        point,
        datetime(2003, 10, 17, 19, 30, 30, tzinfo=UTC),
        pressure_pa=82000.0,
        temperature_c=11.0,
    )
    assert pos.apparent_zenith_deg == pytest.approx(50.11162, abs=1e-3)
    assert pos.azimuth_deg == pytest.approx(194.34024, abs=1e-3)
    assert pos.apparent_elevation_deg == pytest.approx(90.0 - 50.11162, abs=1e-3)
    assert pos.declination_deg == pytest.approx(-9.316179, abs=1e-2)
    # Refraction raises the apparent Sun: apparent elevation > geometric elevation.
    assert pos.apparent_elevation_deg > pos.elevation_deg


@pytest.mark.science
def test_equator_equinox_solar_noon_sun_near_zenith() -> None:
    """Hand-checkable: at the equator on the March equinox (2026-03-20) the Sun
    culminates within ~0.5 deg of the zenith at local solar noon. The equinox is at
    14:46Z; the declination at transit that day is -0.1 .. +0.1 deg. Tolerance 0.5 deg."""
    equator = GeoPoint(latitude_deg=0.0, longitude_deg=0.0, elevation_m=0.0)
    events = solar_day_events(equator, datetime(2026, 3, 20, 12, 0, tzinfo=UTC))
    assert events.transit_elevation_deg > 89.5
    pos = solar_position(equator, events.solar_noon_utc)
    assert pos.zenith_deg < 0.5
    assert pos.declination_deg is not None and abs(pos.declination_deg) < 0.2


@pytest.mark.science
def test_solar_noon_azimuth_north_and_south_hemisphere() -> None:
    """At the instant of upper transit the Sun is on the meridian: azimuth 180 deg
    in the northern mid-latitudes (Sun due south), 0/360 deg in Alice Springs in
    winter (Sun due north). Tolerance 0.5 deg."""
    ev_n = solar_day_events(JONKOPING, datetime(2026, 6, 21, 12, 0, tzinfo=UTC))
    pos_n = solar_position(JONKOPING, ev_n.solar_noon_utc)
    assert pos_n.azimuth_deg == pytest.approx(180.0, abs=0.5)
    assert pos_n.elevation_deg == pytest.approx(ev_n.transit_elevation_deg, abs=0.01)
    # 90 - 57.7826 + 23.44 = 55.66 deg at the June solstice.
    assert ev_n.transit_elevation_deg == pytest.approx(55.66, abs=0.1)

    ev_s = solar_day_events(ALICE, datetime(2027, 8, 24, 3, 0, tzinfo=UTC))
    pos_s = solar_position(ALICE, ev_s.solar_noon_utc)
    assert _angular_distance(pos_s.azimuth_deg, 0.0) < 0.5


@pytest.mark.science
def test_sunrise_agrees_with_elevation_zero_crossing() -> None:
    """Independent cross-check: at SPA's sunrise/sunset time the geometric Sun
    centre is at -0.8333 deg (tolerance 0.01 deg ~ 3 s of Sun motion). Regression guard
    for the raw pvlib behaviour, which is ~0.2 deg (1 min) off for sunrises that fall on
    the previous UTC date (all Australian sunrises)."""
    for place, when in (
        (ALICE, datetime(2027, 8, 24, 3, 0, tzinfo=UTC)),
        (DARWIN, datetime(2027, 8, 24, 3, 0, tzinfo=UTC)),
        (JONKOPING, datetime(2026, 6, 21, 12, 0, tzinfo=UTC)),
        (JONKOPING, datetime(2026, 12, 21, 12, 0, tzinfo=UTC)),
    ):
        ev = solar_day_events(place, when)
        assert ev.sunrise_utc is not None and ev.sunset_utc is not None
        rise = solar_position(place, ev.sunrise_utc)
        sset = solar_position(place, ev.sunset_utc)
        assert rise.elevation_deg == pytest.approx(-0.8333, abs=0.01)
        assert sset.elevation_deg == pytest.approx(-0.8333, abs=0.01)


# --- Vectorised API ----------------------------------------------------------


def test_solar_positions_matches_scalar_and_keeps_order() -> None:
    t0 = datetime(2027, 8, 24, 0, 0, tzinfo=UTC)
    times = [t0 + timedelta(minutes=30 * i) for i in range(48)]
    many = solar_positions(ALICE, times)
    assert [p.time_utc for p in many] == times
    for i in (0, 13, 30, 47):
        one = solar_position(ALICE, times[i])
        assert many[i].elevation_deg == pytest.approx(one.elevation_deg, abs=1e-9)
        assert many[i].azimuth_deg == pytest.approx(one.azimuth_deg, abs=1e-9)
    # Sun rises then sets: both signs of apparent elevation occur over 24 h.
    assert any(p.is_daylight for p in many) and any(not p.is_daylight for p in many)
    # Air mass is None exactly when the Sun is not up.
    assert all((p.air_mass is None) == (not p.is_daylight) for p in many)


def test_solar_positions_empty() -> None:
    assert solar_positions(ALICE, []) == []


def test_naive_datetime_rejected() -> None:
    with pytest.raises(NaiveDatetimeError):
        solar_position(ALICE, datetime(2027, 8, 24, 3, 0))


def test_unknown_elevation_substitutes_zero_and_says_so() -> None:
    no_elev = GeoPoint(latitude_deg=-23.698, longitude_deg=133.8807)
    pos = solar_position(no_elev, datetime(2027, 8, 24, 3, 0, tzinfo=UTC))
    assert "elevation unknown" in (pos.provenance.notes or "")


def test_provenance_fields() -> None:
    when = datetime(2027, 8, 24, 3, 0, tzinfo=UTC)
    pos = solar_position(ALICE, when)
    p = pos.provenance
    assert (p.provider, p.dataset) == ("rwi", "pvlib")
    assert p.kind is DataKind.PHYSICAL_MODEL
    assert p.model_name == "nrel_numpy"
    assert p.model_version is not None
    assert p.valid_time_utc == when
    assert p.point == ALICE
    assert "altitude_m=545.0" in (p.notes or "")


def test_pressure_changes_only_refraction() -> None:
    when = datetime(2027, 8, 24, 3, 0, tzinfo=UTC)
    a = solar_position(ALICE, when, pressure_pa=101325.0)
    b = solar_position(ALICE, when, pressure_pa=80000.0)
    assert a.elevation_deg == pytest.approx(b.elevation_deg, abs=1e-12)
    assert a.apparent_elevation_deg != b.apparent_elevation_deg


def test_invalid_pressure_rejected() -> None:
    with pytest.raises(ValueError):
        solar_position(ALICE, datetime(2027, 8, 24, 3, 0, tzinfo=UTC), pressure_pa=-5.0)


# --- Sunrise / sunset / solar noon ------------------------------------------


def test_jonkoping_day_length_june_and_december() -> None:
    june = solar_day_events(JONKOPING, datetime(2026, 6, 21, 12, 0, tzinfo=UTC))
    dec = solar_day_events(JONKOPING, datetime(2026, 12, 21, 12, 0, tzinfo=UTC))
    assert june.day_length_s > 17 * 3600.0
    assert dec.day_length_s < 7 * 3600.0
    assert june.timezone_name == "Europe/Stockholm"
    assert june.local_date == "2026-06-21"
    for ev in (june, dec):
        assert ev.sunrise_utc is not None and ev.sunset_utc is not None
        assert ev.sunrise_utc < ev.solar_noon_utc < ev.sunset_utc
        assert not ev.is_polar_day and not ev.is_polar_night


def test_polar_night_at_78n_in_december() -> None:
    svalbard = GeoPoint(latitude_deg=78.2, longitude_deg=15.6, elevation_m=10.0)
    ev = solar_day_events(svalbard, datetime(2026, 12, 21, 12, 0, tzinfo=UTC))
    assert ev.sunrise_utc is None and ev.sunset_utc is None
    assert ev.is_polar_night is True and ev.is_polar_day is False
    assert ev.day_length_s == 0.0
    assert ev.transit_elevation_deg < -0.8333


def test_polar_day_at_78n_in_june() -> None:
    svalbard = GeoPoint(latitude_deg=78.2, longitude_deg=15.6, elevation_m=10.0)
    ev = solar_day_events(svalbard, datetime(2026, 6, 21, 12, 0, tzinfo=UTC))
    assert ev.sunrise_utc is None and ev.sunset_utc is None
    assert ev.is_polar_day is True and ev.is_polar_night is False
    assert ev.day_length_s == 86400.0
    assert ev.transit_elevation_deg > 0.0


def test_local_day_boundary_comes_from_point_timezone() -> None:
    # 2027-08-24 18:00Z is already 03:30 on 25 August in Darwin (UTC+9:30) but still
    # 24 August in UTC: the event date must follow the local calendar.
    when = datetime(2027, 8, 24, 18, 0, tzinfo=UTC)
    ev = solar_day_events(DARWIN, when)
    assert ev.local_date == "2027-08-25"
    assert ev.timezone_name == "Australia/Darwin"
    assert ev.sunrise_utc is not None and ev.sunset_utc is not None
    # Both events fall inside the local day [2027-08-24 14:30Z, 2027-08-25 14:30Z).
    lo = datetime(2027, 8, 24, 14, 30, tzinfo=UTC)
    hi = datetime(2027, 8, 25, 14, 30, tzinfo=UTC)
    assert lo <= ev.sunrise_utc < ev.solar_noon_utc < ev.sunset_utc < hi
    # Darwin in late August: ~11.8 h of daylight.
    assert 11.5 * 3600.0 < ev.day_length_s < 12.2 * 3600.0


def test_day_events_same_for_any_instant_in_local_day() -> None:
    tz = timezone_for_point(ALICE)
    early = datetime(2027, 8, 24, 0, 5, tzinfo=tz)
    late = datetime(2027, 8, 24, 23, 55, tzinfo=tz)
    a = solar_day_events(ALICE, early)
    b = solar_day_events(ALICE, late)
    assert a.local_date == b.local_date == "2027-08-24"
    assert a.sunrise_utc == b.sunrise_utc
    assert a.solar_noon_utc == b.solar_noon_utc


def test_dst_transition_day_does_not_fail() -> None:
    # Stockholm spring-forward, 2026-03-29 (local midnight exists, 02:00 does not).
    ev = solar_day_events(JONKOPING, datetime(2026, 3, 29, 12, 0, tzinfo=UTC))
    assert ev.local_date == "2026-03-29"
    assert ev.sunrise_utc is not None and ev.sunset_utc is not None
    assert 12.5 * 3600.0 < ev.day_length_s < 13.5 * 3600.0  # ~12.9 h


def test_day_events_naive_rejected() -> None:
    with pytest.raises(NaiveDatetimeError):
        solar_day_events(ALICE, datetime(2027, 8, 24, 3, 0))
