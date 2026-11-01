"""Solar geometry: position of the Sun and sunrise / sunset / solar noon.

Model: NREL Solar Position Algorithm (Reda & Andreas 2004, Solar Energy 76(5),
577-589; corrigendum Solar Energy 81(6), 838, 2007), as implemented in
``pvlib.solarposition`` (``get_solarposition(method="nrel_numpy")`` and
``sun_rise_set_transit_spa``). Stated uncertainty of SPA is +/-0.0003 deg.

Conventions
-----------
* Angles in degrees. Azimuth is clockwise from true north, 0 <= az < 360.
* ``elevation_deg`` / ``zenith_deg`` are geometric (no refraction).
  ``apparent_elevation_deg`` / ``apparent_zenith_deg`` include SPA's
  atmospheric refraction correction. Day/night is decided by the *apparent*
  elevation (``SolarPosition.is_daylight``).
* All times are aware UTC. Local day boundaries come from
  ``rwi.core.timebase.timezone_for_point``, never from the machine timezone.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pvlib

from rwi.contracts import SolarDayEvents, SolarPosition
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import ensure_utc, timezone_for_point, utc_now
from rwi.solar._frame import (
    PVLIB_SPA_METHOD,
    SolarFrame,
    compute_frame,
    declination_deg,
    to_utc_index,
)

__all__ = ["SPA_SUNRISE_ELEVATION_DEG", "solar_day_events", "solar_position", "solar_positions"]

#: Geometric elevation of the Sun's centre at sunrise/sunset in the SPA
#: rise/set routine (-0.8333 deg = 34' refraction + 16' solar semi-diameter).
SPA_SUNRISE_ELEVATION_DEG = -0.8333

_AIRMASS_MODEL = "kastenyoung1989"


def _refine_crossing(point: GeoPoint, approx: datetime, target_deg: float) -> datetime:
    """Polish an SPA rise/set time so the geometric elevation equals ``target_deg``.

    pvlib's ``sun_rise_set_transit_spa`` interpolates the ephemeris of the UTC date
    label; when the event falls on the neighbouring UTC day (always the case for
    sunrise in Australia, which is UTC+9:30..+10:30) the declination used is up to a
    day out, which moves sunrise by about a minute (0.2 deg of elevation at Alice
    Springs in August). A few secant steps on the same SPA elevation remove that.
    The correction is limited to 15 minutes; the original value is kept if the
    elevation slope is degenerate (near-polar grazing events).
    """
    t = approx
    step = timedelta(seconds=60)
    for _ in range(4):
        idx = to_utc_index([t - step, t + step])
        el = compute_frame(point, idx).data["elevation"].to_numpy(dtype=float)
        slope = (el[1] - el[0]) / (2.0 * step.total_seconds())  # deg per second
        if abs(slope) < 1e-5:
            return approx
        mid = compute_frame(point, to_utc_index([t])).data["elevation"].iloc[0]
        t = t - timedelta(seconds=(float(mid) - target_deg) / slope)
        if abs((t - approx).total_seconds()) > 900.0:
            return approx
    return t


def _provenance(point: GeoPoint, when: datetime, retrieved: datetime, notes: str) -> Provenance:
    return Provenance(
        provider="rwi",
        dataset="pvlib",
        kind=DataKind.PHYSICAL_MODEL,
        model_name=PVLIB_SPA_METHOD,
        model_version=pvlib.__version__,
        retrieved_at_utc=retrieved,
        valid_time_utc=when,
        point=point,
        processing="nrel_spa_get_solarposition",
        notes=(
            f"NREL SPA via pvlib.solarposition.get_solarposition(method='{PVLIB_SPA_METHOD}'); "
            f"{notes}; airmass={_AIRMASS_MODEL} on apparent zenith; "
            "declination derived from SPA elevation/azimuth"
        ),
    )


def _positions_from_frame(
    point: GeoPoint, times_utc: list[datetime], frame: SolarFrame
) -> list[SolarPosition]:
    d = frame.data
    elevation = np.clip(d["elevation"].to_numpy(dtype=float), -90.0, 90.0)
    apparent_elevation = np.clip(d["apparent_elevation"].to_numpy(dtype=float), -90.0, 90.0)
    azimuth = np.mod(d["azimuth"].to_numpy(dtype=float), 360.0)
    zenith = np.clip(d["zenith"].to_numpy(dtype=float), 0.0, 180.0)
    apparent_zenith = np.clip(d["apparent_zenith"].to_numpy(dtype=float), 0.0, 180.0)
    eot = d["equation_of_time"].to_numpy(dtype=float)
    dec = declination_deg(point, frame)
    airmass = pvlib.atmosphere.get_relative_airmass(
        pd.Series(apparent_zenith), model=_AIRMASS_MODEL
    ).to_numpy(dtype=float)

    retrieved = utc_now()
    out: list[SolarPosition] = []
    for i, when in enumerate(times_utc):
        am: float | None = float(airmass[i])
        if apparent_elevation[i] <= 0.0 or not np.isfinite(airmass[i]):
            am = None
        out.append(
            SolarPosition(
                point=point,
                time_utc=when,
                elevation_deg=float(elevation[i]),
                apparent_elevation_deg=float(apparent_elevation[i]),
                azimuth_deg=float(azimuth[i]),
                zenith_deg=float(zenith[i]),
                apparent_zenith_deg=float(apparent_zenith[i]),
                declination_deg=float(dec[i]),
                equation_of_time_min=float(eot[i]),
                air_mass=am,
                provenance=_provenance(point, when, retrieved, frame.notes),
            )
        )
    return out


def solar_positions(
    point: GeoPoint,
    times: Sequence[datetime],
    *,
    pressure_pa: float | None = None,
    temperature_c: float | None = None,
) -> list[SolarPosition]:
    """Sun position at ``point`` for each instant in ``times`` (one vectorised SPA call).

    Units: degrees; equation of time in minutes; times aware UTC (naive datetimes
    raise ``NaiveDatetimeError``). Frame: topocentric, azimuth clockwise from true
    north.

    pvlib inputs: ``altitude = point.elevation_or_zero_m``; ``pressure`` = the
    optional ``pressure_pa`` else the standard-atmosphere pressure for that
    altitude; ``temperature`` = optional ``temperature_c`` else 12 degC. Pressure
    and temperature only affect the refraction correction (apparent elevation).

    ``air_mass`` is Kasten & Young (1989) relative airmass on the apparent zenith,
    ``None`` when the Sun's apparent centre is at or below the horizon.
    """
    if len(times) == 0:
        return []
    index = to_utc_index(times)
    frame = compute_frame(point, index, pressure_pa=pressure_pa, temperature_c=temperature_c)
    return _positions_from_frame(point, [ensure_utc(t) for t in times], frame)


def solar_position(
    point: GeoPoint,
    when: datetime,
    *,
    pressure_pa: float | None = None,
    temperature_c: float | None = None,
) -> SolarPosition:
    """Sun position at ``point`` at the instant ``when`` (aware datetime).

    See :func:`solar_positions` for conventions and what is passed to pvlib.
    ``pressure_pa`` / ``temperature_c`` are optional refinements of the
    refraction correction; the contract call ``solar_position(point, when)``
    uses standard-atmosphere pressure for the point's elevation and 12 degC.
    """
    return solar_positions(point, [when], pressure_pa=pressure_pa, temperature_c=temperature_c)[0]


def solar_day_events(point: GeoPoint, when: datetime) -> SolarDayEvents:
    """Sunrise, sunset and solar noon for the *local* calendar day containing ``when``.

    The local day is defined by the IANA timezone at ``point``
    (``rwi.core.timebase.timezone_for_point``; UTC over open ocean). Uses
    ``pvlib.solarposition.sun_rise_set_transit_spa`` (NREL SPA, Reda & Andreas
    2003 NREL/TP-560-34302), whose sunrise/sunset convention is the Sun's geometric
    centre at -0.8333 deg.

    Polar day / polar night: when the Sun never crosses -0.8333 deg on that local
    day, ``sunrise_utc`` and ``sunset_utc`` are ``None``; ``is_polar_day`` is True
    if the Sun stays above it (``day_length_s = 86400``) else ``is_polar_night``
    (``day_length_s = 0``).

    ``transit_elevation_deg`` is the *geometric* elevation of the Sun at solar noon
    (upper transit). ``day_length_s`` is sunset minus sunrise.

    Accuracy: the raw pvlib rise/set routine can be ~1 min off when the event falls
    on a different UTC date than the local date label (e.g. every Australian
    sunrise), so sunrise and sunset are polished with secant iterations on the SPA
    elevation (``_refine_crossing``); the result then satisfies the -0.8333 deg
    condition to well under a second. Solar noon is pvlib's SPA transit, unmodified.
    """
    when_utc = ensure_utc(when)
    tz = timezone_for_point(point)
    local = when_utc.astimezone(tz)
    local_date = local.date()

    times = pd.DatetimeIndex([pd.Timestamp(local)])
    events = pvlib.solarposition.sun_rise_set_transit_spa(
        times, point.latitude_deg, point.longitude_deg
    )
    row = events.iloc[0]

    def _utc(value: object) -> datetime | None:
        if value is None or pd.isna(value):
            return None
        ts = pd.Timestamp(value).round("us")  # datetime has microsecond resolution
        return ts.tz_convert("UTC").to_pydatetime().astimezone(UTC)

    noon = _utc(row["transit"])
    if noon is None:  # pragma: no cover - SPA always returns a transit
        raise RuntimeError("pvlib returned no solar transit time")
    sunrise = _utc(row["sunrise"])
    sunset = _utc(row["sunset"])

    noon_frame = compute_frame(point, to_utc_index([noon]))
    transit_el = float(np.clip(noon_frame.data["elevation"].iloc[0], -90.0, 90.0))

    polar_day = polar_night = False
    if sunrise is None or sunset is None:
        sunrise = sunset = None
        if transit_el > SPA_SUNRISE_ELEVATION_DEG:
            polar_day = True
            day_length = 86400.0
        else:
            polar_night = True
            day_length = 0.0
    else:
        sunrise = _refine_crossing(point, sunrise, SPA_SUNRISE_ELEVATION_DEG)
        sunset = _refine_crossing(point, sunset, SPA_SUNRISE_ELEVATION_DEG)
        day_length = (sunset - sunrise).total_seconds()
        if day_length < 0.0:  # rise/set straddle the UTC date label
            day_length += 86400.0

    provenance = Provenance(
        provider="rwi",
        dataset="pvlib",
        kind=DataKind.PHYSICAL_MODEL,
        model_name="nrel_spa_sun_rise_set_transit",
        model_version=pvlib.__version__,
        retrieved_at_utc=utc_now(),
        valid_time_utc=when_utc,
        point=point,
        processing="sun_rise_set_transit_spa",
        notes=(
            f"local day {local_date.isoformat()} in {getattr(tz, 'key', str(tz))}; "
            f"sunrise/sunset at geometric elevation {SPA_SUNRISE_ELEVATION_DEG} deg; "
            f"delta_t=67 s (pvlib default); rise/set refined by secant on SPA elevation"
        ),
    )
    return SolarDayEvents(
        point=point,
        local_date=local_date.isoformat(),
        timezone_name=getattr(tz, "key", None),
        sunrise_utc=sunrise,
        sunset_utc=sunset,
        solar_noon_utc=noon,
        transit_elevation_deg=transit_el,
        day_length_s=float(day_length),
        is_polar_day=polar_day,
        is_polar_night=polar_night,
        provenance=provenance,
    )
