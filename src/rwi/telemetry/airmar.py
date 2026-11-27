"""Airmar 200WX: canboat records -> 1 s windows with true wind (ADR 0010 §2-3).

True wind is the apparent wind vector plus the vehicle's velocity over ground:

    dir_from_apparent = heading_true + AWA
    (u_a, v_a) = met_to_uv(AWS, dir_from_apparent)
    (u_c, v_c) = SOG · (sin COG, cos COG)
    speed, dir_from = uv_to_met(u_a + u_c, v_a + v_c)

Every input comes from the same 1 s window. A window that lacks any of them
gets no true wind; nothing is carried over from an earlier window.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from rwi.core.geo import GeoPoint
from rwi.core.wind import met_to_uv, uv_to_met
from rwi.telemetry.nmea2000 import (
    AttitudeRecord,
    CogSogRecord,
    GnssRecord,
    HeadingRecord,
    HumidityRecord,
    N2kRecord,
    PositionRecord,
    PressureRecord,
    TemperatureRecord,
    WindRecord,
)
from rwi.telemetry.onboard import (
    SecondBucketer,
    SensorConfigError,
    SensorWindow,
    circular_mean_deg,
    mean,
)

log = logging.getLogger(__name__)

AIRMAR_STREAM = "airmar-200wx"
TRUE_WIND_PROCESSING = "true_wind_from_apparent+cog_sog"

#: WMO-No. 8 screen height band for air temperature and humidity, metres.
SCREEN_HEIGHT_BAND_M = (1.25, 2.0)

_OUTSIDE_TEMPERATURE = "Outside Temperature"
_OUTSIDE_HUMIDITY = "Outside"
_ATMOSPHERIC = "Atmospheric"


class SensorMount(StrEnum):
    """Which vehicle the sensor is fitted to. Carried into provenance."""

    SOLAR_CAR = "solar_car"
    SUPPORT_CAR = "support_car"


@dataclass(frozen=True)
class AirmarConfig:
    """Validated Airmar settings. Build with :meth:`from_values`."""

    serial: str
    mount: SensorMount
    height_m: float
    drive_position: bool = False
    naive_utc: bool = False
    command: str | None = None

    @classmethod
    def from_values(
        cls,
        *,
        serial: str | None,
        mount: str | None,
        height_m: float | None,
        drive_position: bool = False,
        naive_utc: bool = False,
        command: str | None = None,
    ) -> "AirmarConfig":
        """Validate raw settings; raise :class:`SensorConfigError` naming every missing key."""
        problems: list[str] = []
        if not serial:
            problems.append("RWI_AIRMAR_SERIAL is required")
        parsed_mount: SensorMount | None = None
        if not mount:
            problems.append("RWI_AIRMAR_MOUNT is required (solar_car|support_car)")
        else:
            try:
                parsed_mount = SensorMount(mount)
            except ValueError:
                problems.append(f"RWI_AIRMAR_MOUNT={mount!r} is not solar_car|support_car")
        if height_m is None:
            problems.append("RWI_AIRMAR_HEIGHT_M is required (sensor height above road, m)")
        elif not height_m > 0:
            problems.append("RWI_AIRMAR_HEIGHT_M must be > 0")
        if problems:
            raise SensorConfigError("Airmar configuration incomplete: " + "; ".join(problems))
        assert serial and parsed_mount is not None and height_m is not None
        return cls(serial, parsed_mount, float(height_m), drive_position, naive_utc, command)

    @property
    def at_screen_height(self) -> bool:
        lo, hi = SCREEN_HEIGHT_BAND_M
        return lo <= self.height_m <= hi


@dataclass(frozen=True)
class TrueWind:
    """True wind over ground (direction it blows from, degrees from true north)."""

    speed_mps: float
    dir_from_deg: float


def true_wind(
    *,
    apparent_speed_mps: float,
    apparent_angle_deg: float,
    heading_true_deg: float,
    cog_deg: float | None,
    sog_mps: float,
) -> TrueWind:
    """True wind from apparent wind (AWA clockwise from bow) plus COG/SOG.

    ``cog_deg`` may be None only when ``sog_mps`` is 0 (standing still).
    """
    dir_from_apparent = heading_true_deg + apparent_angle_deg
    u_a, v_a = met_to_uv(apparent_speed_mps, dir_from_apparent)
    if sog_mps == 0.0:
        u_c = v_c = 0.0
    else:
        if cog_deg is None:
            raise ValueError("COG is required when SOG > 0")
        u_c = sog_mps * math.sin(math.radians(cog_deg))
        v_c = sog_mps * math.cos(math.radians(cog_deg))
    speed, direction = uv_to_met(u_a + u_c, v_a + v_c)
    return TrueWind(speed, direction)


def _heading_true(records: list[HeadingRecord]) -> tuple[float | None, str | None]:
    """Mean true heading of a window and how it was obtained.

    Magnetic headings become true only with a variation from the same record
    (magnetic = heading + deviation when deviation is given; true = magnetic +
    variation). Variation is never assumed to be 0.
    """
    trues: list[float] = []
    refs: set[str] = set()
    for r in records:
        if r.heading_deg is None:
            continue
        if r.reference == "True":
            trues.append(r.heading_deg)
            refs.add("True")
        elif r.reference == "Magnetic" and r.variation_deg is not None:
            magnetic = r.heading_deg + (r.deviation_deg or 0.0)
            trues.append(magnetic + r.variation_deg)
            refs.add("Magnetic+variation")
    if not trues:
        return None, None
    return circular_mean_deg(trues), "+".join(sorted(refs))


def build_airmar_window(start: datetime, records: list[N2kRecord]) -> SensorWindow:
    """Reduce one second of Airmar records to a :class:`SensorWindow`."""
    values: dict[str, float] = {}
    notes: list[str] = []

    # Apparent wind: mean of the body-frame vector (AWA clockwise from bow).
    winds: list[tuple[float, float]] = [
        (r.speed_mps, r.angle_deg)
        for r in records
        if isinstance(r, WindRecord)
        and r.reference == "Apparent"
        and r.speed_mps is not None
        and r.angle_deg is not None
    ]
    aws = awa = None
    if winds:
        x = sum(s * math.sin(math.radians(a)) for s, a in winds) / len(winds)
        y = sum(s * math.cos(math.radians(a)) for s, a in winds) / len(winds)
        aws = math.hypot(x, y)
        awa = math.degrees(math.atan2(x, y)) % 360.0 if aws > 0 else 0.0
        values["apparent_wind_speed_mps"] = aws
        values["apparent_wind_angle_deg"] = awa

    heading, heading_ref = _heading_true([r for r in records if isinstance(r, HeadingRecord)])
    if heading is not None:
        values["heading_true_deg"] = heading
        notes.append(f"heading_reference={heading_ref}")

    # Velocity over ground: mean vector of true-referenced COG/SOG.
    cogsog = [
        r
        for r in records
        if isinstance(r, CogSogRecord)
        and r.sog_mps is not None
        and (r.reference == "True" or r.sog_mps == 0.0)
        and (r.cog_deg is not None or r.sog_mps == 0.0)
    ]
    sog = cog = None
    if cogsog:
        ue = sum(
            (r.sog_mps or 0.0) * math.sin(math.radians(r.cog_deg or 0.0)) for r in cogsog
        ) / len(cogsog)
        vn = sum(
            (r.sog_mps or 0.0) * math.cos(math.radians(r.cog_deg or 0.0)) for r in cogsog
        ) / len(cogsog)
        sog = math.hypot(ue, vn)
        cog = math.degrees(math.atan2(ue, vn)) % 360.0 if sog > 0 else None
        values["sog_mps"] = sog
        if cog is not None:
            values["cog_deg"] = cog

    if aws is not None and awa is not None and heading is not None and sog is not None:
        tw = true_wind(
            apparent_speed_mps=aws,
            apparent_angle_deg=awa,
            heading_true_deg=heading,
            cog_deg=cog,
            sog_mps=sog if cog is not None else 0.0,
        )
        values["true_wind_speed_mps"] = tw.speed_mps
        values["true_wind_dir_from_deg"] = tw.dir_from_deg

    temp = mean(
        r.temperature_c
        for r in records
        if isinstance(r, TemperatureRecord) and r.source == _OUTSIDE_TEMPERATURE
    )
    if temp is not None:
        values["air_temperature_degc"] = temp
    hum = mean(
        r.relative_humidity_percent
        for r in records
        if isinstance(r, HumidityRecord) and r.source == _OUTSIDE_HUMIDITY
    )
    if hum is not None:
        values["relative_humidity_percent"] = hum
    pres = mean(
        r.pressure_pa for r in records if isinstance(r, PressureRecord) and r.source == _ATMOSPHERIC
    )
    if pres is not None:
        values["pressure_pa"] = pres

    attitude = [r for r in records if isinstance(r, AttitudeRecord)]
    pitch = mean(r.pitch_deg for r in attitude)
    roll = mean(r.roll_deg for r in attitude)
    if pitch is not None:
        values["pitch_deg"] = pitch
    if roll is not None:
        values["roll_deg"] = roll

    positions = [
        r
        for r in records
        if isinstance(r, PositionRecord)
        and r.latitude_deg is not None
        and r.longitude_deg is not None
    ]
    gnss = [r for r in records if isinstance(r, GnssRecord)]
    last_gnss = gnss[-1] if gnss else None
    position: GeoPoint | None = None
    if positions and not (last_gnss is not None and last_gnss.method == "no GNSS"):
        lat = mean(r.latitude_deg for r in positions)
        lon = mean(r.longitude_deg for r in positions)
        alt = mean(r.altitude_m for r in positions)
        assert lat is not None and lon is not None
        if alt is not None:
            values["altitude_m"] = alt
        position = GeoPoint(latitude_deg=lat, longitude_deg=lon)

    return SensorWindow(
        stream=AIRMAR_STREAM,
        start_utc=start,
        values=values,
        position=position,
        gnss_time_utc=last_gnss.gnss_time_utc if last_gnss else None,
        fix_method=last_gnss.method if last_gnss else None,
        satellites=last_gnss.satellites if last_gnss else None,
        hdop=last_gnss.hdop if last_gnss else None,
        sample_count=len(records),
        notes=tuple(notes),
    )


class AirmarWindower:
    """Feed decoded records in, get closed 1 s :class:`SensorWindow` rows out."""

    def __init__(self, *, late_tolerance_s: float) -> None:
        self._bucketer: SecondBucketer[N2kRecord] = SecondBucketer(
            late_tolerance_s=late_tolerance_s
        )

    @property
    def late_count(self) -> int:
        return self._bucketer.late_count

    def add(self, record: N2kRecord) -> list[SensorWindow]:
        return [
            build_airmar_window(s, items)
            for s, items in self._bucketer.add(record.time_utc, record)
        ]

    def flush(self) -> list[SensorWindow]:
        return [build_airmar_window(s, items) for s, items in self._bucketer.flush()]


__all__ = [
    "AIRMAR_STREAM",
    "SCREEN_HEIGHT_BAND_M",
    "TRUE_WIND_PROCESSING",
    "AirmarConfig",
    "AirmarWindower",
    "SensorMount",
    "TrueWind",
    "build_airmar_window",
    "true_wind",
]
