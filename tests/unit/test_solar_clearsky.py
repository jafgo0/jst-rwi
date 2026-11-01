"""Tests for rwi.solar.clearsky (Ineichen-Perez). No network access."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from rwi.core.errors import NaiveDatetimeError
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind
from rwi.solar.clearsky import (
    clear_sky_irradiance,
    clear_sky_series,
    extraterrestrial_horizontal_w_m2,
)
from rwi.solar.geometry import solar_day_events, solar_position, solar_positions

JONKOPING = GeoPoint(latitude_deg=57.7826, longitude_deg=14.1618, elevation_m=227.0)
DARWIN = GeoPoint(latitude_deg=-12.4634, longitude_deg=130.8456, elevation_m=30.0)
ALICE = GeoPoint(latitude_deg=-23.6980, longitude_deg=133.8807, elevation_m=545.0)
RACE_NOON = datetime(2027, 8, 24, 3, 0, tzinfo=UTC)


# --- THE CRITICAL NIGHT TEST ------------------------------------------------


def test_jonkoping_midnight_clear_sky_is_exactly_zero() -> None:
    when = datetime(2026, 1, 15, 23, 0, tzinfo=UTC)  # 00:00 local
    pos = solar_position(JONKOPING, when)
    assert pos.apparent_elevation_deg < 0.0
    assert pos.is_daylight is False
    cs = clear_sky_irradiance(JONKOPING, when)
    assert cs.ghi_w_m2 == 0.0
    assert cs.dni_w_m2 == 0.0
    assert cs.dhi_w_m2 == 0.0
    assert cs.kind is DataKind.CLEAR_SKY_MODEL
    assert cs.solar_elevation_deg is not None and cs.solar_elevation_deg < 0.0


def test_darwin_midnight_clear_sky_is_exactly_zero() -> None:
    when = datetime(2027, 8, 23, 14, 30, tzinfo=UTC)  # 00:00 ACST
    assert solar_position(DARWIN, when).is_daylight is False
    cs = clear_sky_irradiance(DARWIN, when)
    assert (cs.ghi_w_m2, cs.dni_w_m2, cs.dhi_w_m2) == (0.0, 0.0, 0.0)


def test_all_night_samples_exactly_zero_over_a_full_day() -> None:
    t0 = datetime(2027, 8, 24, 0, 0, tzinfo=UTC)
    times = [t0 + timedelta(minutes=10 * i) for i in range(144)]
    series = clear_sky_series(ALICE, times)
    positions = solar_positions(ALICE, times)
    night = [
        (c, p) for c, p in zip(series, positions, strict=True) if p.apparent_elevation_deg <= 0
    ]
    assert len(night) > 40
    for c, _ in night:
        assert c.ghi_w_m2 == 0.0 and c.dni_w_m2 == 0.0 and c.dhi_w_m2 == 0.0


# --- Kind / provenance -------------------------------------------------------


def test_clear_sky_is_never_tagged_as_observation() -> None:
    cs = clear_sky_irradiance(ALICE, RACE_NOON)
    assert cs.kind is DataKind.CLEAR_SKY_MODEL
    assert cs.provenance.kind is DataKind.CLEAR_SKY_MODEL
    assert cs.provenance.is_measurement is False
    assert cs.provenance.model_name == "ineichen_perez"
    assert cs.provenance.valid_time_utc == RACE_NOON
    assert "linke_turbidity" in (cs.provenance.notes or "")


# --- Physical sanity ---------------------------------------------------------


def test_race_noon_values_plausible() -> None:
    cs = clear_sky_irradiance(ALICE, RACE_NOON)
    # Alice Springs, late August, Sun ~55 deg up, 545 m, dry clear air:
    # GHI roughly 800-950, DNI roughly 850-1050 W/m2.
    assert 750.0 < cs.ghi_w_m2 < 1000.0
    assert 800.0 < cs.dni_w_m2 < 1100.0
    assert 40.0 < cs.dhi_w_m2 < 200.0
    assert cs.clearness_index is not None and 0.6 < cs.clearness_index < 0.95
    assert cs.solar_elevation_deg is not None and cs.solar_elevation_deg > 50.0


def test_ghi_bounds_dhi_and_extraterrestrial() -> None:
    t0 = datetime(2027, 8, 24, 0, 0, tzinfo=UTC)
    times = [t0 + timedelta(minutes=20 * i) for i in range(72)]
    for c in clear_sky_series(ALICE, times):
        assert c.ghi_w_m2 >= c.dhi_w_m2 >= 0.0
        pos = solar_position(ALICE, c.time_utc)
        if pos.elevation_deg > 5.0:  # near the horizon refraction blurs the comparison
            et = extraterrestrial_horizontal_w_m2(ALICE, c.time_utc)
            assert c.ghi_w_m2 <= et
            assert c.clearness_index is not None and c.clearness_index <= 1.0


def test_ghi_peaks_near_solar_noon() -> None:
    ev = solar_day_events(ALICE, RACE_NOON)
    t0 = datetime(2027, 8, 24, 0, 0, tzinfo=UTC)
    times = [t0 + timedelta(minutes=5 * i) for i in range(288)]
    series = clear_sky_series(ALICE, times)
    peak = max(series, key=lambda c: c.ghi_w_m2)
    assert abs((peak.time_utc - ev.solar_noon_utc).total_seconds()) < 15 * 60
    # Monotonic rise to noon and fall afterwards (smooth day).
    before = [c.ghi_w_m2 for c in series if c.time_utc <= peak.time_utc]
    after = [c.ghi_w_m2 for c in series if peak.time_utc <= c.time_utc <= ev.sunset_utc]
    assert before == sorted(before)
    assert after == sorted(after, reverse=True)


def test_series_matches_single_calls() -> None:
    times = [RACE_NOON + timedelta(hours=h) for h in (-2, 0, 1, 3)]
    series = clear_sky_series(ALICE, times)
    assert [c.time_utc for c in series] == times
    for c, t in zip(series, times, strict=True):
        one = clear_sky_irradiance(ALICE, t)
        assert c.ghi_w_m2 == pytest.approx(one.ghi_w_m2, abs=1e-9)
        assert c.dni_w_m2 == pytest.approx(one.dni_w_m2, abs=1e-9)


def test_series_empty() -> None:
    assert clear_sky_series(ALICE, []) == []


def test_ghi_closure_with_clear_sky_components() -> None:
    cs = clear_sky_irradiance(ALICE, RACE_NOON)
    pos = solar_position(ALICE, RACE_NOON)
    closure = cs.dni_w_m2 * math.cos(math.radians(pos.zenith_deg)) + cs.dhi_w_m2
    assert closure == pytest.approx(cs.ghi_w_m2, rel=0.01)


def test_higher_turbidity_reduces_dni_and_raises_nothing_at_night() -> None:
    clean = clear_sky_irradiance(ALICE, RACE_NOON, linke_turbidity=2.0)
    hazy = clear_sky_irradiance(ALICE, RACE_NOON, linke_turbidity=6.0)
    assert hazy.dni_w_m2 < clean.dni_w_m2
    assert hazy.ghi_w_m2 < clean.ghi_w_m2
    night = clear_sky_irradiance(
        JONKOPING, datetime(2026, 1, 15, 23, 0, tzinfo=UTC), linke_turbidity=6.0
    )
    assert night.ghi_w_m2 == 0.0


def test_supplied_pressure_and_temperature_recorded() -> None:
    cs = clear_sky_irradiance(ALICE, RACE_NOON, pressure_pa=95000.0, temperature_c=25.0)
    notes = cs.provenance.notes or ""
    assert "pressure_pa=95000 (supplied)" in notes
    assert "temperature_c=25.0 (supplied)" in notes


def test_invalid_turbidity_rejected() -> None:
    with pytest.raises(ValueError):
        clear_sky_irradiance(ALICE, RACE_NOON, linke_turbidity=0.5)


def test_bundled_linke_climatology_works_offline() -> None:
    cs = clear_sky_irradiance(ALICE, RACE_NOON)
    assert "bundled climatology" in (cs.provenance.notes or "")
    assert "FALLBACK" not in (cs.provenance.notes or "")


def test_naive_datetime_rejected() -> None:
    with pytest.raises(NaiveDatetimeError):
        clear_sky_irradiance(ALICE, datetime(2027, 8, 24, 3, 0))


# --- Extraterrestrial --------------------------------------------------------


def test_extraterrestrial_zero_at_night_and_positive_by_day() -> None:
    assert (
        extraterrestrial_horizontal_w_m2(JONKOPING, datetime(2026, 1, 15, 23, 0, tzinfo=UTC)) == 0.0
    )
    et = extraterrestrial_horizontal_w_m2(ALICE, RACE_NOON)
    assert 1000.0 < et < 1250.0


@pytest.mark.science
def test_extraterrestrial_matches_solar_constant_geometry() -> None:
    """Equator, March equinox solar noon: Sun at the zenith so ET horizontal equals
    the normal extraterrestrial irradiance = S0 * (r0/r)^2. On 2026-03-20 the Earth-Sun
    distance factor is ~1.0000 +/- 0.005 (near the equinoxes, r ~ 0.996 AU), and the
    NREL/pvlib solar constant is 1366.1 W/m2. Expected 1366 +/- 15 W/m2."""
    equator = GeoPoint(latitude_deg=0.0, longitude_deg=0.0, elevation_m=0.0)
    ev = solar_day_events(equator, datetime(2026, 3, 20, 12, 0, tzinfo=UTC))
    et = extraterrestrial_horizontal_w_m2(equator, ev.solar_noon_utc)
    assert et == pytest.approx(1366.1, abs=15.0)


@pytest.mark.science
def test_clear_sky_ghi_sea_level_sun_overhead_reference_range() -> None:
    """Ineichen-Perez with Linke turbidity 3 (typical rural mid-latitude) and Sun near
    the zenith at sea level gives GHI of roughly 1000-1100 W/m2 (published clear-sky
    peaks for TL=3 are ~1050 W/m2). Range check, not a point-value validation."""
    equator = GeoPoint(latitude_deg=0.0, longitude_deg=0.0, elevation_m=0.0)
    ev = solar_day_events(equator, datetime(2026, 3, 20, 12, 0, tzinfo=UTC))
    cs = clear_sky_irradiance(equator, ev.solar_noon_utc, linke_turbidity=3.0)
    assert 1000.0 < cs.ghi_w_m2 < 1150.0
