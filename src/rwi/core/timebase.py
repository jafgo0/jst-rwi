"""Single temporal framework for the platform.

Rules enforced here:
  * every internal datetime is timezone-aware and in UTC;
  * naive datetimes are rejected loudly rather than guessed at;
  * local time is derived from the *active geographic position*, never from the
    machine's own timezone, and never from one hardcoded Australian offset. The
    route crosses NT into SA: during race week (August) both observe UTC+9:30,
    but South Australia observes daylight saving and the Northern Territory does
    not, so the two diverge outside winter. Deriving the zone from the position
    is correct in both cases.

Regulation clock times are a separate matter: clause 1.3.1 fixes event time at
ACST = UTC+9:30 regardless of position. Those live in :mod:`rwi.regulations`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, tzinfo
from functools import lru_cache
from typing import TYPE_CHECKING, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, model_validator

from rwi.core.errors import NaiveDatetimeError

if TYPE_CHECKING:
    from rwi.core.geo import GeoPoint

__all__ = [
    "UTC",
    "TimeWindow",
    "ensure_utc",
    "floor_to_seconds",
    "local_utc_offset_hours",
    "timezone_for_point",
    "to_local",
    "utc_now",
]


def utc_now() -> datetime:
    """Current wall-clock instant as an aware UTC datetime."""
    return datetime.now(tz=UTC)


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` converted to UTC, rejecting naive datetimes.

    A naive datetime is an unanswerable question ("22:00 where?"), so it is an
    error rather than something to assume about.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise NaiveDatetimeError(
            f"naive datetime {value.isoformat()} - supply tzinfo "
            "(use rwi.core.timebase.utc_now() or an explicit zone)"
        )
    return value.astimezone(UTC)


@lru_cache(maxsize=1)
def _finder() -> object:
    from timezonefinder import TimezoneFinder

    return TimezoneFinder(in_memory=True)


@lru_cache(maxsize=4096)
def _tz_name(lat_rounded: float, lon_rounded: float) -> str | None:
    return _finder().timezone_at(lat=lat_rounded, lng=lon_rounded)  # type: ignore[attr-defined]


def timezone_name_for(latitude_deg: float, longitude_deg: float) -> str | None:
    """IANA timezone name for a coordinate, or None over open ocean."""
    return _tz_name(round(latitude_deg, 3), round(longitude_deg, 3))


def timezone_for_point(point: GeoPoint) -> tzinfo:
    """Timezone at the given position; falls back to UTC when undetermined."""
    name = timezone_name_for(point.latitude_deg, point.longitude_deg)
    return ZoneInfo(name) if name else UTC


def to_local(when: datetime, point: GeoPoint) -> datetime:
    """Convert an instant to local civil time at the given position."""
    return ensure_utc(when).astimezone(timezone_for_point(point))


def local_utc_offset_hours(when: datetime, point: GeoPoint) -> float:
    """UTC offset in hours at that position and instant, DST included."""
    local = to_local(when, point)
    offset = local.utcoffset()
    return 0.0 if offset is None else offset.total_seconds() / 3600.0


def floor_to_seconds(when: datetime, step_s: int) -> datetime:
    """Floor an instant to a multiple of ``step_s`` since the Unix epoch."""
    if step_s <= 0:
        raise ValueError("step_s must be positive")
    ts = ensure_utc(when).timestamp()
    return datetime.fromtimestamp(ts - (ts % step_s), tz=UTC)


class TimeWindow(BaseModel):
    """A half-open UTC interval [start, end)."""

    model_config = ConfigDict(frozen=True)

    start_utc: datetime
    end_utc: datetime

    @model_validator(mode="after")
    def _normalise(self) -> Self:
        start = ensure_utc(self.start_utc)
        end = ensure_utc(self.end_utc)
        if end < start:
            raise ValueError("end_utc must not precede start_utc")
        object.__setattr__(self, "start_utc", start)
        object.__setattr__(self, "end_utc", end)
        return self

    @property
    def duration(self) -> timedelta:
        return self.end_utc - self.start_utc

    def contains(self, when: datetime) -> bool:
        return self.start_utc <= ensure_utc(when) < self.end_utc

    def steps(self, step_s: int) -> Iterator[datetime]:
        """Yield instants from start (inclusive) to end (exclusive) every ``step_s``."""
        if step_s <= 0:
            raise ValueError("step_s must be positive")
        current = self.start_utc
        delta = timedelta(seconds=step_s)
        while current < self.end_utc:
            yield current
            current += delta

    @classmethod
    def from_now(cls, hours: float, *, now: datetime | None = None) -> TimeWindow:
        start = ensure_utc(now) if now else utc_now()
        return cls(start_utc=start, end_utc=start + timedelta(hours=hours))
