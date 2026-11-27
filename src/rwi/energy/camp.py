"""Camp-site charging forecast for a candidate overnight stop (ADR 0015).

The car stops for the night at about 17:00 event time and may charge from the
stationary array until the sun sets or the 19:00 charging lockout starts, then
again from sunrise or the 06:30 lockout end until the 08:00 start (regulations
clauses 3.17.2, 3.21, 3.27.9; :data:`rwi.regulations.SCHEDULE`). This module
evaluates one candidate stop. Choosing where to stop is the external strategy
tool's job; nothing here ranks or picks a stop.

What is computed, per window
----------------------------
* Clear-sky GHI/DNI/DHI (Ineichen-Perez, ``CLEAR_SKY_MODEL``) sampled every
  :data:`SAMPLE_STEP_S` and integrated to Wh/m2 (trapezoid). Sun below the
  horizon gives exactly 0.0 (hard rule 5), so the samples need no clamping.
* Forecast GHI/DNI energy (``FORECAST``) from the provider's preceding-hour
  means: each provider hour ``(t-1h, t]`` contributes
  ``mean * 1 h * E_cs(hour ∩ window) / E_cs(hour)``, i.e. the hour's forecast
  energy is spread in time with the clear-sky shape (clear-sky-index
  interpolation). The weight is in [0, 1], so no threshold on a small clear-sky
  denominator is needed; an hour with zero clear-sky energy contributes 0.
* The strongest 10 m gust of the provider hours overlapping the window, and a
  warning flag when it reaches the configured threshold. The 10 m gust is
  stronger than the gust at array height, so the flag errs on the safe side.

Horizon cloud
-------------
Low sun is blocked by cloud far along the line of sight, not overhead. Cloud
cover is read at points a configured distance from the stop along the sunset
azimuth (west) and the next sunrise azimuth (east), at the provider step
nearest sunset / sunrise.

Dew on the array at dawn
------------------------
At night a horizontal array radiates to a sky colder than the air and cools
below air temperature; dew forms when it reaches the dew point. Array
temperature per forecast hour from sunset to the morning window comes from
``pvlib.temperature.faiman_rad`` (Driesse, Stein & Theristis 2022, "Global
horizontal irradiance ... Faiman model with radiative loss", pvlib; Faiman 2008
coefficients u0 = 25, u1 = 6.84; emissivity 0.88 and sky view 1.0 are pvlib's
defaults, array assumed horizontal) with POA = 0 and downwelling longwave
``IR_down = eps_sky * sigma * T_air^4`` where

* clear-sky emissivity ``eps_clr = 1.24 (e / T_air)^(1/7)``, e in hPa, T in K
  (Brutsaert 1975, Water Resour. Res. 11(5), 742-744, doi:10.1029/WR011i005p00742);
* e is the saturation vapour pressure at the dew point, Magnus form with
  Alduchov & Eskridge (1996) coefficients (J. Appl. Meteor. 35, 601-609);
* cloud correction ``eps_sky = eps_clr (1 - c) + c`` with c the total cloud
  fraction (Crawford & Duchon 1999, J. Appl. Meteor. 38, 474-480). Crawford &
  Duchon derived c from a solar deficit; here it is the NWP cloud cover, a
  documented adaptation.

Wind is the 10 m value times the Faiman array-height factor of
:mod:`rwi.energy.thermal`. Dew risk is ``T_array - T_dew <= margin`` with the
margin from config; there is no default margin.
"""

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Literal, NamedTuple

import numpy as np
import pvlib
from pydantic import BaseModel, ConfigDict, Field

from rwi.contracts import WeatherSeries
from rwi.core.geo import GeoPoint, destination_point
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import ensure_utc
from rwi.energy.thermal import wind_height_factor_for
from rwi.regulations import EVENT_TIMEZONE, SCHEDULE, RaceSchedule
from rwi.solar.clearsky import clear_sky_series
from rwi.solar.geometry import solar_day_events, solar_positions

__all__ = [
    "SAMPLE_STEP_S",
    "CampDew",
    "CampForecast",
    "ChargeWindow",
    "DewHour",
    "HorizonCloud",
    "WindowBounds",
    "WindowEnergy",
    "brutsaert_clear_sky_emissivity",
    "camp_forecast",
    "charge_windows",
    "dew_hours",
    "downwelling_longwave_w_m2",
    "horizon_cloud",
    "horizon_points",
    "saturation_vapour_pressure_hpa",
    "sun_azimuth_deg",
    "sun_events",
    "window_clear_sky",
    "window_forecast_energy",
    "window_gusts",
]

#: Clear-sky sampling step inside a window [s]; a numerical choice, not physics.
SAMPLE_STEP_S = 60.0
_HOUR = timedelta(hours=1)
_SIGMA = 5.670374419e-8  # Stefan-Boltzmann [W m-2 K-4], CODATA 2018
_KELVIN = 273.15
# Magnus coefficients over water, Alduchov & Eskridge (1996), eq. 21.
_MAGNUS_A_HPA, _MAGNUS_B, _MAGNUS_C_DEGC = 6.1094, 17.625, 243.04
_BRUTSAERT_COEF, _BRUTSAERT_EXP = 1.24, 1.0 / 7.0
# pvlib.temperature.faiman_rad defaults (Driesse et al. 2022 / Faiman 2008).
_FAIMAN_U0, _FAIMAN_U1, _ARRAY_EMISSIVITY, _ARRAY_SKY_VIEW = 25.0, 6.84, 0.88, 1.0

WindowLabel = Literal["evening", "morning"]


class WindowBounds(NamedTuple):
    """UTC bounds of one charging window and what limits each end; None bounds = no window."""

    start_utc: datetime | None
    end_utc: datetime | None
    start_limited_by: str
    end_limited_by: str


class _CsEnergy(NamedTuple):
    ghi: float
    dni: float
    dhi: float
    elevation_deg: list[float | None]


class WindowEnergy(BaseModel):
    """Irradiance energy over a window [Wh/m2] with its provenance."""

    model_config = ConfigDict(frozen=True)

    ghi_wh_m2: float | None
    dni_wh_m2: float | None
    dhi_wh_m2: float | None = None
    provenance: Provenance


class ChargeWindow(BaseModel):
    """One charging window at the stop: limits, irradiance energy and gusts."""

    model_config = ConfigDict(frozen=True)

    label: WindowLabel
    start_utc: datetime | None
    end_utc: datetime | None
    start_limited_by: str
    end_limited_by: str
    duration_s: float = Field(ge=0.0)
    solar_elevation_start_deg: float | None = None
    solar_elevation_end_deg: float | None = None
    clear_sky: WindowEnergy | None = None
    forecast: WindowEnergy | None = None
    gust_max_m_s: float | None = None
    gust_warning: bool | None = Field(
        default=None, description="None when the gust threshold is not configured"
    )
    gust_provenance: Provenance | None = None
    notes: list[str] = Field(default_factory=list)


class HorizonCloud(BaseModel):
    """Cloud cover [%] at a point along the sunset or sunrise azimuth."""

    model_config = ConfigDict(frozen=True)

    side: Literal["west", "east"]
    offset_km: float
    azimuth_deg: float
    point: GeoPoint
    valid_time_utc: datetime | None
    cloud_cover_pct: float | None
    cloud_cover_low_pct: float | None
    cloud_cover_mid_pct: float | None
    cloud_cover_high_pct: float | None
    provenance: Provenance | None


class DewHour(BaseModel):
    """Modelled array temperature vs dew point at one forecast hour [degC]."""

    model_config = ConfigDict(frozen=True)

    valid_time_utc: datetime
    air_temperature_c: float
    dew_point_c: float
    array_temperature_c: float
    margin_c: float = Field(description="array temperature minus dew point")
    sky_emissivity: float
    dew_risk: bool | None


class CampDew(BaseModel):
    """Night-time dew risk on the array, hour by hour, and at sunrise."""

    model_config = ConfigDict(frozen=True)

    margin_threshold_c: float | None
    hours: list[DewHour]
    min_margin_c: float | None
    first_risk_utc: datetime | None
    risk_at_sunrise: bool | None = Field(
        default=None, description="dew risk at the forecast step nearest sunrise"
    )
    provenance: Provenance | None
    notes: list[str] = Field(default_factory=list)


class CampForecast(BaseModel):
    """Charging outlook for one candidate overnight stop."""

    model_config = ConfigDict(frozen=True)

    route_id: str | None
    distance_along_m: float | None
    point: GeoPoint
    stop_date_event: date = Field(description="event-time (ACST) calendar date of the stop")
    sunset_utc: datetime | None
    sunrise_utc: datetime | None
    evening: ChargeWindow
    morning: ChargeWindow
    horizon_west: list[HorizonCloud] = Field(default_factory=list)
    horizon_east: list[HorizonCloud] = Field(default_factory=list)
    dew: CampDew | None = None
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Windows                                                                       #
# --------------------------------------------------------------------------- #


def _event_utc(day: date, clock: object, schedule: RaceSchedule) -> datetime:
    del schedule
    return datetime.combine(day, clock, tzinfo=EVENT_TIMEZONE).astimezone(UTC)  # type: ignore[arg-type]


def charge_windows(
    stop_date: date,
    sunset_utc: datetime | None,
    sunrise_utc: datetime | None,
    schedule: RaceSchedule = SCHEDULE,
) -> tuple[WindowBounds, WindowBounds]:
    """Evening and morning charging-window bounds at the stop, UTC (regulations SCHEDULE).

    Evening: ``daily_stop`` on ``stop_date`` to ``min(sunset, lockout start)``.
    Morning: ``max(sunrise, lockout end)`` to ``daily_start`` the next day. A
    window whose end is not after its start, or whose sun event is unknown,
    has None bounds.
    """
    ev_start = _event_utc(stop_date, schedule.daily_stop, schedule)
    lock_start = _event_utc(stop_date, schedule.charging_lockout_start, schedule)
    next_day = stop_date + timedelta(days=1)
    lock_end = _event_utc(next_day, schedule.charging_lockout_end, schedule)
    mo_end = _event_utc(next_day, schedule.daily_start, schedule)

    if sunset_utc is None:
        evening = WindowBounds(None, None, "daily_stop", "sunset_unknown")
    else:
        ss = ensure_utc(sunset_utc)
        end, end_by = (ss, "sunset") if ss <= lock_start else (lock_start, "charging_lockout")
        evening = (
            WindowBounds(ev_start, end, "daily_stop", end_by)
            if end > ev_start
            else WindowBounds(None, None, "daily_stop", f"{end_by}_before_daily_stop")
        )
    if sunrise_utc is None:
        morning = WindowBounds(None, None, "sunrise_unknown", "daily_start")
    else:
        sr = ensure_utc(sunrise_utc)
        begin, start_by = (sr, "sunrise") if sr >= lock_end else (lock_end, "charging_lockout")
        morning = (
            WindowBounds(begin, mo_end, start_by, "daily_start")
            if mo_end > begin
            else WindowBounds(None, None, f"{start_by}_after_daily_start", "daily_start")
        )
    return evening, morning


def _samples(start: datetime, end: datetime) -> list[datetime]:
    n = max(math.ceil((end - start).total_seconds() / SAMPLE_STEP_S), 1)
    step = (end - start) / n
    return [start + step * i for i in range(n + 1)]


def _trapz_wh(times: Sequence[datetime], values: Sequence[float]) -> float:
    if len(times) < 2:
        return 0.0
    t = np.array([(x - times[0]).total_seconds() for x in times], dtype=float)
    return float(np.trapezoid(np.asarray(values, dtype=float), t) / 3600.0)


def _clear_sky_energy(point: GeoPoint, start: datetime, end: datetime) -> _CsEnergy:
    times = _samples(start, end)
    cs = clear_sky_series(point, times)
    return _CsEnergy(
        ghi=_trapz_wh(times, [c.ghi_w_m2 for c in cs]),
        dni=_trapz_wh(times, [c.dni_w_m2 for c in cs]),
        dhi=_trapz_wh(times, [c.dhi_w_m2 for c in cs]),
        elevation_deg=[c.solar_elevation_deg for c in cs],
    )


def window_clear_sky(
    point: GeoPoint, start: datetime, end: datetime, *, now: datetime
) -> tuple[WindowEnergy, float | None, float | None]:
    """Clear-sky energy [Wh/m2] over the window (CLEAR_SKY_MODEL) and end-point elevations."""
    e = _clear_sky_energy(point, start, end)
    prov = Provenance(
        provider="rwi",
        dataset="pvlib",
        kind=DataKind.CLEAR_SKY_MODEL,
        retrieved_at_utc=now,
        valid_time_utc=start,
        model_name="ineichen_perez",
        point=point,
        temporal_resolution_s=SAMPLE_STEP_S,
        processing="trapezoid integration of instantaneous samples",
        notes=(
            f"clear-sky energy {start.isoformat()} to {end.isoformat()} [Wh/m2]; "
            "monthly SoDa Linke turbidity; not a forecast"
        ),
    )
    energy = WindowEnergy(ghi_wh_m2=e.ghi, dni_wh_m2=e.dni, dhi_wh_m2=e.dhi, provenance=prov)
    return energy, e.elevation_deg[0], e.elevation_deg[-1]


def window_forecast_energy(
    point: GeoPoint, start: datetime, end: datetime, series: WeatherSeries
) -> WindowEnergy | None:
    """Forecast GHI/DNI energy [Wh/m2] from preceding-hour means, clear-sky weighted (FORECAST)."""
    totals: dict[str, float] = {"ghi": 0.0, "dni": 0.0}
    used: dict[str, int] = {"ghi": 0, "dni": 0}
    missing: dict[str, int] = {"ghi": 0, "dni": 0}
    for step in series.steps:
        t = ensure_utc(step.valid_time_utc)
        h0, h1 = t - _HOUR, t
        lo, hi = max(h0, start), min(h1, end)
        if hi <= lo:
            continue
        full = _clear_sky_energy(point, h0, h1)
        part = _clear_sky_energy(point, lo, hi)
        for var in ("ghi", "dni"):
            mean = step.value(var)
            if mean is None:
                missing[var] += 1
                continue
            e_full, e_part = getattr(full, var), getattr(part, var)
            weight = e_part / e_full if e_full > 0.0 else 0.0
            totals[var] += mean * 1.0 * weight
            used[var] += 1
    if not used["ghi"] and not used["dni"]:
        return None
    base = series.base_provenance()
    notes = (
        f"forecast energy {start.isoformat()} to {end.isoformat()} [Wh/m2] from preceding-hour "
        "means spread with the clear-sky shape; hours used ghi "
        f"{used['ghi']} (missing {missing['ghi']}), dni {used['dni']} (missing {missing['dni']})"
    )
    prov = base.model_copy(update={"valid_time_utc": start, "notes": notes})
    return WindowEnergy(
        ghi_wh_m2=totals["ghi"] if used["ghi"] and not missing["ghi"] else None,
        dni_wh_m2=totals["dni"] if used["dni"] and not missing["dni"] else None,
        provenance=prov,
    )


def window_gusts(
    start: datetime, end: datetime, series: WeatherSeries, threshold_m_s: float | None
) -> tuple[float | None, bool | None]:
    """Strongest 10 m gust [m/s] of provider hours overlapping the window, and the warning flag."""
    if threshold_m_s is not None and (not math.isfinite(threshold_m_s) or threshold_m_s <= 0.0):
        raise ValueError(f"camp_gust_warning_m_s must be positive, got {threshold_m_s!r}")
    gusts = []
    for step in series.steps:
        t = ensure_utc(step.valid_time_utc)
        if min(t, end) > max(t - _HOUR, start):
            g = step.value("wind_gusts_10m")
            if g is not None:
                gusts.append(g)
    if not gusts:
        return None, None
    peak = max(gusts)
    return peak, (None if threshold_m_s is None else peak >= threshold_m_s)


# --------------------------------------------------------------------------- #
# Horizon cloud                                                                 #
# --------------------------------------------------------------------------- #


def horizon_points(
    point: GeoPoint, azimuth_deg: float, offsets_km: Sequence[float]
) -> list[tuple[float, GeoPoint]]:
    """(offset_km, point) along ``azimuth_deg`` from the stop, one per offset."""
    out = []
    for d in offsets_km:
        if not math.isfinite(d) or d <= 0.0:
            raise ValueError(f"camp_horizon_offsets_km must be positive, got {d!r}")
        out.append((float(d), destination_point(point, azimuth_deg, d * 1000.0)))
    return out


def horizon_cloud(
    side: Literal["west", "east"],
    offset_km: float,
    azimuth_deg: float,
    point: GeoPoint,
    at_utc: datetime,
    series: WeatherSeries | None,
) -> HorizonCloud:
    """Cloud cover at the provider step nearest ``at_utc`` (within half a step), else None."""
    step = None
    if series is not None and series.steps:
        half = timedelta(seconds=(series.temporal_resolution_s or 3600.0) / 2.0)
        best = min(series.steps, key=lambda s: abs(ensure_utc(s.valid_time_utc) - at_utc))
        if abs(ensure_utc(best.valid_time_utc) - at_utc) <= half:
            step = best

    def v(name: str) -> float | None:
        return step.value(name) if step is not None else None

    return HorizonCloud(
        side=side,
        offset_km=offset_km,
        azimuth_deg=azimuth_deg,
        point=point,
        valid_time_utc=step.valid_time_utc if step is not None else None,
        cloud_cover_pct=v("cloud_cover"),
        cloud_cover_low_pct=v("cloud_cover_low"),
        cloud_cover_mid_pct=v("cloud_cover_mid"),
        cloud_cover_high_pct=v("cloud_cover_high"),
        provenance=(
            series.base_provenance().model_copy(update={"valid_time_utc": step.valid_time_utc})
            if series is not None and step is not None
            else None
        ),
    )


# --------------------------------------------------------------------------- #
# Dew                                                                           #
# --------------------------------------------------------------------------- #


def saturation_vapour_pressure_hpa(temperature_c: float) -> float:
    """Saturation vapour pressure over water [hPa], Magnus form (Alduchov & Eskridge 1996)."""
    return _MAGNUS_A_HPA * math.exp(_MAGNUS_B * temperature_c / (temperature_c + _MAGNUS_C_DEGC))


def brutsaert_clear_sky_emissivity(air_temperature_c: float, dew_point_c: float) -> float:
    """Clear-sky effective emissivity, ``1.24 (e/T)^(1/7)`` (Brutsaert 1975)."""
    e = saturation_vapour_pressure_hpa(dew_point_c)
    return _BRUTSAERT_COEF * (e / (air_temperature_c + _KELVIN)) ** _BRUTSAERT_EXP


def downwelling_longwave_w_m2(
    air_temperature_c: float, dew_point_c: float, cloud_fraction: float
) -> tuple[float, float]:
    """(IR_down [W/m2], sky emissivity): Brutsaert clear sky, Crawford & Duchon cloud term."""
    if not 0.0 <= cloud_fraction <= 1.0:
        raise ValueError(f"cloud_fraction must be in [0, 1], got {cloud_fraction!r}")
    eps_clr = brutsaert_clear_sky_emissivity(air_temperature_c, dew_point_c)
    eps = min(eps_clr * (1.0 - cloud_fraction) + cloud_fraction, 1.0)
    return eps * _SIGMA * (air_temperature_c + _KELVIN) ** 4, eps


def dew_hours(
    series: WeatherSeries,
    start: datetime,
    end: datetime,
    margin_c: float | None,
) -> tuple[list[DewHour], int]:
    """Array temperature vs dew point at each forecast step in [start, end]; skipped count."""
    if margin_c is not None and not math.isfinite(margin_c):
        raise ValueError(f"camp_dew_margin_c must be finite, got {margin_c!r}")
    factor = wind_height_factor_for("faiman")
    out: list[DewHour] = []
    skipped = 0
    for step in series.steps:
        t = ensure_utc(step.valid_time_utc)
        if t < start or t > end:
            continue
        ta, td = step.value("temperature_2m"), step.value("dew_point_2m")
        ws, cc = step.value("wind_speed_10m"), step.value("cloud_cover")
        if ta is None or td is None or ws is None or cc is None:
            skipped += 1
            continue
        ir_down, eps = downwelling_longwave_w_m2(ta, td, cc / 100.0)
        t_array = float(
            pvlib.temperature.faiman_rad(
                0.0,
                ta,
                ws * factor,
                ir_down=ir_down,
                u0=_FAIMAN_U0,
                u1=_FAIMAN_U1,
                sky_view=_ARRAY_SKY_VIEW,
                emissivity=_ARRAY_EMISSIVITY,
            )
        )
        margin = t_array - td
        out.append(
            DewHour(
                valid_time_utc=t,
                air_temperature_c=ta,
                dew_point_c=td,
                array_temperature_c=t_array,
                margin_c=margin,
                sky_emissivity=eps,
                dew_risk=None if margin_c is None else margin <= margin_c,
            )
        )
    return out, skipped


def _camp_dew(
    series: WeatherSeries,
    start: datetime,
    end: datetime,
    sunrise_utc: datetime | None,
    margin_c: float | None,
) -> CampDew:
    hours, skipped = dew_hours(series, start, end, margin_c)
    notes = [
        "array temperature: pvlib faiman_rad, POA 0, horizontal array, emissivity 0.88; "
        "IR_down from Brutsaert (1975) and Crawford & Duchon (1999) cloud term"
    ]
    if skipped:
        notes.append(f"{skipped} forecast step(s) skipped: missing T, dew point, wind or cloud")
    if margin_c is None:
        notes.append("dew risk flags off: RWI_CAMP_DEW_MARGIN_C not configured")
    risk_at_sunrise = None
    if sunrise_utc is not None and hours and margin_c is not None:
        half = timedelta(seconds=(series.temporal_resolution_s or 3600.0) / 2.0)
        near = min(hours, key=lambda h: abs(h.valid_time_utc - sunrise_utc))
        if abs(near.valid_time_utc - sunrise_utc) <= half:
            risk_at_sunrise = near.dew_risk
    first = next((h.valid_time_utc for h in hours if h.dew_risk), None)
    prov = series.base_provenance().model_copy(
        update={"notes": f"dew inputs T, Td, wind 10 m, cloud cover; {series.provider}"}
    )
    return CampDew(
        margin_threshold_c=margin_c,
        hours=hours,
        min_margin_c=min((h.margin_c for h in hours), default=None),
        first_risk_utc=first,
        risk_at_sunrise=risk_at_sunrise,
        provenance=prov,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# Assembly                                                                      #
# --------------------------------------------------------------------------- #


def _window(
    label: WindowLabel,
    bounds: WindowBounds,
    point: GeoPoint,
    series: WeatherSeries | None,
    gust_threshold_m_s: float | None,
    now: datetime,
    forecast_is_hour_mean: bool,
) -> ChargeWindow:
    start, end, start_by, end_by = bounds
    if start is None or end is None:
        return ChargeWindow(
            label=label,
            start_utc=None,
            end_utc=None,
            start_limited_by=start_by,
            end_limited_by=end_by,
            duration_s=0.0,
            notes=["no charging window"],
        )
    cs, e0, e1 = window_clear_sky(point, start, end, now=now)
    notes = []
    fc = gust = warn = gust_prov = None
    if series is None:
        notes.append("no forecast at the stop: clear-sky only")
    else:
        if not forecast_is_hour_mean:
            notes.append("forecast radiation is not a preceding-hour mean: forecast energy skipped")
        else:
            fc = window_forecast_energy(point, start, end, series)
            if fc is None:
                notes.append("forecast has no GHI/DNI for this window")
        gust, warn = window_gusts(start, end, series, gust_threshold_m_s)
        gust_prov = series.base_provenance() if gust is not None else None
        if gust_threshold_m_s is None:
            notes.append("gust warning off: RWI_CAMP_GUST_WARNING_M_S not configured")
    return ChargeWindow(
        label=label,
        start_utc=start,
        end_utc=end,
        start_limited_by=start_by,
        end_limited_by=end_by,
        duration_s=(end - start).total_seconds(),
        solar_elevation_start_deg=e0,
        solar_elevation_end_deg=e1,
        clear_sky=cs,
        forecast=fc,
        gust_max_m_s=gust,
        gust_warning=warn,
        gust_provenance=gust_prov,
        notes=notes,
    )


def sun_events(point: GeoPoint, stop_date: date) -> tuple[datetime | None, datetime | None]:
    """(sunset on ``stop_date``, sunrise the next morning) at ``point``, UTC (pvlib SPA)."""
    noon = datetime.combine(stop_date, datetime.min.time(), tzinfo=EVENT_TIMEZONE) + timedelta(
        hours=12
    )
    sunset = solar_day_events(point, noon).sunset_utc
    sunrise = solar_day_events(point, noon + timedelta(days=1)).sunrise_utc
    return sunset, sunrise


def sun_azimuth_deg(point: GeoPoint, when: datetime) -> float:
    """Solar azimuth [deg from north, clockwise] at ``when`` (pvlib SPA)."""
    return float(solar_positions(point, [when])[0].azimuth_deg)


def camp_forecast(
    point: GeoPoint,
    stop_date: date,
    *,
    now: datetime,
    stop_series: WeatherSeries | None,
    horizon_west: Sequence[tuple[float, GeoPoint, WeatherSeries | None]] = (),
    horizon_east: Sequence[tuple[float, GeoPoint, WeatherSeries | None]] = (),
    gust_warning_m_s: float | None = None,
    dew_margin_c: float | None = None,
    forecast_is_hour_mean: bool = True,
    route_id: str | None = None,
    distance_along_m: float | None = None,
    schedule: RaceSchedule = SCHEDULE,
) -> CampForecast:
    """Camp charging outlook at one stop; clear-sky always, forecast parts where series exist."""
    sunset, sunrise = sun_events(point, stop_date)
    evening_b, morning_b = charge_windows(stop_date, sunset, sunrise, schedule)
    evening = _window(
        "evening", evening_b, point, stop_series, gust_warning_m_s, now, forecast_is_hour_mean
    )
    morning = _window(
        "morning", morning_b, point, stop_series, gust_warning_m_s, now, forecast_is_hour_mean
    )
    west = (
        [
            horizon_cloud("west", d, sun_azimuth_deg(point, sunset), p, sunset, s)
            for d, p, s in horizon_west
        ]
        if sunset is not None
        else []
    )
    east = (
        [
            horizon_cloud("east", d, sun_azimuth_deg(point, sunrise), p, sunrise, s)
            for d, p, s in horizon_east
        ]
        if sunrise is not None
        else []
    )
    dew = None
    notes = []
    if stop_series is not None and sunset is not None:
        # POA 0 holds until sunrise; half a step past it keeps the step nearest sunrise.
        half = timedelta(seconds=(stop_series.temporal_resolution_s or 3600.0) / 2.0)
        night_end = (sunrise or sunset + timedelta(hours=12)) + half
        dew = _camp_dew(stop_series, sunset, night_end, sunrise, dew_margin_c)
    elif stop_series is None:
        notes.append("no forecast at the stop: no gusts, horizon or dew assessment")
    return CampForecast(
        route_id=route_id,
        distance_along_m=distance_along_m,
        point=point,
        stop_date_event=stop_date,
        sunset_utc=sunset,
        sunrise_utc=sunrise,
        evening=evening,
        morning=morning,
        horizon_west=west,
        horizon_east=east,
        dew=dew,
        notes=notes,
    )
