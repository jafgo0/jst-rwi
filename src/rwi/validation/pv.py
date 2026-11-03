"""Retrospective PV validation building blocks."""

from __future__ import annotations

import csv
import json
import logging
import re
import urllib.request
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

from rwi.contracts import ArrayConfig, IrradianceComponents, Provenance
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind
from rwi.core.timebase import utc_now
from rwi.energy.angular import AngularResponseTable
from rwi.energy.array import array_power
from rwi.energy.poa import poa_irradiance
from rwi.solar.geometry import solar_position

log = logging.getLogger(__name__)
_DATE_RE = re.compile(r'"\$date":"([^"]+)"')


def _read_timestamp(cell: str) -> datetime | None:
    m = _DATE_RE.search(cell)
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
    except ValueError:
        return None


def read_hourly_measured_power(day_dir: Path) -> dict[datetime, float]:
    """Mean power per hour summed across all four MPPT groups."""
    by_hour_by_group: dict[datetime, dict[int, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for g in range(1, 5):
        path = day_dir / f"MPPT{g}_output_data.csv"
        if not path.is_file():
            continue
        with path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                ts = _read_timestamp(row.get("timestamp", ""))
                if ts is None:
                    continue
                try:
                    power = float(row["output_volt"]) * float(row["output_curr"])
                except (KeyError, ValueError, TypeError):
                    continue
                hour = ts.replace(minute=0, second=0, microsecond=0)
                by_hour_by_group[hour][g].append(power)
    out: dict[datetime, float] = {}
    for hour, by_group in by_hour_by_group.items():
        total = sum(sum(vals) / max(1, len(vals)) for vals in by_group.values())
        out[hour] = total
    return out


def read_hourly_positions(day_dir: Path) -> dict[datetime, GeoPoint]:
    """Mean GPS position per hour from the day's gps_nav.csv."""
    buckets: dict[datetime, list[tuple[float, float]]] = defaultdict(list)
    path = day_dir / "gps_nav.csv"
    if not path.is_file():
        return {}
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            ts = _read_timestamp(row.get("timestamp", ""))
            if ts is None:
                continue
            try:
                lat = float(row["lat"]) * 1e-7
                lon = float(row["lon"]) * 1e-7
            except (KeyError, ValueError, TypeError):
                continue
            hour = ts.replace(minute=0, second=0, microsecond=0)
            buckets[hour].append((lat, lon))
    out: dict[datetime, GeoPoint] = {}
    for hour, pts in buckets.items():
        lat = mean(p[0] for p in pts)
        lon = mean(p[1] for p in pts)
        out[hour] = GeoPoint(latitude_deg=lat, longitude_deg=lon, elevation_m=100.0)
    return out


def _fetch_reanalysis(site: GeoPoint, date: str) -> dict | None:
    url = (
        "https://archive-api.open-meteo.com/v1/archive?"
        f"latitude={site.latitude_deg:.3f}&longitude={site.longitude_deg:.3f}"
        f"&start_date={date}&end_date={date}"
        "&hourly=shortwave_radiation,direct_normal_irradiance,diffuse_radiation,"
        "temperature_2m,wind_speed_10m"
        "&models=ecmwf_ifs&timezone=UTC"
    )
    try:
        with urllib.request.urlopen(url, timeout=25) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        log.warning("reanalysis fetch failed: %s", exc)
        return None





def read_hourly_headings(day_dir: Path) -> dict[datetime, float]:
    """Mean vehicle heading per hour from gps_speed.csv.

    Returns bearing in degrees (0 = north, 90 = east). This is what the
    angular response matrix needs to convert world sun azimuth to the
    vehicle's own frame; without it the matrix is applied as if the car
    always pointed due north, which is wrong for every hour the car was
    not driving north.

    Circular mean would be more correct for angles that span the north
    discontinuity but the arithmetic mean is adequate at the few-degree
    scale that matters for a per-hour aggregate.
    """
    buckets: dict[datetime, list[float]] = defaultdict(list)
    path = day_dir / "gps_speed.csv"
    if not path.is_file():
        return {}
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            ts = _read_timestamp(row.get("timestamp", ""))
            if ts is None:
                continue
            try:
                heading = float(row["headMot"]) * 1e-5
            except (KeyError, ValueError, TypeError):
                continue
            hour = ts.replace(minute=0, second=0, microsecond=0)
            buckets[hour].append(heading)
    import math
    out: dict[datetime, float] = {}
    for hour, hs in buckets.items():
        # Circular mean so a span across north does not average to south.
        x = sum(math.sin(math.radians(h)) for h in hs) / len(hs)
        y = sum(math.cos(math.radians(h)) for h in hs) / len(hs)
        out[hour] = (math.degrees(math.atan2(x, y)) + 360.0) % 360.0
    return out

def read_hourly_speeds(day_dir: Path) -> dict[datetime, float]:
    """Mean vehicle ground speed per hour in m/s from gps_speed.csv.

    Used to classify an hour as *moving* or *parked*. The team's own
    simulator switches between two solar-intake paths at that boundary:
    while moving the shadow-analysis matrix is indexed with the vehicle
    heading; while parked the panel is manually aimed at the sun and the
    matrix is indexed at azimuth 0 with an effective zenith of
    ``sun_zenith * (1 - tilt_effectiveness)``.
    """
    buckets: dict[datetime, list[float]] = defaultdict(list)
    path = day_dir / "gps_speed.csv"
    if not path.is_file():
        return {}
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            ts = _read_timestamp(row.get("timestamp", ""))
            if ts is None:
                continue
            try:
                speed_mps = float(row["gSpeed"]) * 1e-3
            except (KeyError, ValueError, TypeError):
                continue
            hour = ts.replace(minute=0, second=0, microsecond=0)
            buckets[hour].append(speed_mps)
    return {h: mean(v) for h, v in buckets.items() if v}


@dataclass(frozen=True)
class ParkedTiltModel:
    """Team's heuristic for solar power when the car is parked.

    When a solar car stops, the drivers manually tilt the panel toward the
    sun. The team models this with a single scalar ``tilt_effectiveness``
    in [0, 1]: 0 = panel left flat, 1 = panel aimed perfectly at the sun.
    The resulting angle of incidence on the panel is ``sun_zenith * (1 -
    tilt_effectiveness)``, so a 60-degree-elevation sun on a panel tilted
    with effectiveness 0.9 lands at an AOI of 3 degrees.

    Values are the team's own, taken from their ``config.py`` on 2026-11-03:
    0.95 for the single precise aim at a 30-minute control stop; 0.90 for
    the periodic re-aiming during the morning charge and the overnight
    camp; 0.90 as an environmental derating that lumps together shade,
    dust and poor site quality.
    """

    tilt_eff_control_stop: float = 0.95
    tilt_eff_morning_evening: float = 0.90
    env_derating: float = 0.90
    moving_speed_threshold_mps: float = 2.0
    """Below this mean hourly ground speed the hour counts as parked.

    2 m/s (= 7.2 km/h) is well below the WSC lower cruising band of
    65 km/h and well above GPS jitter at a stopped vehicle.
    """


def _is_control_stop_hour(hour_utc: datetime) -> bool:
    """True when the hour falls inside the mid-day control-stop band.

    BWSC lets cars drive from 08:00 to 17:00 ACST (UTC+9:30). A 30-minute
    control stop happens once per day between roughly 09:30 and 15:30
    ACST. Anything parked outside that band is morning charge or evening
    camp, which the team re-aims less precisely.
    """
    acst_hour = (hour_utc.hour + 9) % 24 + (hour_utc.minute + 30) / 60.0
    return 9.5 <= acst_hour <= 15.5


def _parked_hour_power_w(
    *,
    array: ArrayConfig,
    sun_zenith_deg: float,
    ghi_w_m2: float,
    air_temperature_c: float,
    tilt_model: ParkedTiltModel,
    is_control_stop: bool,
) -> float:
    """DC power under the team's parked-tilt heuristic.

    Mirrors ``_parked_relative_sun_position`` and ``_panel_group_power_w``
    in the team's ``p_intake_model.py``: effective zenith shrinks by the
    tilt effectiveness; the shadow matrix is indexed at vehicle-frame
    azimuth 0 (panel aimed at the sun, so the sun is "in front" of the
    panel's own frame); GHI feeds the model directly because an aimed
    panel loses almost nothing to cosine incidence; the result is derated
    for shade, dust and site quality.
    """
    if sun_zenith_deg >= 90.0 or ghi_w_m2 <= 0.0:
        return 0.0

    tilt_eff = (
        tilt_model.tilt_eff_control_stop
        if is_control_stop
        else tilt_model.tilt_eff_morning_evening
    )
    ze_eff_deg = sun_zenith_deg * (1.0 - tilt_eff)

    if isinstance(array.angular_response, AngularResponseTable):
        angular_factor = array.angular_response.mean_response(
            azimuth_deg=0.0,
            zenith_deg=min(ze_eff_deg, 89.999),
        )
    else:
        angular_factor = 1.0

    t_cell_c = air_temperature_c + (array.noct_c - 20.0) * ghi_w_m2 / 800.0
    temp_factor = 1.0 + array.temperature_coefficient_pmax_per_c * (t_cell_c - 25.0)

    return (
        ghi_w_m2
        * array.area_m2
        * array.cell_efficiency_stc
        * angular_factor
        * temp_factor
        * array.mppt_efficiency
        * array.wiring_efficiency
        * tilt_model.env_derating
    )


@dataclass(frozen=True)
class HourlyComparison:
    hour_utc: datetime
    site: GeoPoint
    solar_elevation_deg: float
    reanalysis_ghi_w_m2: float
    poa_global_w_m2: float
    vehicle_heading_deg: float
    vehicle_speed_mps: float
    activity: str
    sun_azimuth_world_deg: float
    sun_azimuth_vehicle_deg: float
    modelled_dc_power_w: float
    measured_dc_power_w: float

    @property
    def error_w(self) -> float:
        return self.modelled_dc_power_w - self.measured_dc_power_w


@dataclass(frozen=True)
class RetrospectiveReport:
    comparisons: list[HourlyComparison]
    day_label: str
    array_label: str

    @property
    def hour_count(self) -> int:
        return len(self.comparisons)

    @property
    def modelled_mean_w(self) -> float:
        return mean(c.modelled_dc_power_w for c in self.comparisons) if self.comparisons else 0.0

    @property
    def measured_mean_w(self) -> float:
        return mean(c.measured_dc_power_w for c in self.comparisons) if self.comparisons else 0.0

    @property
    def mae_w(self) -> float:
        if not self.comparisons:
            return 0.0
        return mean(abs(c.error_w) for c in self.comparisons)

    @property
    def bias_w(self) -> float:
        if not self.comparisons:
            return 0.0
        return mean(c.error_w for c in self.comparisons)

    @property
    def model_over_measured(self) -> float:
        if not self.comparisons or self.measured_mean_w == 0:
            return float("nan")
        return self.modelled_mean_w / self.measured_mean_w


def build_report(
    *,
    day_dir: Path,
    array: ArrayConfig,
    day_label: str,
    array_label: str,
    hours: Iterable[datetime] | None = None,
    min_solar_elevation_deg: float = 2.0,
    tilt_model: ParkedTiltModel | None = None,
) -> RetrospectiveReport:
    power_by_hour = read_hourly_measured_power(day_dir)
    pos_by_hour = read_hourly_positions(day_dir)
    heading_by_hour = read_hourly_headings(day_dir)
    speed_by_hour = read_hourly_speeds(day_dir)
    tilt = tilt_model if tilt_model is not None else ParkedTiltModel()
    hour_set = set(hours) if hours is not None else None

    comparisons: list[HourlyComparison] = []
    for hour in sorted(power_by_hour):
        if hour_set is not None and hour not in hour_set:
            continue
        site = pos_by_hour.get(hour)
        if site is None:
            continue
        solar = solar_position(site, hour)
        if solar.apparent_elevation_deg <= min_solar_elevation_deg:
            continue
        data = _fetch_reanalysis(site, hour.strftime("%Y-%m-%d"))
        if data is None:
            continue
        h = data.get("hourly", {})
        times = h.get("time", [])
        try:
            idx = next(
                i for i, t in enumerate(times)
                if datetime.fromisoformat(t).replace(tzinfo=UTC) == hour
            )
        except StopIteration:
            continue
        ghi = h["shortwave_radiation"][idx]
        if ghi is None or ghi == 0:
            continue
        dni = h["direct_normal_irradiance"][idx] or 0
        dhi = h["diffuse_radiation"][idx] or 0
        t_air = h["temperature_2m"][idx] or 15.0
        wind = h["wind_speed_10m"][idx] or 2.0

        irr = IrradianceComponents(
            time_utc=hour, point=site,
            ghi_w_m2=float(ghi), dni_w_m2=float(dni), dhi_w_m2=float(dhi),
            kind=DataKind.ANALYSIS,
            provenance=Provenance(
                provider="open-meteo", dataset="archive",
                kind=DataKind.ANALYSIS, retrieved_at_utc=utc_now(),
                valid_time_utc=hour, model_name="ecmwf_ifs", point=site,
            ),
            solar_elevation_deg=solar.apparent_elevation_deg,
        )
        heading = heading_by_hour.get(hour, 0.0)
        speed = speed_by_hour.get(hour, 0.0)
        sun_zenith_deg = 90.0 - solar.apparent_elevation_deg

        if speed < tilt.moving_speed_threshold_mps:
            # Parked path: panel manually aimed at the sun. The shadow
            # matrix is indexed at vehicle-frame azimuth 0 with an
            # effective zenith that shrinks by tilt effectiveness. GHI
            # feeds directly because an aimed panel captures close to
            # GHI with little cosine loss.
            is_control = _is_control_stop_hour(hour)
            activity = "control_stop" if is_control else "parked"
            dc_power = _parked_hour_power_w(
                array=array,
                sun_zenith_deg=sun_zenith_deg,
                ghi_w_m2=float(ghi),
                air_temperature_c=float(t_air),
                tilt_model=tilt,
                is_control_stop=is_control,
            )
            poa_global = float(ghi)
            sun_az_vehicle = 0.0
        else:
            # Moving path: vehicle-frame sun azimuth = world - heading.
            # Pvlib's POA transposition and our array_power together
            # apply the shadow matrix at (sun_az_vehicle, AOI).
            activity = "moving"
            sun_az_vehicle = (solar.azimuth_deg - heading) % 360.0
            poa = poa_irradiance(
                solar=solar, irradiance=irr,
                surface_tilt_deg=0.0,
                surface_azimuth_deg=sun_az_vehicle,
                albedo=0.2,
            )
            est = array_power(
                poa, array,
                air_temperature_c=float(t_air),
                wind_speed_mps=float(wind),
            )
            dc_power = est.dc_power_w
            poa_global = poa.poa_global_w_m2

        comparisons.append(HourlyComparison(
            hour_utc=hour, site=site,
            solar_elevation_deg=solar.apparent_elevation_deg,
            reanalysis_ghi_w_m2=float(ghi),
            poa_global_w_m2=poa_global,
            vehicle_heading_deg=heading,
            vehicle_speed_mps=speed,
            activity=activity,
            sun_azimuth_world_deg=solar.azimuth_deg,
            sun_azimuth_vehicle_deg=sun_az_vehicle,
            modelled_dc_power_w=dc_power,
            measured_dc_power_w=power_by_hour[hour],
        ))
    return RetrospectiveReport(
        comparisons=comparisons, day_label=day_label, array_label=array_label,
    )
