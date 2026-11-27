"""EKO ML-01 silicon pyranometer through an external ADC (ADR 0010 §4).

Input is one CSV line per reading, ``time_utc,value[,pitch_deg,roll_deg]``,
with ``value`` in mV or W/m² as configured. mV is converted with the
certificate sensitivity::

    W/m² = mV · 1000 / sensitivity_µV_per_Wm2

Nothing here has a default for a physical constant: sensitivity, serial and
calibration date are required, and a missing one stops start-up.
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from rwi.telemetry.onboard import (
    SecondBucketer,
    SensorConfigError,
    SensorWindow,
    mean,
    tilt_from_pitch_roll_deg,
)

log = logging.getLogger(__name__)

ML01_STREAM = "eko-ml01"

#: Manufacturer output range of the ML-01, mV. Above this the reading is not trusted.
ML01_OUTPUT_RANGE_MV = (0.0, 100.0)

#: Calibration older than this many years gets a warning and a provenance note.
CALIBRATION_WARN_YEARS = 2

ML01_SENSOR_NOTE = (
    "EKO ML-01 silicon pyranometer, 400-1100 nm; silicon spectral mismatch error "
    "grows under cloud and at low sun, not quantified until compared with a "
    "thermopile reference"
)


class Ml01InputUnit(StrEnum):
    MV = "mv"
    WM2 = "wm2"


class Ml01Mount(StrEnum):
    """Where the ML-01 sits. ``shared_with_airmar`` takes tilt and position from the Airmar."""

    SOLAR_CAR = "solar_car"
    SUPPORT_CAR = "support_car"
    SHARED_WITH_AIRMAR = "shared_with_airmar"


@dataclass(frozen=True)
class Ml01Config:
    """Validated ML-01 settings. Build with :meth:`from_values`."""

    sensitivity_uv_per_wm2: float
    serial: str
    calibration_date: date
    input_unit: Ml01InputUnit
    mount: Ml01Mount
    level_tolerance_deg: float | None = None
    assume_level: bool = False
    command: str | None = None

    @classmethod
    def from_values(
        cls,
        *,
        sensitivity_uv_per_wm2: float | None,
        serial: str | None,
        calibration_date: date | None,
        input_unit: str | None,
        mount: str | None,
        level_tolerance_deg: float | None = None,
        assume_level: bool = False,
        command: str | None = None,
    ) -> "Ml01Config":
        """Validate raw settings; raise :class:`SensorConfigError` naming every problem."""
        problems: list[str] = []
        if sensitivity_uv_per_wm2 is None:
            problems.append(
                "RWI_ML01_SENSITIVITY_UV_PER_WM2 is required (from the sensor's calibration "
                "certificate; there is no default)"
            )
        elif not (math.isfinite(sensitivity_uv_per_wm2) and sensitivity_uv_per_wm2 > 0):
            problems.append("RWI_ML01_SENSITIVITY_UV_PER_WM2 must be a positive number")
        if not serial:
            problems.append("RWI_ML01_SERIAL is required")
        if calibration_date is None:
            problems.append("RWI_ML01_CALIBRATION_DATE is required (YYYY-MM-DD)")
        unit: Ml01InputUnit | None = None
        if not input_unit:
            problems.append("RWI_ML01_INPUT_UNIT is required (mv|wm2)")
        else:
            try:
                unit = Ml01InputUnit(input_unit)
            except ValueError:
                problems.append(f"RWI_ML01_INPUT_UNIT={input_unit!r} is not mv|wm2")
        parsed_mount: Ml01Mount | None = None
        if not mount:
            problems.append("RWI_ML01_MOUNT is required (solar_car|support_car|shared_with_airmar)")
        else:
            try:
                parsed_mount = Ml01Mount(mount)
            except ValueError:
                problems.append(
                    f"RWI_ML01_MOUNT={mount!r} is not solar_car|support_car|shared_with_airmar"
                )
        if level_tolerance_deg is not None and not level_tolerance_deg >= 0:
            problems.append("RWI_ML01_LEVEL_TOLERANCE_DEG must be >= 0")
        if parsed_mount is Ml01Mount.SHARED_WITH_AIRMAR and level_tolerance_deg is None:
            problems.append(
                "RWI_ML01_LEVEL_TOLERANCE_DEG is required when tilt comes from the Airmar"
            )
        if problems:
            raise SensorConfigError("ML-01 configuration incomplete: " + "; ".join(problems))
        assert sensitivity_uv_per_wm2 is not None and serial and calibration_date is not None
        assert unit is not None and parsed_mount is not None
        return cls(
            float(sensitivity_uv_per_wm2),
            serial,
            calibration_date,
            unit,
            parsed_mount,
            level_tolerance_deg,
            assume_level,
            command,
        )

    def to_wm2(self, value: float) -> float:
        """Irradiance in W/m² from one ADC value in the configured unit."""
        if self.input_unit is Ml01InputUnit.WM2:
            return value
        return value * 1000.0 / self.sensitivity_uv_per_wm2

    def to_mv(self, value: float) -> float:
        """Sensor output in mV from one ADC value in the configured unit."""
        if self.input_unit is Ml01InputUnit.MV:
            return value
        return value * self.sensitivity_uv_per_wm2 / 1000.0


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 February
        return d.replace(year=d.year + years, day=28)


def calibration_is_old(calibration_date: date, on: date) -> bool:
    """True when ``on`` is more than :data:`CALIBRATION_WARN_YEARS` after calibration."""
    return on > _add_years(calibration_date, CALIBRATION_WARN_YEARS)


def provenance_notes(config: Ml01Config, on: date) -> list[str]:
    """Provenance notes for ML-01 values observed on ``on`` (UTC date)."""
    notes = [
        ML01_SENSOR_NOTE,
        f"serial={config.serial}",
        f"calibration_date={config.calibration_date.isoformat()}",
        f"sensitivity_uv_per_wm2={config.sensitivity_uv_per_wm2:g}",
        f"input_unit={config.input_unit.value}",
        f"mount={config.mount.value}",
    ]
    if calibration_is_old(config.calibration_date, on):
        notes.append(f"calibration older than {CALIBRATION_WARN_YEARS} years on {on.isoformat()}")
    return notes


@dataclass(frozen=True)
class LevelVerdict:
    """Whether a window's reading may be archived as GHI, and why."""

    level: bool
    tilt_deg: float | None
    reason: str


def level_verdict(
    *,
    pitch_deg: float | None,
    roll_deg: float | None,
    tolerance_deg: float | None,
    assume_level: bool,
) -> LevelVerdict:
    """Apply the level gate (ADR 0010 §4).

    Known tilt needs a tolerance; a missing tolerance raises rather than
    guessing one. Unknown tilt passes only with ``assume_level``.
    """
    if pitch_deg is None or roll_deg is None:
        if assume_level:
            return LevelVerdict(True, None, "tilt unknown; level assumed by configuration")
        return LevelVerdict(False, None, "tilt unknown and ml01_assume_level is not set")
    tilt = tilt_from_pitch_roll_deg(pitch_deg, roll_deg)
    if tolerance_deg is None:
        raise SensorConfigError("RWI_ML01_LEVEL_TOLERANCE_DEG is required when tilt is available")
    if tilt > tolerance_deg:
        return LevelVerdict(False, tilt, f"tilt {tilt:.2f} deg > tolerance {tolerance_deg:g} deg")
    return LevelVerdict(True, tilt, f"tilt {tilt:.2f} deg <= tolerance {tolerance_deg:g} deg")


@dataclass(frozen=True)
class Ml01Reading:
    time_utc: datetime
    value: float
    pitch_deg: float | None = None
    roll_deg: float | None = None


@dataclass
class Ml01ParseStats:
    lines: int = 0
    readings: int = 0
    malformed: int = 0
    header: int = 0


def _float_or_none(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    v = float(raw)
    if not math.isfinite(v):
        raise ValueError("non-finite")
    return v


@dataclass
class Ml01Parser:
    """Parse ``time_utc,value[,pitch_deg,roll_deg]`` lines. Bad lines are counted, not fatal."""

    stats: Ml01ParseStats = field(default_factory=Ml01ParseStats)

    def feed_line(self, line: str) -> Ml01Reading | None:
        line = line.strip()
        if not line or line.startswith("#"):
            return None
        self.stats.lines += 1
        parts = [p.strip() for p in line.split(",")]
        if parts[0].lower() == "time_utc":
            self.stats.header += 1
            return None
        try:
            if len(parts) not in (2, 4):
                raise ValueError("expected 2 or 4 columns")
            when = datetime.fromisoformat(parts[0].replace("Z", "+00:00"))
            if when.tzinfo is None:
                raise ValueError("time_utc needs an explicit UTC offset")
            value = _float_or_none(parts[1])
            if value is None:
                raise ValueError("empty value")
            pitch = roll = None
            if len(parts) == 4:
                pitch = _float_or_none(parts[2])
                roll = _float_or_none(parts[3])
        except ValueError:
            self.stats.malformed += 1
            return None
        self.stats.readings += 1
        return Ml01Reading(when, value, pitch, roll)


def build_ml01_window(
    config: Ml01Config, start: datetime, readings: list[Ml01Reading]
) -> SensorWindow:
    """Reduce one second of ML-01 readings to a :class:`SensorWindow`.

    ``irradiance_wm2`` is the mean converted reading and ``output_mv_max`` the
    largest sensor output in the window (for the range check). No clamping.
    """
    values: dict[str, float] = {}
    wm2 = mean(config.to_wm2(r.value) for r in readings)
    if wm2 is not None:
        values["irradiance_wm2"] = wm2
        values["output_mv_max"] = max(config.to_mv(r.value) for r in readings)
    pitch = mean(r.pitch_deg for r in readings)
    roll = mean(r.roll_deg for r in readings)
    if pitch is not None:
        values["pitch_deg"] = pitch
    if roll is not None:
        values["roll_deg"] = roll
    return SensorWindow(
        stream=ML01_STREAM,
        start_utc=start,
        values=values,
        sample_count=len(readings),
    )


class Ml01Windower:
    """Feed readings in, get closed 1 s :class:`SensorWindow` rows out."""

    def __init__(self, config: Ml01Config, *, late_tolerance_s: float) -> None:
        self._config = config
        self._bucketer: SecondBucketer[Ml01Reading] = SecondBucketer(
            late_tolerance_s=late_tolerance_s
        )

    @property
    def late_count(self) -> int:
        return self._bucketer.late_count

    def add(self, reading: Ml01Reading) -> list[SensorWindow]:
        closed = self._bucketer.add(reading.time_utc, reading)
        return [build_ml01_window(self._config, s, items) for s, items in closed]

    def flush(self) -> list[SensorWindow]:
        return [build_ml01_window(self._config, s, items) for s, items in self._bucketer.flush()]


__all__ = [
    "CALIBRATION_WARN_YEARS",
    "ML01_OUTPUT_RANGE_MV",
    "ML01_SENSOR_NOTE",
    "ML01_STREAM",
    "LevelVerdict",
    "Ml01Config",
    "Ml01InputUnit",
    "Ml01Mount",
    "Ml01ParseStats",
    "Ml01Parser",
    "Ml01Reading",
    "Ml01Windower",
    "build_ml01_window",
    "calibration_is_old",
    "level_verdict",
    "provenance_notes",
]
