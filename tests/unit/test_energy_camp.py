"""Camp-site charging forecast: windows, low-sun energy, gusts, horizon cloud, dew (ADR 0015)."""

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta

import pytest

from rwi.contracts import WeatherSeries, WeatherStep
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance, measured
from rwi.energy.camp import (
    brutsaert_clear_sky_emissivity,
    camp_forecast,
    charge_windows,
    dew_hours,
    downwelling_longwave_w_m2,
    horizon_cloud,
    horizon_points,
    saturation_vapour_pressure_hpa,
    sun_events,
    window_clear_sky,
    window_forecast_energy,
    window_gusts,
)
from rwi.regulations import EVENT_TIMEZONE
from rwi.solar.geometry import solar_positions

ALICE = GeoPoint(latitude_deg=-23.698, longitude_deg=133.8807, elevation_m=545.0)
DAY = date(2027, 8, 24)
NOW = datetime(2027, 8, 24, 0, 0, tzinfo=UTC)
NOON_UTC = datetime(2027, 8, 24, 2, 0, tzinfo=UTC)  # 11:30 ACST


def acst(day: date, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm, ss), tzinfo=EVENT_TIMEZONE).astimezone(UTC)


def series(
    start: datetime,
    n: int,
    values: dict[str, float | Sequence[float | None]],
    *,
    point: GeoPoint = ALICE,
) -> WeatherSeries:
    steps = []
    for i in range(n):
        t = start + timedelta(hours=i)
        prov = Provenance(
            provider="open-meteo",
            dataset="forecast",
            kind=DataKind.FORECAST,
            retrieved_at_utc=NOW,
            valid_time_utc=t,
            model_name="ecmwf_ifs025",
            point=point,
        )
        vals = {}
        for var, v in values.items():
            x = v[i] if isinstance(v, Sequence) else v
            if x is not None:
                vals[var] = measured(var, x, prov)
        steps.append(WeatherStep(valid_time_utc=t, values=vals))
    return WeatherSeries(
        point=point,
        requested_point=point,
        provider="open-meteo",
        dataset="forecast",
        kind=DataKind.FORECAST,
        model_name="ecmwf_ifs025",
        retrieved_at_utc=NOW,
        temporal_resolution_s=3600.0,
        steps=steps,
    )


# --------------------------------------------------------------------------- #
# Dew physics                                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.science
def test_magnus_saturation_vapour_pressure_at_20c() -> None:
    # Alduchov & Eskridge (1996) eq. 21: 6.1094 exp(17.625*20/(20+243.04)) = 23.334 hPa.
    assert saturation_vapour_pressure_hpa(20.0) == pytest.approx(23.334, abs=1e-3)
    assert saturation_vapour_pressure_hpa(0.0) == pytest.approx(6.1094, abs=1e-9)


@pytest.mark.science
def test_brutsaert_emissivity_and_clear_sky_longwave() -> None:
    # Brutsaert (1975): eps = 1.24 (e/T)^(1/7); Td 7 C gives e ~ 10.0 hPa, T 288.15 K
    # -> eps = 0.767; IR = eps * sigma * T^4 = 299.9 W/m2 (hand calculation).
    assert brutsaert_clear_sky_emissivity(15.0, 7.0) == pytest.approx(0.767, abs=1e-3)
    ir, eps = downwelling_longwave_w_m2(15.0, 7.0, 0.0)
    assert eps == pytest.approx(0.767, abs=1e-3)
    assert ir == pytest.approx(299.9, abs=0.2)


@pytest.mark.science
def test_overcast_sky_is_a_blackbody_at_air_temperature() -> None:
    # Crawford & Duchon (1999): eps = eps_clr (1 - c) + c, so c = 1 gives eps = 1.
    ir, eps = downwelling_longwave_w_m2(15.0, 7.0, 1.0)
    assert eps == 1.0
    assert ir == pytest.approx(5.670374419e-8 * 288.15**4, rel=1e-12)
    ir_half, _ = downwelling_longwave_w_m2(15.0, 7.0, 0.5)
    assert 299.9 < ir_half < ir


@pytest.mark.parametrize("bad", [-0.01, 1.01, math.nan])
def test_cloud_fraction_outside_unit_interval_is_rejected(bad: float) -> None:
    with pytest.raises(ValueError, match="cloud_fraction"):
        downwelling_longwave_w_m2(15.0, 7.0, bad)


@pytest.mark.science
def test_clear_calm_night_cools_array_below_air_overcast_does_not() -> None:
    # pvlib faiman_rad (Driesse et al. 2022): with POA 0 the only heat term is net longwave,
    # so a clear sky (eps < 1) pulls the array below air temperature and overcast (eps = 1)
    # leaves it at air temperature.
    start = acst(DAY, 22)
    common = {"temperature_2m": 10.0, "dew_point_2m": 8.0, "wind_speed_10m": 0.5}
    clear, _ = dew_hours(series(start, 1, {**common, "cloud_cover": 0.0}), start, start, 1.0)
    cloudy, _ = dew_hours(series(start, 1, {**common, "cloud_cover": 100.0}), start, start, 1.0)
    assert clear[0].array_temperature_c < 10.0 - 1.0
    assert cloudy[0].array_temperature_c == pytest.approx(10.0, abs=1e-9)
    assert clear[0].margin_c < cloudy[0].margin_c
    assert clear[0].dew_risk is True and cloudy[0].dew_risk is False


def test_dew_hours_skip_incomplete_steps_and_flag_only_when_configured() -> None:
    start = acst(DAY, 22)
    s = series(
        start,
        3,
        {
            "temperature_2m": 10.0,
            "dew_point_2m": [8.0, None, 8.0],
            "wind_speed_10m": 1.0,
            "cloud_cover": 0.0,
        },
    )
    hours, skipped = dew_hours(s, start, start + timedelta(hours=2), None)
    assert skipped == 1 and len(hours) == 2
    assert all(h.dew_risk is None for h in hours)
    with pytest.raises(ValueError, match="camp_dew_margin_c"):
        dew_hours(s, start, start, math.inf)


# --------------------------------------------------------------------------- #
# Window bounds                                                                 #
# --------------------------------------------------------------------------- #


def test_evening_ends_at_sunset_and_morning_starts_at_sunrise() -> None:
    nxt = DAY + timedelta(days=1)
    evening, morning = charge_windows(DAY, acst(DAY, 18, 10), acst(nxt, 6, 55))
    assert evening == (acst(DAY, 17), acst(DAY, 18, 10), "daily_stop", "sunset")
    assert morning == (acst(nxt, 6, 55), acst(nxt, 8), "sunrise", "daily_start")


def test_charging_lockout_caps_late_sunset_and_early_sunrise() -> None:
    nxt = DAY + timedelta(days=1)
    evening, morning = charge_windows(DAY, acst(DAY, 19, 30), acst(nxt, 6, 0))
    assert evening.end_utc == acst(DAY, 19) and evening.end_limited_by == "charging_lockout"
    assert morning.start_utc == acst(nxt, 6, 30)
    assert morning.start_limited_by == "charging_lockout"


def test_no_window_when_sun_is_down_before_stop_or_up_after_start() -> None:
    nxt = DAY + timedelta(days=1)
    evening, morning = charge_windows(DAY, acst(DAY, 16, 30), acst(nxt, 8, 30))
    assert evening.start_utc is None and evening.end_limited_by == "sunset_before_daily_stop"
    assert morning.start_utc is None
    assert morning.start_limited_by == "sunrise_after_daily_start"
    unknown_ev, unknown_mo = charge_windows(DAY, None, None)
    assert unknown_ev.end_limited_by == "sunset_unknown" and unknown_ev.start_utc is None
    assert unknown_mo.start_limited_by == "sunrise_unknown" and unknown_mo.end_utc is None


# --------------------------------------------------------------------------- #
# Energy                                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.science
def test_clear_sky_energy_after_sunset_is_exactly_zero() -> None:
    # CLAUDE.md rule 5: sun below the horizon means direct irradiance exactly 0.0.
    sunset, _ = sun_events(ALICE, DAY)
    assert sunset is not None
    e, el0, el1 = window_clear_sky(
        ALICE, sunset + timedelta(minutes=30), sunset + timedelta(minutes=90), now=NOW
    )
    assert e.dni_wh_m2 == 0.0 and e.ghi_wh_m2 == 0.0
    assert el0 is not None and el1 is not None and el1 < el0 < 0.0
    assert e.provenance.kind is DataKind.CLEAR_SKY_MODEL


@pytest.mark.science
def test_alice_springs_late_august_sun_times_and_low_sun_window() -> None:
    # Sunrise/sunset are defined at geometric elevation -0.833 deg (34' refraction + 16'
    # semidiameter; Reda & Andreas 2004, NREL SPA). Alice Springs 24 Aug 2027: pvlib SPA gives
    # sunset 18:20:51 and sunrise 06:52:34 ACST; sunset is west (az > 270), sunrise east.
    sunset, sunrise = sun_events(ALICE, DAY)
    assert sunset is not None and sunrise is not None
    assert abs(sunset - acst(DAY, 18, 20, 51)) < timedelta(seconds=5)
    assert abs(sunrise - acst(DAY + timedelta(days=1), 6, 52, 34)) < timedelta(seconds=5)
    ss, sr = solar_positions(ALICE, [sunset, sunrise])
    assert ss.elevation_deg == pytest.approx(-0.8333, abs=0.01)
    assert sr.elevation_deg == pytest.approx(-0.8333, abs=0.01)
    assert 270.0 < ss.azimuth_deg < 290.0 and 70.0 < sr.azimuth_deg < 90.0
    camp = camp_forecast(ALICE, DAY, now=NOW, stop_series=None)
    ev = camp.evening
    assert ev.end_limited_by == "sunset" and ev.clear_sky is not None
    assert ev.solar_elevation_start_deg is not None and 15.0 < ev.solar_elevation_start_deg < 19.0
    assert ev.solar_elevation_end_deg is not None and abs(ev.solar_elevation_end_deg) < 1.5
    assert ev.clear_sky.ghi_wh_m2 is not None and 50.0 < ev.clear_sky.ghi_wh_m2 < 300.0
    assert ev.forecast is None and "no forecast at the stop: clear-sky only" in ev.notes
    assert camp.dew is None and camp.horizon_west == []
    assert "no forecast at the stop: no gusts, horizon or dew assessment" in camp.notes


def test_forecast_energy_full_hours_is_mean_times_hours() -> None:
    s = series(NOON_UTC + timedelta(hours=1), 4, {"ghi": 300.0, "dni": 500.0})
    e = window_forecast_energy(ALICE, NOON_UTC, NOON_UTC + timedelta(hours=3), s)
    assert e is not None
    assert e.ghi_wh_m2 == pytest.approx(900.0, rel=1e-12)
    assert e.dni_wh_m2 == pytest.approx(1500.0, rel=1e-12)
    assert e.provenance.kind is DataKind.FORECAST


def test_forecast_energy_partial_hour_is_weighted_by_clear_sky_shape() -> None:
    s = series(NOON_UTC + timedelta(hours=1), 3, {"ghi": 300.0, "dni": 500.0})
    e = window_forecast_energy(
        ALICE, NOON_UTC + timedelta(minutes=30), NOON_UTC + timedelta(hours=3), s
    )
    assert e is not None and e.ghi_wh_m2 is not None
    assert 600.0 + 100.0 < e.ghi_wh_m2 < 900.0 - 100.0


def test_forecast_energy_missing_hour_gives_none_for_that_variable() -> None:
    s = series(NOON_UTC + timedelta(hours=1), 3, {"ghi": [300.0, None, 300.0], "dni": 500.0})
    e = window_forecast_energy(ALICE, NOON_UTC, NOON_UTC + timedelta(hours=3), s)
    assert e is not None and e.ghi_wh_m2 is None
    assert e.dni_wh_m2 == pytest.approx(1500.0, rel=1e-12)
    assert (
        window_forecast_energy(
            ALICE,
            NOON_UTC,
            NOON_UTC + timedelta(hours=3),
            series(NOON_UTC, 3, {"temperature_2m": 20.0}),
        )
        is None
    )


# --------------------------------------------------------------------------- #
# Gusts and horizon                                                             #
# --------------------------------------------------------------------------- #


def test_gust_peak_and_threshold_flag() -> None:
    start = acst(DAY, 17)
    s = series(start + timedelta(hours=1), 4, {"wind_gusts_10m": [5.0, 12.0, 8.0, 30.0]})
    end = start + timedelta(hours=3)
    assert window_gusts(start, end, s, 10.0) == (12.0, True)
    assert window_gusts(start, end, s, 15.0) == (12.0, False)
    assert window_gusts(start, end, s, None) == (12.0, None)
    for bad in (0.0, -1.0, math.nan):
        with pytest.raises(ValueError, match="camp_gust_warning_m_s"):
            window_gusts(start, end, s, bad)


def test_horizon_points_go_along_the_azimuth() -> None:
    [(d, p)] = horizon_points(ALICE, 270.0, [100.0])
    assert d == 100.0
    assert p.latitude_deg == pytest.approx(ALICE.latitude_deg, abs=0.05)
    dlon_km = (ALICE.longitude_deg - p.longitude_deg) * 111.32 * math.cos(math.radians(23.7))
    assert dlon_km == pytest.approx(100.0, rel=0.01)
    with pytest.raises(ValueError, match="camp_horizon_offsets_km"):
        horizon_points(ALICE, 270.0, [50.0, 0.0])


def test_horizon_cloud_uses_nearest_step_within_half_resolution() -> None:
    t0 = acst(DAY, 18)
    s = series(t0, 2, {"cloud_cover": [40.0, 90.0], "cloud_cover_low": [10.0, 60.0]})
    near = horizon_cloud("west", 100.0, 265.0, ALICE, t0 + timedelta(minutes=20), s)
    assert near.cloud_cover_pct == 40.0 and near.cloud_cover_low_pct == 10.0
    assert near.cloud_cover_high_pct is None and near.valid_time_utc == t0
    far = horizon_cloud("west", 100.0, 265.0, ALICE, t0 + timedelta(hours=3), s)
    assert far.cloud_cover_pct is None and far.valid_time_utc is None
    assert far.provenance is None
    none = horizon_cloud("east", 100.0, 85.0, ALICE, t0, None)
    assert none.provenance is None and none.cloud_cover_pct is None


# --------------------------------------------------------------------------- #
# Assembly                                                                      #
# --------------------------------------------------------------------------- #


def test_camp_forecast_with_series_has_forecast_gusts_and_dew() -> None:
    start = acst(DAY, 16)
    s = series(
        start,
        17,
        {
            "ghi": 200.0,
            "dni": 400.0,
            "wind_gusts_10m": 9.0,
            "temperature_2m": 8.0,
            "dew_point_2m": 6.0,
            "wind_speed_10m": 1.0,
            "cloud_cover": 0.0,
        },
    )
    camp = camp_forecast(ALICE, DAY, now=NOW, stop_series=s, gust_warning_m_s=8.0, dew_margin_c=0.0)
    assert camp.evening.forecast is not None and camp.evening.forecast.ghi_wh_m2 is not None
    assert camp.evening.gust_max_m_s == 9.0 and camp.evening.gust_warning is True
    assert camp.morning.forecast is not None
    assert camp.dew is not None and camp.dew.hours
    assert camp.dew.min_margin_c is not None and camp.dew.min_margin_c < 0.0
    assert camp.dew.first_risk_utc is not None
    assert camp.dew.risk_at_sunrise is True
    assert camp.notes == []


def test_camp_forecast_instant_radiation_skips_forecast_energy() -> None:
    s = series(acst(DAY, 16), 17, {"ghi": 200.0, "dni": 400.0, "wind_gusts_10m": 5.0})
    camp = camp_forecast(ALICE, DAY, now=NOW, stop_series=s, forecast_is_hour_mean=False)
    assert camp.evening.forecast is None
    assert any("not a preceding-hour mean" in n for n in camp.evening.notes)
    assert any("RWI_CAMP_GUST_WARNING_M_S" in n for n in camp.evening.notes)
    assert camp.evening.gust_warning is None
