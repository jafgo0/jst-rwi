"""Replay the team's 2025 race-day telemetry from the CSV exports.

The team archives one CSV per CAN frame type with the Mongo exporter. Each row
is::

    _id,<signal columns>,timestamp

where ``timestamp`` is a MongoDB extended-JSON array of one date document,
written literally as the string ``[{"$date":"2025-07-08T15:47:02.175Z"}]``.
The reader parses that back into a UTC datetime at the vehicle's own clock,
which is the only clock the rest of this platform cares about.

The reader stitches frames into :class:`VehicleTelemetrySample` objects using
a bounded time window: a battery row at 12:00:00.100 and an MPPT row at
12:00:00.080 become one sample, as the two CAN frames are both reporting the
state of the car at that instant to within radio latency. The window is
configurable; its default is tight enough that two consecutive samples never
blur into one.
"""

from __future__ import annotations

import asyncio
import heapq
import json
import logging
import re
from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from rwi.contracts import (
    ArrayTelemetry,
    BatteryTelemetry,
    MotorTelemetry,
    TelemetryGap,
    TelemetrySource,
    VehicleTelemetrySample,
)
from rwi.core.errors import RwiError
from rwi.core.geo import GeoPoint
from rwi.core.position import FixQuality, PositionFix, PositionMode
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import utc_now
from rwi.telemetry.frames import KNOWN_FRAMES, UNMAPPED, FrameSpec

log = logging.getLogger(__name__)

#: The default stitching window - half a second. CAN frames at 10 Hz arrive
#: at least 100 ms apart; two frames within 500 ms are safe to merge, and the
#: tightest 2-Hz logger still gives 500 ms spacing. Can be tightened to match
#: a known high-rate stream.
DEFAULT_STITCH_WINDOW_S: Final = 0.5

#: ``[{"$date":"2025-07-08T15:47:02.175Z"}]`` is the Mongo extended-JSON form
#: the exporter writes. The reader needs to pull the ISO string out of it.
_MONGO_DATE_RE: Final = re.compile(
    r'\[\s*{\s*"\$date"\s*:\s*"([^"]+)"\s*}\s*\]'
)


class ReplayError(RwiError):
    """A replay source is unreadable or inconsistent."""


class ReplayConfig(BaseModel):
    """How to drive a CSV replay."""

    model_config = ConfigDict(frozen=True)

    root: Path = Field(description="directory holding the per-frame CSV files")
    stitch_window_s: float = Field(default=DEFAULT_STITCH_WINDOW_S, gt=0.0)
    gap_threshold_s: float = Field(default=5.0, gt=0.0,
        description="any silence longer than this is reported as a TelemetryGap")
    speed_multiplier: float = Field(default=0.0, ge=0.0,
        description="0 means replay as fast as the consumer can take it; >0 means "
                    "sleep so the wall clock advances `speed_multiplier` times "
                    "real race time (useful for a dashboard demo).")


def _parse_timestamp(cell: str) -> datetime:
    """Parse the Mongo extended-JSON timestamp cell to an aware UTC datetime.

    Accepts the exported form ``[{"$date":"2025-07-08T15:47:02.175Z"}]`` and
    also, as a fallback, a bare ISO string. Rejects anything else loudly;
    silently treating an unparsable timestamp as "now" would corrupt the whole
    replay.
    """
    cell = cell.strip()
    if cell.startswith("["):
        match = _MONGO_DATE_RE.match(cell)
        if match is None:
            # Try the full JSON path; the exporter may change its spacing.
            try:
                parsed = json.loads(cell)
            except json.JSONDecodeError as exc:
                raise ReplayError(f"unparseable timestamp cell: {cell!r}") from exc
            if not (isinstance(parsed, list) and parsed and isinstance(parsed[0], dict)
                    and "$date" in parsed[0]):
                raise ReplayError(f"timestamp cell has unexpected shape: {cell!r}")
            iso = str(parsed[0]["$date"])
        else:
            iso = match.group(1)
    else:
        iso = cell
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(iso).astimezone(UTC)
    except ValueError as exc:
        raise ReplayError(f"unparseable timestamp: {iso!r}") from exc


def _decode_row(frame: FrameSpec, row: dict[str, str]) -> dict[str, float]:
    """Apply the frame's scalings to one CSV row. Missing or empty cells are
    skipped (not zeroed) so a reader can tell what the sensor did and did not
    report."""
    out: dict[str, float] = {}
    for col in frame.columns:
        raw = row.get(col.column, "").strip()
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        out[col.signal] = (value + col.offset) * col.scale
    return out


def _iter_frame_rows(
    path: Path, frame: FrameSpec
) -> Iterable[tuple[datetime, str, dict[str, float]]]:
    """Yield (timestamp, frame_name, decoded) for each row of a frame CSV."""
    # Use the stdlib csv module via a generator; we never want the whole file
    # (28 MB for a race day) in memory. Opened in text mode with UTF-8, which
    # the Mongo exporter uses by default.
    import csv

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            raw = row.get("timestamp", "").strip()
            if not raw:
                continue
            try:
                when = _parse_timestamp(raw)
            except ReplayError as exc:
                log.warning("skipping row with bad timestamp in %s: %s", path.name, exc)
                continue
            decoded = _decode_row(frame, row)
            if not decoded:
                continue
            yield when, frame.csv_name, decoded


def _merge_streams(
    streams: Sequence[Iterable[tuple[datetime, str, dict[str, float]]]],
) -> Iterable[tuple[datetime, str, dict[str, float]]]:
    """Merge several time-sorted iterators into one global time-sorted stream.

    Each per-file iterator is already in order (the Mongo export sorts by
    insert time, which is the device time). ``heapq.merge`` with a time key
    gives us the full day's events in one pass.
    """
    yield from heapq.merge(*streams, key=lambda item: item[0])


def _provenance(csv_name: str, now: datetime) -> Provenance:
    return Provenance(
        provider="ju-solar-team",
        dataset=f"race_data2025/{csv_name}",
        kind=DataKind.OBSERVATION,
        retrieved_at_utc=now,
        observation_time_utc=now,
        processing="CSV replay from team Mongo exporter",
        notes=(
            "Data arrived from the real car via CAN -> STM32 -> radio -> "
            "pit laptop -> MongoDB -> CSV export. Replay preserves the vehicle "
            "clock; receive-side timestamps are NOT used."
        ),
    )


class CsvReplayReader:
    """Yields :class:`VehicleTelemetrySample` from a directory of race-day CSVs.

    Discovers every known frame file in ``config.root``, merges them in time,
    stitches rows within ``config.stitch_window_s`` into one sample, and
    detects gaps longer than ``config.gap_threshold_s``. Unknown frames in the
    directory are logged once and skipped.

    This is a real :class:`~rwi.contracts.TelemetryReader` implementation.
    """

    source: TelemetrySource = TelemetrySource.FILE_REPLAY

    def __init__(self, config: ReplayConfig) -> None:
        if not config.root.is_dir():
            raise ReplayError(
                f"replay root {config.root} is not a directory; "
                "point it at race_data2025/day1 (or similar)"
            )
        self._config = config
        self._gaps: list[TelemetryGap] = []
        self._mapped, self._unmapped = self._discover_frames()
        self._closed = False

    def _discover_frames(self) -> tuple[dict[Path, FrameSpec], list[str]]:
        """Partition the files in the replay directory into mapped and unknown."""
        mapped: dict[Path, FrameSpec] = {}
        unknown: list[str] = []
        for entry in sorted(self._config.root.iterdir()):
            if entry.suffix.lower() != ".csv":
                continue
            frame = KNOWN_FRAMES.get(entry.name)
            if frame is not None:
                mapped[entry] = frame
            elif entry.name not in UNMAPPED:
                unknown.append(entry.name)
        if unknown:
            log.info(
                "CSV replay: %d unmapped frame file(s) skipped: %s",
                len(unknown), ", ".join(unknown[:5]),
            )
        if not mapped:
            raise ReplayError(
                f"no known frame CSVs in {self._config.root}; "
                "the directory may be the wrong one or the exporter format "
                "may have changed"
            )
        return mapped, unknown

    @property
    def mapped_frames(self) -> tuple[str, ...]:
        return tuple(sorted(frame.csv_name for frame in self._mapped.values()))

    @property
    def unmapped_frames(self) -> tuple[str, ...]:
        return tuple(self._unmapped)

    async def gaps_so_far(self) -> list[TelemetryGap]:
        return list(self._gaps)

    async def close(self) -> None:
        self._closed = True

    async def stream(self) -> AsyncIterator[VehicleTelemetrySample]:
        """Yield one stitched sample per time window.

        A race day has 5 million rows. The decoder is CPU-bound, so wrapping
        each event in asyncio queueing was dominating the cost - a run that
        should take seconds was taking minutes. The implementation is now a
        plain synchronous iterator that yields through the async surface. The
        event loop still runs, but every N samples (SLICE_YIELD_EVERY) we
        hand control back so a dashboard consumer stays responsive. If a
        future reader needs true concurrent I/O (live Mongo, UDP) it can wrap
        the same stitching logic itself.
        """
        SLICE_YIELD_EVERY = 500
        for i, sample in enumerate(self._iter_samples()):
            yield sample
            if i % SLICE_YIELD_EVERY == 0:
                await asyncio.sleep(0)

    def _iter_samples(self) -> Iterator[VehicleTelemetrySample]:
        streams = [_iter_frame_rows(path, spec) for path, spec in self._mapped.items()]
        merged = _merge_streams(streams)
        window: dict[str, dict[str, float]] = {}
        anchor: datetime | None = None
        last_time: datetime | None = None
        for when, csv_name, decoded in merged:
            if anchor is None or (when - anchor).total_seconds() > self._config.stitch_window_s:
                if window and anchor is not None:
                    yield self._emit(anchor, window)
                    window = {}
                if last_time is not None:
                    gap = (when - last_time).total_seconds()
                    if gap > self._config.gap_threshold_s:
                        self._gaps.append(
                            TelemetryGap(
                                start_utc=last_time,
                                end_utc=when,
                                expected_cadence_s=self._config.stitch_window_s,
                                missing_samples_estimate=int(
                                    gap // self._config.stitch_window_s
                                ),
                                reason="no frames arrived",
                            )
                        )
                anchor = when
            window[csv_name] = {**window.get(csv_name, {}), **decoded}
            last_time = when
        if window and anchor is not None:
            yield self._emit(anchor, window)

    def _emit(
        self, center: datetime, bag: dict[str, dict[str, float]]
    ) -> VehicleTelemetrySample:
        now = utc_now()
        return VehicleTelemetrySample(
            time_utc=center,
            source=self.source,
            position=self._position(center, bag, now),
            battery=self._battery(center, bag, now),
            array=self._array(center, bag, now),
            motor=self._motor(center, bag, now),
            aggregation_window_s=self._config.stitch_window_s,
            provenance=_provenance("stitched", now),
        )

    # --- stitching helpers -------------------------------------------------

    def _battery(
        self, when: datetime, bag: dict[str, dict[str, float]], now: datetime
    ) -> BatteryTelemetry | None:
        soc = bag.get("BMU_SoC.csv", {}).get("soc_fraction")
        bat = bag.get("battery_data.csv", {})
        temps = bag.get("cell_max_min_temp.csv", {})
        voltage = bat.get("pack_voltage_v")
        current = bat.get("pack_current_a")
        power = voltage * current if voltage is not None and current is not None else None
        if not any((soc, voltage, current, temps)):
            return None
        return BatteryTelemetry(
            time_utc=when,
            state_of_charge_fraction=soc,
            pack_voltage_v=voltage,
            pack_current_a=current,
            pack_power_w=power,
            min_cell_temperature_c=temps.get("min_cell_temperature_c"),
            max_cell_temperature_c=temps.get("max_cell_temperature_c"),
            provenance=_provenance("battery_stitched", now),
        )

    def _array(
        self, when: datetime, bag: dict[str, dict[str, float]], now: datetime
    ) -> ArrayTelemetry | None:
        per_group: dict[str, float] = {}
        total = 0.0
        any_group = False
        for i in range(1, 5):
            key = f"MPPT{i}_output_data.csv"
            group = bag.get(key)
            if not group:
                continue
            voltage = group.get("mppt_voltage_v")
            current = group.get("mppt_current_a")
            if voltage is None or current is None:
                continue
            power = max(0.0, voltage * current)
            per_group[f"mppt{i}"] = power
            total += power
            any_group = True
        if not any_group:
            return None
        return ArrayTelemetry(
            time_utc=when,
            total_power_w=total,
            per_group_power_w=per_group,
            provenance=_provenance("array_stitched", now),
        )

    def _motor(
        self, when: datetime, bag: dict[str, dict[str, float]], now: datetime
    ) -> MotorTelemetry | None:
        motor = bag.get("MotorConFrm0.csv")
        gps_sp = bag.get("gps_speed.csv")
        if motor is None and gps_sp is None:
            return None
        rpm = motor.get("motor_rpm") if motor else None
        motor_current = motor.get("motor_current_a") if motor else None
        speed = gps_sp.get("speed_mps") if gps_sp else None
        bat_voltage = motor.get("bat_voltage_from_motor_v") if motor else None
        bat_current = motor.get("bat_current_from_motor_a") if motor else None
        bus_power = (
            bat_voltage * bat_current
            if bat_voltage is not None and bat_current is not None
            else None
        )
        return MotorTelemetry(
            time_utc=when,
            speed_mps=speed,
            bus_power_w=bus_power,
            motor_current_a=motor_current,
            motor_rpm=rpm,
            provenance=_provenance("motor_stitched", now),
        )

    def _position(
        self, when: datetime, bag: dict[str, dict[str, float]], now: datetime
    ) -> PositionFix | None:
        nav = bag.get("gps_nav.csv")
        if not nav:
            return None
        lat = nav.get("latitude_deg")
        lon = nav.get("longitude_deg")
        if lat is None or lon is None:
            return None
        speed_bag = bag.get("gps_speed.csv", {})
        return PositionFix(
            point=GeoPoint(latitude_deg=lat, longitude_deg=lon, elevation_m=None),
            mode=PositionMode.HISTORICAL_REPLAY,
            source="ju-solar-team race_data2025 CSV",
            received_at_utc=now,
            gps_time_utc=when,
            speed_mps=speed_bag.get("speed_mps"),
            course_deg=speed_bag.get("heading_deg"),
            fix_quality=FixQuality.SIMULATED,
        )
