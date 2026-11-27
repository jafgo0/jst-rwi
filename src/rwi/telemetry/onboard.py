"""Shared pieces of on-board sensor ingestion (ADR 0010 §5).

A sensor driver turns raw readings into :class:`SensorWindow` rows, one per
UTC second. :class:`SecondBucketer` does the bucketing: a window closes once
the newest reading is at least ``late_tolerance_s`` past the window's end, and
a reading for a window that has already closed is counted as late and dropped,
never merged into a later window.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from rwi.core.errors import RwiError
from rwi.core.geo import GeoPoint
from rwi.core.timebase import ensure_utc, floor_to_seconds
from rwi.core.units import wrap_deg_360

#: Width of one ingest window. Fixed by the prompt (1 Hz, 1 s averaging).
WINDOW_S = 1


class SensorConfigError(RwiError):
    """Sensor configuration is missing or invalid. The message lists every problem."""


class SensorWindow(BaseModel):
    """One closed 1 s window of one sensor stream, as written to the spool.

    ``values`` holds raw named quantities in the units their key names
    (``*_mps``, ``*_deg``, ``*_degc``, ``*_percent``, ``*_pa``, ``*_wm2``,
    ``*_mv``). A key is absent when the window had no reading for it; nothing
    is filled in. Mapping to canonical variables, quality flags and the ML-01
    level gate happens at archive time.
    """

    model_config = ConfigDict(frozen=True)

    stream: str
    start_utc: datetime
    width_s: float = Field(default=float(WINDOW_S), gt=0.0)
    values: dict[str, float]
    position: GeoPoint | None = None
    gnss_time_utc: datetime | None = None
    fix_method: str | None = None
    satellites: int | None = Field(default=None, ge=0)
    hdop: float | None = None
    sample_count: int = Field(ge=0)
    notes: tuple[str, ...] = ()

    @property
    def end_utc(self) -> datetime:
        return self.start_utc + timedelta(seconds=self.width_s)


@dataclass
class _Bucket[T]:
    start: datetime
    items: list[T] = field(default_factory=list)


class SecondBucketer[T]:
    """Group timestamped items into 1 s UTC windows with a late tolerance."""

    def __init__(self, *, late_tolerance_s: float) -> None:
        if late_tolerance_s < 0:
            raise SensorConfigError("sensor_late_tolerance_s must be >= 0")
        self._late = timedelta(seconds=late_tolerance_s)
        self._open: dict[datetime, _Bucket[T]] = {}
        self._newest: datetime | None = None
        self._closed_until: datetime | None = None
        self.late_count = 0

    def add(self, when: datetime, item: T) -> list[tuple[datetime, list[T]]]:
        """Add one item; return the windows (start, items) this closes, oldest first."""
        when = ensure_utc(when)
        start = floor_to_seconds(when, WINDOW_S)
        if self._closed_until is not None and start < self._closed_until:
            self.late_count += 1
            return []
        self._open.setdefault(start, _Bucket(start)).items.append(item)
        if self._newest is None or when > self._newest:
            self._newest = when
        return self._close_ready()

    def flush(self) -> list[tuple[datetime, list[T]]]:
        """Close every open window (end of stream)."""
        out = [(b.start, b.items) for b in sorted(self._open.values(), key=lambda b: b.start)]
        if out:
            self._closed_until = out[-1][0] + timedelta(seconds=WINDOW_S)
        self._open.clear()
        return out

    def _close_ready(self) -> list[tuple[datetime, list[T]]]:
        assert self._newest is not None
        ready = sorted(
            s for s in self._open if s + timedelta(seconds=WINDOW_S) + self._late <= self._newest
        )
        out: list[tuple[datetime, list[T]]] = []
        for s in ready:
            out.append((s, self._open.pop(s).items))
            self._closed_until = s + timedelta(seconds=WINDOW_S)
        return out


def mean(values: Iterable[float | None]) -> float | None:
    """Arithmetic mean of the non-None values, or None when there are none."""
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def circular_mean_deg(angles_deg: Sequence[float]) -> float | None:
    """Unit-vector mean of angles in degrees, in [0, 360). None if empty or undefined."""
    if not angles_deg:
        return None
    s = sum(math.sin(math.radians(a)) for a in angles_deg)
    c = sum(math.cos(math.radians(a)) for a in angles_deg)
    if math.hypot(s, c) < 1e-9 * len(angles_deg):
        return None
    return wrap_deg_360(math.degrees(math.atan2(s, c)))


def tilt_from_pitch_roll_deg(pitch_deg: float, roll_deg: float) -> float:
    """Angle between the sensor's normal and the local vertical, from pitch and roll.

    For a rotation by pitch about one horizontal axis then roll about the
    other, the normal's vertical component is cos(pitch)·cos(roll).
    """
    cos_tilt = math.cos(math.radians(pitch_deg)) * math.cos(math.radians(roll_deg))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_tilt))))


__all__ = [
    "WINDOW_S",
    "SecondBucketer",
    "SensorConfigError",
    "SensorWindow",
    "circular_mean_deg",
    "mean",
    "tilt_from_pitch_roll_deg",
]
