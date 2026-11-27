"""Decode canboat ``analyzer -json -si`` output into typed NMEA 2000 records (ADR 0009).

canboat does the CAN framing, fast-packet reassembly and per-PGN bit layouts.
This module only maps its JSON, one line at a time, to small typed records in
the platform's canonical units. Field names are matched exactly against
canboat.json v10.0.0; a field that is absent becomes ``None`` and is counted,
never guessed from an alias.

Units arrive as SI because the stream must open with the ``"units":"si"``
banner: K, Pa, rad, m/s, and humidity as a 0-1 ratio. They are converted here,
at the boundary, to degC, Pa, deg and percent.
"""

import json
import logging
import math
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final

from rwi.core.errors import RwiError
from rwi.core.units import kelvin_to_celsius, rad_to_deg, wrap_deg_360

log = logging.getLogger(__name__)

#: The canboat.json release whose field names this decoder is pinned to.
CANBOAT_FIELD_VERSION: Final = "10.0.0"

#: PGNs this decoder understands. Anything else is counted as unused.
SUPPORTED_PGNS: Final = frozenset(
    {127250, 127257, 129025, 129026, 129029, 130306, 130310, 130311, 130312, 130313, 130314}
)


class CanboatError(RwiError):
    """The canboat stream cannot be trusted (missing SI banner, naive timestamps)."""


# ---------------------------------------------------------------------------
# Typed records. ``time_utc`` is the capture host's clock as canboat stamped it.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WindRecord:
    """PGN 130306. ``angle_deg`` is clockwise from the bow when apparent."""

    time_utc: datetime
    src: int | None
    speed_mps: float | None
    angle_deg: float | None
    reference: str | None


@dataclass(frozen=True, slots=True)
class TemperatureRecord:
    """Air or water temperature from PGN 130310/130311/130312, in degC."""

    time_utc: datetime
    src: int | None
    pgn: int
    temperature_c: float | None
    source: str | None


@dataclass(frozen=True, slots=True)
class HumidityRecord:
    """Relative humidity from PGN 130311/130313, in percent."""

    time_utc: datetime
    src: int | None
    pgn: int
    relative_humidity_percent: float | None
    source: str | None


@dataclass(frozen=True, slots=True)
class PressureRecord:
    """Pressure from PGN 130310/130311/130314, in Pa (never reduced to MSL)."""

    time_utc: datetime
    src: int | None
    pgn: int
    pressure_pa: float | None
    source: str | None


@dataclass(frozen=True, slots=True)
class HeadingRecord:
    """PGN 127250. ``reference`` is 'True' or 'Magnetic'."""

    time_utc: datetime
    src: int | None
    heading_deg: float | None
    deviation_deg: float | None
    variation_deg: float | None
    reference: str | None


@dataclass(frozen=True, slots=True)
class AttitudeRecord:
    """PGN 127257, in degrees."""

    time_utc: datetime
    src: int | None
    yaw_deg: float | None
    pitch_deg: float | None
    roll_deg: float | None


@dataclass(frozen=True, slots=True)
class PositionRecord:
    """PGN 129025 (rapid) or the position part of 129029."""

    time_utc: datetime
    src: int | None
    pgn: int
    latitude_deg: float | None
    longitude_deg: float | None
    altitude_m: float | None = None


@dataclass(frozen=True, slots=True)
class CogSogRecord:
    """PGN 129026. ``reference`` is 'True' or 'Magnetic'."""

    time_utc: datetime
    src: int | None
    cog_deg: float | None
    sog_mps: float | None
    reference: str | None


@dataclass(frozen=True, slots=True)
class GnssRecord:
    """PGN 129029 fix metadata. ``gnss_time_utc`` is stored next to the host time."""

    time_utc: datetime
    src: int | None
    gnss_time_utc: datetime | None
    method: str | None
    satellites: int | None
    hdop: float | None


N2kRecord = (
    WindRecord
    | TemperatureRecord
    | HumidityRecord
    | PressureRecord
    | HeadingRecord
    | AttitudeRecord
    | PositionRecord
    | CogSogRecord
    | GnssRecord
)


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        f = float(value)
        return f if math.isfinite(f) else None
    return None


def _enum(value: Any) -> str | None:
    """canboat prints lookups as their name; newer builds may wrap them in a dict."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        name = value.get("name")
        return name if isinstance(name, str) else None
    return None


def _k_to_c(value: Any) -> float | None:
    k = _num(value)
    return kelvin_to_celsius(k) if k is not None else None


def _rad_to_deg360(value: Any) -> float | None:
    r = _num(value)
    return wrap_deg_360(rad_to_deg(r)) if r is not None else None


def _rad_to_deg_signed(value: Any) -> float | None:
    r = _num(value)
    return rad_to_deg(r) if r is not None else None


def _ratio_to_percent(value: Any) -> float | None:
    r = _num(value)
    return r * 100.0 if r is not None else None


def parse_canboat_timestamp(raw: str, *, naive_is_utc: bool) -> datetime:
    """Parse canboat's host timestamp; a naive one is UTC only when declared so."""
    text = raw.strip()
    # Legacy actisense-serial form: 2024-08-22-03:15:00.123
    if len(text) > 10 and text[10] == "-":
        text = text[:10] + "T" + text[11:]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"unparseable canboat timestamp {raw!r}") from exc
    if parsed.tzinfo is None:
        if not naive_is_utc:
            raise CanboatError(
                f"canboat timestamp {raw!r} has no timezone; set "
                "RWI_AIRMAR_CANBOAT_NAIVE_UTC=true only if the capture host clock is UTC"
            )
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _gnss_datetime(date_raw: Any, time_raw: Any) -> datetime | None:
    """PGN 129029 Date/Time as canboat prints them ('2027.08.22', '03:15:00.12345')."""
    d: date | None = None
    if isinstance(date_raw, str):
        try:
            d = date.fromisoformat(date_raw.replace(".", "-"))
        except ValueError:
            return None
    elif isinstance(date_raw, int | float) and not isinstance(date_raw, bool):
        d = date(1970, 1, 1) + timedelta(days=int(date_raw))
    if d is None:
        return None
    if isinstance(time_raw, str):
        main, _, frac = time_raw.partition(".")
        try:
            t = time.fromisoformat(main)
        except ValueError:
            return None
        micro = int((frac + "000000")[:6]) if frac.isdigit() else 0
        return datetime.combine(d, t, tzinfo=UTC) + timedelta(microseconds=micro)
    seconds = _num(time_raw)
    if seconds is None:
        return None
    return datetime.combine(d, time(0), tzinfo=UTC) + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------


@dataclass
class DecoderStats:
    """What the decoder saw. Each counter is its own failure mode."""

    lines: int = 0
    records: int = 0
    malformed: int = 0
    unused_pgn: int = 0
    no_timestamp: int = 0
    missing_fields: Counter[str] | None = None

    def __post_init__(self) -> None:
        if self.missing_fields is None:
            self.missing_fields = Counter()


class CanboatDecoder:
    """Stateful line decoder: banner first, then one record line at a time."""

    def __init__(self, *, naive_is_utc: bool = False) -> None:
        self._naive_is_utc = naive_is_utc
        self.banner_version: str | None = None
        self.stats = DecoderStats()

    @property
    def banner_seen(self) -> bool:
        return self.banner_version is not None

    def feed_line(self, line: str) -> list[N2kRecord]:
        """Decode one canboat output line into zero or more records.

        Raises :class:`CanboatError` when the first non-empty line is not the SI
        banner, or for a naive timestamp without the UTC declaration. Every other
        problem is counted and the line is skipped.
        """
        text = line.strip()
        if not text:
            return []
        self.stats.lines += 1
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            if not self.banner_seen:
                raise CanboatError("canboat stream does not start with a JSON banner") from None
            self.stats.malformed += 1
            return []
        if not isinstance(obj, dict):
            self.stats.malformed += 1
            return []

        if not self.banner_seen:
            if obj.get("units") != "si" or "version" not in obj:
                raise CanboatError(
                    'canboat stream must start with the {"version":…,"units":"si"} '
                    "banner; run analyzer with -json -si"
                )
            self.banner_version = str(obj["version"])
            return []

        pgn = obj.get("pgn")
        if not isinstance(pgn, int) or isinstance(pgn, bool):
            self.stats.malformed += 1
            return []
        if pgn not in SUPPORTED_PGNS:
            self.stats.unused_pgn += 1
            return []
        raw_ts = obj.get("timestamp")
        if not isinstance(raw_ts, str):
            self.stats.no_timestamp += 1
            return []
        try:
            when = parse_canboat_timestamp(raw_ts, naive_is_utc=self._naive_is_utc)
        except ValueError:
            self.stats.malformed += 1
            return []
        fields = obj.get("fields")
        if not isinstance(fields, dict):
            self.stats.malformed += 1
            return []
        src_raw = obj.get("src")
        src = src_raw if isinstance(src_raw, int) and not isinstance(src_raw, bool) else None

        records = self._decode(pgn, when, src, fields)
        self.stats.records += len(records)
        return records

    def _field(self, pgn: int, fields: dict[str, Any], name: str) -> Any:
        if name not in fields:
            assert self.stats.missing_fields is not None
            self.stats.missing_fields[f"{pgn}:{name}"] += 1
            return None
        return fields[name]

    def _decode(
        self, pgn: int, when: datetime, src: int | None, fields: dict[str, Any]
    ) -> list[N2kRecord]:
        f = self._field
        if pgn == 130306:
            return [
                WindRecord(
                    when,
                    src,
                    _num(f(pgn, fields, "Wind Speed")),
                    _rad_to_deg360(f(pgn, fields, "Wind Angle")),
                    _enum(f(pgn, fields, "Reference")),
                )
            ]
        if pgn == 130310:
            out: list[N2kRecord] = []
            air = f(pgn, fields, "Outside Ambient Air Temperature")
            if air is not None:
                out.append(TemperatureRecord(when, src, pgn, _k_to_c(air), "Outside Temperature"))
            water = fields.get("Water Temperature")
            if water is not None:
                out.append(TemperatureRecord(when, src, pgn, _k_to_c(water), "Sea Temperature"))
            pressure = f(pgn, fields, "Atmospheric Pressure")
            if pressure is not None:
                out.append(PressureRecord(when, src, pgn, _num(pressure), "Atmospheric"))
            return out
        if pgn == 130311:
            out = []
            temp = f(pgn, fields, "Temperature")
            if temp is not None:
                out.append(
                    TemperatureRecord(
                        when, src, pgn, _k_to_c(temp), _enum(f(pgn, fields, "Temperature Source"))
                    )
                )
            hum = f(pgn, fields, "Humidity")
            if hum is not None:
                out.append(
                    HumidityRecord(
                        when,
                        src,
                        pgn,
                        _ratio_to_percent(hum),
                        _enum(f(pgn, fields, "Humidity Source")),
                    )
                )
            pressure = f(pgn, fields, "Atmospheric Pressure")
            if pressure is not None:
                out.append(PressureRecord(when, src, pgn, _num(pressure), "Atmospheric"))
            return out
        if pgn == 130312:
            return [
                TemperatureRecord(
                    when,
                    src,
                    pgn,
                    _k_to_c(f(pgn, fields, "Actual Temperature")),
                    _enum(f(pgn, fields, "Source")),
                )
            ]
        if pgn == 130313:
            return [
                HumidityRecord(
                    when,
                    src,
                    pgn,
                    _ratio_to_percent(f(pgn, fields, "Actual Humidity")),
                    _enum(f(pgn, fields, "Source")),
                )
            ]
        if pgn == 130314:
            return [
                PressureRecord(
                    when,
                    src,
                    pgn,
                    _num(f(pgn, fields, "Pressure")),
                    _enum(f(pgn, fields, "Source")),
                )
            ]
        if pgn == 127250:
            return [
                HeadingRecord(
                    when,
                    src,
                    _rad_to_deg360(f(pgn, fields, "Heading")),
                    _rad_to_deg_signed(fields.get("Deviation")),
                    _rad_to_deg_signed(fields.get("Variation")),
                    _enum(f(pgn, fields, "Reference")),
                )
            ]
        if pgn == 127257:
            return [
                AttitudeRecord(
                    when,
                    src,
                    _rad_to_deg_signed(fields.get("Yaw")),
                    _rad_to_deg_signed(f(pgn, fields, "Pitch")),
                    _rad_to_deg_signed(f(pgn, fields, "Roll")),
                )
            ]
        if pgn == 129025:
            return [
                PositionRecord(
                    when,
                    src,
                    pgn,
                    _num(f(pgn, fields, "Latitude")),
                    _num(f(pgn, fields, "Longitude")),
                )
            ]
        if pgn == 129026:
            return [
                CogSogRecord(
                    when,
                    src,
                    _rad_to_deg360(f(pgn, fields, "COG")),
                    _num(f(pgn, fields, "SOG")),
                    _enum(f(pgn, fields, "COG Reference")),
                )
            ]
        # 129029
        sats = _num(fields.get("Number of SVs"))
        return [
            PositionRecord(
                when,
                src,
                pgn,
                _num(f(pgn, fields, "Latitude")),
                _num(f(pgn, fields, "Longitude")),
                _num(fields.get("Altitude")),
            ),
            GnssRecord(
                when,
                src,
                _gnss_datetime(fields.get("Date"), fields.get("Time")),
                _enum(f(pgn, fields, "Method")),
                int(sats) if sats is not None else None,
                _num(fields.get("HDOP")),
            ),
        ]


__all__ = [
    "CANBOAT_FIELD_VERSION",
    "SUPPORTED_PGNS",
    "AttitudeRecord",
    "CanboatDecoder",
    "CanboatError",
    "CogSogRecord",
    "DecoderStats",
    "GnssRecord",
    "HeadingRecord",
    "HumidityRecord",
    "N2kRecord",
    "PositionRecord",
    "PressureRecord",
    "TemperatureRecord",
    "WindRecord",
    "parse_canboat_timestamp",
]
