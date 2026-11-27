"""On-board sensor pipeline: drivers -> spool -> live position + archive (ADR 0010 §5-6).

Data flow for every closed 1 s window::

    canboat / ADC line -> windower -> SensorSpool.put -> live sink (Airmar position)
                                                    \\-> archive sink (interval series)

The spool is written first, so a sink that is down (archive locked, uplink
lost) only delays that sink. Each sink marks what it dealt with and how; the
rest stays pending and is drained oldest-first later. Nothing is interpolated,
and a window that arrives after its archive interval was written is recorded
as ``late_after_archive`` instead of becoming a second series for that time.
"""

import asyncio
import logging
import math
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rwi.archive.store import SqliteArchive
from rwi.config import Settings
from rwi.contracts import (
    TelemetryGap,
    TelemetrySource,
    VehicleTelemetrySample,
    WeatherSeries,
    WeatherStep,
)
from rwi.core.geo import GeoPoint
from rwi.core.position import FixQuality, PositionFix, PositionMode
from rwi.core.provenance import DataKind, Measured, Provenance, QualityFlag, measured
from rwi.core.state import StateStore
from rwi.core.timebase import ensure_utc, utc_now
from rwi.core.units import wrap_deg_360
from rwi.core.variables import spec_for
from rwi.core.wind import met_to_uv, uv_to_met
from rwi.telemetry.airmar import (
    AIRMAR_STREAM,
    TRUE_WIND_PROCESSING,
    AirmarConfig,
    AirmarWindower,
)
from rwi.telemetry.ml01 import (
    ML01_OUTPUT_RANGE_MV,
    ML01_STREAM,
    Ml01Config,
    Ml01Mount,
    Ml01Parser,
    Ml01Windower,
    level_verdict,
    provenance_notes,
)
from rwi.telemetry.nmea2000 import CanboatDecoder
from rwi.telemetry.onboard import WINDOW_S, SensorConfigError, SensorWindow, mean
from rwi.telemetry.spool import SensorSpool

log = logging.getLogger(__name__)

ONBOARD_PROVIDER = "jst-onboard"
SINK_ARCHIVE = "archive"
SINK_LIVE = "live"

#: A window older than this is never shown as the live position (coordinator gate is +60 s).
LIVE_MAX_AGE_S = 60.0

TEMPERATURE_BIAS_NOTE = (
    "vehicle-mounted: aspirated by vehicle motion, exposed to road-surface heating"
)
SOLAR_CAR_HEAT_NOTE = "solar car: array heat may bias air temperature and humidity"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OnboardConfig:
    """Validated pipeline settings. At least one sensor is configured."""

    airmar: AirmarConfig | None
    ml01: Ml01Config | None
    spool_path: Path
    archive_interval_s: int
    late_tolerance_s: float

    @classmethod
    def from_settings(cls, s: Settings) -> "OnboardConfig":
        """Build from settings; a sensor with any of its keys set must be fully configured."""
        airmar_keys = (s.airmar_serial, s.airmar_mount, s.airmar_height_m, s.airmar_canboat_command)
        ml01_keys = (
            s.ml01_sensitivity_uv_per_wm2,
            s.ml01_serial,
            s.ml01_calibration_date,
            s.ml01_input_unit,
            s.ml01_mount,
            s.ml01_command,
        )
        airmar = None
        if any(k is not None for k in airmar_keys):
            airmar = AirmarConfig.from_values(
                serial=s.airmar_serial,
                mount=s.airmar_mount,
                height_m=s.airmar_height_m,
                drive_position=s.airmar_drive_position,
                naive_utc=s.airmar_canboat_naive_utc,
                command=s.airmar_canboat_command,
            )
        ml01 = None
        if any(k is not None for k in ml01_keys):
            ml01 = Ml01Config.from_values(
                sensitivity_uv_per_wm2=s.ml01_sensitivity_uv_per_wm2,
                serial=s.ml01_serial,
                calibration_date=s.ml01_calibration_date,
                input_unit=s.ml01_input_unit,
                mount=s.ml01_mount,
                level_tolerance_deg=s.ml01_level_tolerance_deg,
                assume_level=s.ml01_assume_level,
                command=s.ml01_command,
            )
        if airmar is None and ml01 is None:
            raise SensorConfigError(
                "onboard sensors enabled but neither RWI_AIRMAR_* nor RWI_ML01_* is configured"
            )
        if ml01 is not None and ml01.mount is Ml01Mount.SHARED_WITH_AIRMAR and airmar is None:
            raise SensorConfigError("RWI_ML01_MOUNT=shared_with_airmar needs the Airmar configured")
        if s.sensor_archive_interval_s < WINDOW_S:
            raise SensorConfigError("RWI_SENSOR_ARCHIVE_INTERVAL_S must be >= 1")
        return cls(
            airmar,
            ml01,
            s.sensor_spool_path,
            int(s.sensor_archive_interval_s),
            float(s.sensor_late_tolerance_s),
        )

    def ml01_position_from_airmar(self) -> bool:
        """True when the Airmar sits on the same vehicle as the ML-01."""
        if self.ml01 is None or self.airmar is None:
            return False
        if self.ml01.mount is Ml01Mount.SHARED_WITH_AIRMAR:
            return True
        return self.ml01.mount.value == self.airmar.mount.value


# ---------------------------------------------------------------------------
# Gaps
# ---------------------------------------------------------------------------


@dataclass
class GapTracker:
    """Turn missing 1 s windows of each stream into :class:`TelemetryGap` records."""

    gaps: list[TelemetryGap] = field(default_factory=list)
    _last_end: dict[str, datetime] = field(default_factory=dict)

    def see(self, window: SensorWindow) -> TelemetryGap | None:
        start = ensure_utc(window.start_utc)
        last = self._last_end.get(window.stream)
        gap = None
        if last is not None and start > last:
            missing = round((start - last).total_seconds() / WINDOW_S)
            gap = TelemetryGap(
                start_utc=last,
                end_utc=start,
                expected_cadence_s=float(WINDOW_S),
                missing_samples_estimate=missing,
                reason=f"{window.stream}: no readings for {missing} window(s)",
            )
            self.gaps.append(gap)
        if last is None or window.end_utc > last:
            self._last_end[window.stream] = ensure_utc(window.end_utc)
        return gap


# ---------------------------------------------------------------------------
# Live sink: Airmar GNSS -> VehicleTelemetrySample
# ---------------------------------------------------------------------------


def _fix_quality(method: str | None) -> FixQuality | None:
    """Map the PGN 129029 method to a fix quality; None means "do not use as a fix"."""
    if method is None:
        return FixQuality.UNKNOWN
    m = method.lower()
    if m.startswith("rtk"):
        return FixQuality.RTK
    if m.startswith("dgnss"):
        return FixQuality.DGPS
    if m in {"gnss fix", "precise gnss"}:
        return FixQuality.GPS
    return None  # "no GNSS", dead reckoning, manual input, simulator mode


class OnboardLiveReader:
    """Queue-backed :class:`~rwi.contracts.TelemetryReader` fed with Airmar windows.

    Live: source LIVE_ONBOARD_SENSOR, mode LIVE_GPS, windows older than
    :data:`LIVE_MAX_AGE_S` are refused. Replay: source FILE_REPLAY, mode
    HISTORICAL_REPLAY, no age check.
    """

    def __init__(
        self,
        airmar: AirmarConfig,
        gaps: GapTracker,
        *,
        replay: bool,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._airmar = airmar
        self._gaps = gaps
        self._replay = replay
        self._now = now
        self.source = TelemetrySource.FILE_REPLAY if replay else TelemetrySource.LIVE_ONBOARD_SENSOR
        self._queue: asyncio.Queue[VehicleTelemetrySample | None] = asyncio.Queue()
        self._ended = False
        self.sent = 0

    @property
    def fix_source(self) -> str:
        return f"{AIRMAR_STREAM} {self._airmar.serial} ({self._airmar.mount.value})"

    def to_sample(self, window: SensorWindow) -> tuple[VehicleTelemetrySample | None, str]:
        """The sample for one Airmar window and the outcome to record in the spool."""
        now = self._now()
        end = ensure_utc(window.end_utc)
        if not self._replay and (now - end).total_seconds() > LIVE_MAX_AGE_S:
            return None, "live_stale"
        quality = _fix_quality(window.fix_method)
        if window.position is None or quality is None:
            return None, "no_position"
        cog = window.values.get("cog_deg")
        fix = PositionFix(
            point=window.position,
            mode=PositionMode.HISTORICAL_REPLAY if self._replay else PositionMode.LIVE_GPS,
            source=self.fix_source,
            received_at_utc=end if self._replay else now,
            gps_time_utc=window.gnss_time_utc,
            speed_mps=window.values.get("sog_mps"),
            course_deg=wrap_deg_360(cog) if cog is not None else None,
            fix_quality=quality,
            satellites=window.satellites,
        )
        provenance = Provenance(
            provider=ONBOARD_PROVIDER,
            dataset=AIRMAR_STREAM,
            kind=DataKind.OBSERVATION,
            retrieved_at_utc=now,
            observation_time_utc=end,
            point=window.position,
            temporal_resolution_s=float(WINDOW_S),
            processing="mean of one 1 s window",
            notes=f"serial={self._airmar.serial}; mount={self._airmar.mount.value}",
        )
        sample = VehicleTelemetrySample(
            time_utc=end,
            source=self.source,
            position=fix,
            aggregation_window_s=float(WINDOW_S),
            provenance=provenance,
        )
        return sample, "sent"

    def offer(self, window: SensorWindow) -> str:
        """Queue the sample for ``window`` if it is usable; return the outcome."""
        sample, outcome = self.to_sample(window)
        if sample is not None and not self._ended:
            self._queue.put_nowait(sample)
            self.sent += 1
        return outcome

    def end(self) -> None:
        """No more samples will come; ``stream()`` finishes after the queued ones."""
        if not self._ended:
            self._ended = True
            self._queue.put_nowait(None)

    async def stream(self) -> AsyncIterator[VehicleTelemetrySample]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def gaps_so_far(self) -> list[TelemetryGap]:
        return [g for g in self._gaps.gaps if AIRMAR_STREAM in (g.reason or "")]

    async def close(self) -> None:
        self.end()


# ---------------------------------------------------------------------------
# Archive sink: pending windows -> one OBSERVATION series per stream interval
# ---------------------------------------------------------------------------


@dataclass
class ArchiveReport:
    series_written: int = 0
    values_written: int = 0
    outcomes: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    failed: bool = False


def _floor(when: datetime, step_s: int) -> datetime:
    epoch = int(ensure_utc(when).timestamp())
    return datetime.fromtimestamp(epoch - epoch % step_s, tz=UTC)


def _mean_point(points: Iterable[GeoPoint | None]) -> GeoPoint | None:
    pts = [p for p in points if p is not None]
    if not pts:
        return None
    lat = mean(p.latitude_deg for p in pts)
    lon = mean(p.longitude_deg for p in pts)
    assert lat is not None and lon is not None
    return GeoPoint(latitude_deg=lat, longitude_deg=lon)


def _range_flag(variable: str, value: float) -> QualityFlag:
    spec = spec_for(variable)
    return QualityFlag.OK if spec.minimum <= value <= spec.maximum else QualityFlag.OUT_OF_RANGE


@dataclass(frozen=True)
class _Built:
    series: WeatherSeries | None
    outcome: str


class ArchiveSink:
    """Write spool windows to the SQLite archive as interval-mean observations."""

    def __init__(
        self,
        config: OnboardConfig,
        spool: SensorSpool,
        archive: SqliteArchive,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._config = config
        self._spool = spool
        self._archive = archive
        self._now = now
        self._warned_no_position = False

    @property
    def interval(self) -> timedelta:
        return timedelta(seconds=self._config.archive_interval_s)

    def _streams(self) -> list[str]:
        out = []
        if self._config.airmar is not None:
            out.append(AIRMAR_STREAM)
        if self._config.ml01 is not None:
            out.append(ML01_STREAM)
        return out

    def _ready(self, stream: str, end: datetime, force: bool) -> bool:
        if force:
            return True
        own = self._spool.newest_end(stream)
        if own is None or own < end:
            return False
        if stream == ML01_STREAM and self._config.ml01_position_from_airmar():
            airmar = self._spool.newest_end(AIRMAR_STREAM)
            # Wait for the Airmar to cover the interval, but not forever: one more
            # interval of ML-01 data without it means the Airmar is not coming.
            return (airmar is not None and airmar >= end) or own >= end + self.interval
        return True

    def drain(self, *, force: bool = False) -> ArchiveReport:
        """Archive every ready interval, oldest first. ``force`` also takes open ones."""
        report = ArchiveReport()
        for stream in self._streams():
            groups: dict[datetime, list[SensorWindow]] = defaultdict(list)
            for w in self._spool.pending(SINK_ARCHIVE, stream=stream):
                groups[_floor(w.start_utc, self._config.archive_interval_s)].append(w)
            for start in sorted(groups):
                end = start + self.interval
                windows = groups[start]
                if not self._ready(stream, end, force):
                    break
                if self._spool.forwarded_between(SINK_ARCHIVE, stream, start, end):
                    outcome = "late_after_archive"
                    log.warning("%d late %s window(s) for archived %s", len(windows), stream, start)
                else:
                    try:
                        built = (
                            self._build_airmar(start, windows)
                            if stream == AIRMAR_STREAM
                            else self._build_ml01(start, windows)
                        )
                    except SensorConfigError as exc:
                        # Config gap found only in the data: keep the windows for
                        # backfill after the fix, keep ingesting into the spool.
                        log.error("%s: %s; %s kept pending in spool", stream, exc, start)
                        report.failed = True
                        break
                    outcome = built.outcome
                    if built.series is not None:
                        try:
                            written = self._archive.record_series(built.series)
                        except Exception:
                            log.exception("archive write failed; %s kept pending in spool", stream)
                            report.failed = True
                            break
                        report.series_written += 1
                        report.values_written += written
                self._spool.mark_forwarded(SINK_ARCHIVE, windows, outcome=outcome)
                report.outcomes[outcome] += len(windows)
        return report

    def _provenance(
        self,
        stream: str,
        serial: str,
        point: GeoPoint,
        end: datetime,
        retrieved: datetime,
        processing: str,
        notes: list[str],
    ) -> Provenance:
        return Provenance(
            provider=ONBOARD_PROVIDER,
            dataset=stream,
            kind=DataKind.OBSERVATION,
            retrieved_at_utc=retrieved,
            valid_time_utc=end,
            observation_time_utc=end,
            model_name=serial,
            point=point,
            temporal_resolution_s=float(self._config.archive_interval_s),
            processing=processing,
            notes="; ".join(notes),
        )

    def _series(
        self,
        stream: str,
        serial: str,
        point: GeoPoint,
        end: datetime,
        retrieved: datetime,
        values: dict[str, Measured],
    ) -> WeatherSeries:
        return WeatherSeries(
            point=point,
            provider=ONBOARD_PROVIDER,
            dataset=stream,
            kind=DataKind.OBSERVATION,
            model_name=serial,
            retrieved_at_utc=retrieved,
            temporal_resolution_s=float(self._config.archive_interval_s),
            steps=[WeatherStep(valid_time_utc=end, values=values)],
        )

    def _build_airmar(self, start: datetime, windows: list[SensorWindow]) -> _Built:
        cfg = self._config.airmar
        assert cfg is not None
        end = start + self.interval
        point = _mean_point(w.position for w in windows)
        if point is None:
            return _Built(None, "no_position")
        retrieved = self._now()
        coverage = f"windows={len(windows)}/{self._config.archive_interval_s}"
        base = [f"serial={cfg.serial}", f"mount={cfg.mount.value}", f"height_m={cfg.height_m:g}"]
        heading_refs = sorted({n for w in windows for n in w.notes if n.startswith("heading_")})
        values: dict[str, Measured] = {}

        def put(variable: str, value: float, processing: str, notes: list[str]) -> None:
            prov = self._provenance(
                AIRMAR_STREAM, cfg.serial, point, end, retrieved, processing, notes
            )
            values[variable] = measured(variable, value, prov, quality=_range_flag(variable, value))

        winds = [
            (w.values["true_wind_speed_mps"], w.values["true_wind_dir_from_deg"])
            for w in windows
            if "true_wind_speed_mps" in w.values and "true_wind_dir_from_deg" in w.values
        ]
        if winds:
            n = f"mean of {len(winds)} 1 s windows"
            wind_notes = [*base, coverage, f"true-wind windows={len(winds)}", *heading_refs]
            put(
                "wind_speed_sensor",
                sum(s for s, _ in winds) / len(winds),
                f"{TRUE_WIND_PROCESSING}; scalar {n}",
                wind_notes,
            )
            uv = [met_to_uv(s, d) for s, d in winds]
            u = sum(c[0] for c in uv) / len(uv)
            v = sum(c[1] for c in uv) / len(uv)
            if math.hypot(u, v) > 0:
                put(
                    "wind_direction_sensor",
                    uv_to_met(u, v)[1],
                    f"{TRUE_WIND_PROCESSING}; vector {n}",
                    wind_notes,
                )

        thermo_notes = [*base, coverage, TEMPERATURE_BIAS_NOTE]
        if cfg.mount.value == "solar_car":
            thermo_notes.append(SOLAR_CAR_HEAT_NOTE)
        for key, variable in (
            ("air_temperature_degc", "temperature_2m"),
            ("relative_humidity_percent", "relative_humidity_2m"),
        ):
            vals = [w.values[key] for w in windows if key in w.values]
            if vals and cfg.at_screen_height:
                put(
                    variable,
                    sum(vals) / len(vals),
                    f"mean of {len(vals)} 1 s windows",
                    thermo_notes,
                )
        pres = [w.values["pressure_pa"] for w in windows if "pressure_pa" in w.values]
        if pres:
            put(
                "surface_pressure",
                sum(pres) / len(pres),
                f"mean of {len(pres)} 1 s windows",
                [*base, coverage, f"pressure at sensor height {cfg.height_m:g} m, not reduced"],
            )
        if not values:
            return _Built(None, "no_values")
        return _Built(
            self._series(AIRMAR_STREAM, cfg.serial, point, end, retrieved, values), "archived"
        )

    def _build_ml01(self, start: datetime, windows: list[SensorWindow]) -> _Built:
        cfg = self._config.ml01
        assert cfg is not None
        end = start + self.interval
        from_airmar = self._config.ml01_position_from_airmar()
        airmar = (
            {w.start_utc: w for w in self._spool.windows(AIRMAR_STREAM, start, end)}
            if from_airmar
            else {}
        )
        point = _mean_point(w.position for w in airmar.values())
        if point is None:
            if not from_airmar and not self._warned_no_position:
                log.warning(
                    "ML-01 (mount=%s) has no position source on its vehicle; "
                    "its readings stay in the spool and are not archived",
                    cfg.mount.value,
                )
                self._warned_no_position = True
            return _Built(None, "no_position")

        shared = cfg.mount is Ml01Mount.SHARED_WITH_AIRMAR
        level: list[SensorWindow] = []
        for w in windows:
            if "irradiance_wm2" not in w.values:
                continue
            pitch, roll = w.values.get("pitch_deg"), w.values.get("roll_deg")
            if (pitch is None or roll is None) and shared and w.start_utc in airmar:
                a = airmar[w.start_utc]
                pitch, roll = a.values.get("pitch_deg"), a.values.get("roll_deg")
            verdict = level_verdict(
                pitch_deg=pitch,
                roll_deg=roll,
                tolerance_deg=cfg.level_tolerance_deg,
                assume_level=cfg.assume_level,
            )
            if verdict.level:
                level.append(w)
        readable = sum(1 for w in windows if "irradiance_wm2" in w.values)
        if readable == 0:
            return _Built(None, "no_values")
        if not level:
            return _Built(None, "not_level")

        retrieved = self._now()
        ghi = sum(w.values["irradiance_wm2"] for w in level) / len(level)
        mv_max = max(w.values.get("output_mv_max", 0.0) for w in level)
        quality = _range_flag("ghi", ghi)
        if mv_max > ML01_OUTPUT_RANGE_MV[1]:
            quality = QualityFlag.OUT_OF_RANGE
        notes = [
            *provenance_notes(cfg, end.date()),
            f"windows={len(windows)}/{self._config.archive_interval_s}",
            f"level windows={len(level)}/{readable}",
        ]
        if cfg.assume_level:
            notes.append("level assumed by configuration where tilt was unknown")
        if mv_max > ML01_OUTPUT_RANGE_MV[1]:
            notes.append(f"output {mv_max:.2f} mV above the {ML01_OUTPUT_RANGE_MV[1]:g} mV range")
        prov = self._provenance(
            ML01_STREAM,
            cfg.serial,
            point,
            end,
            retrieved,
            f"mean of {len(level)} level 1 s windows",
            notes,
        )
        values = {"ghi": measured("ghi", ghi, prov, quality=quality)}
        series = self._series(ML01_STREAM, cfg.serial, point, end, retrieved, values)
        return _Built(series, "archived")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


@dataclass
class IngestStats:
    windows_new: int = 0
    windows_repeated: int = 0
    gaps: int = 0
    live_outcomes: dict[str, int] = field(default_factory=lambda: defaultdict(int))


class OnboardPipeline:
    """Drivers -> spool -> sinks for one process. Not thread-safe; one event loop."""

    def __init__(
        self,
        config: OnboardConfig,
        spool: SensorSpool,
        archive: SqliteArchive | None,
        *,
        replay: bool,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.config = config
        self.spool = spool
        self.replay = replay
        self.gaps = GapTracker()
        self.stats = IngestStats()
        self.decoder = (
            CanboatDecoder(naive_is_utc=config.airmar.naive_utc) if config.airmar else None
        )
        self.airmar_windower = (
            AirmarWindower(late_tolerance_s=config.late_tolerance_s) if config.airmar else None
        )
        self.ml01_parser = Ml01Parser() if config.ml01 else None
        self.ml01_windower = (
            Ml01Windower(config.ml01, late_tolerance_s=config.late_tolerance_s)
            if config.ml01
            else None
        )
        self.live: OnboardLiveReader | None = None
        if config.airmar is not None and config.airmar.drive_position:
            self.live = OnboardLiveReader(config.airmar, self.gaps, replay=replay, now=now)
        self.archive_sink = (
            ArchiveSink(config, spool, archive, now=now) if archive is not None else None
        )

    def _accept(self, windows: list[SensorWindow]) -> None:
        if not windows:
            return
        new = self.spool.put(windows)
        self.stats.windows_new += new
        self.stats.windows_repeated += len(windows) - new
        for w in windows:
            if self.gaps.see(w) is not None:
                self.stats.gaps += 1
            if w.stream == AIRMAR_STREAM and self.live is not None:
                self.offer_live(w)

    def offer_live(self, window: SensorWindow) -> None:
        assert self.live is not None
        outcome = self.live.offer(window)
        if self.spool.mark_forwarded(SINK_LIVE, [window], outcome=outcome):
            self.stats.live_outcomes[outcome] += 1

    def feed_canboat(self, line: str) -> None:
        if self.decoder is None or self.airmar_windower is None:
            raise SensorConfigError("Airmar is not configured")
        for record in self.decoder.feed_line(line):
            self._accept(self.airmar_windower.add(record))

    def feed_ml01(self, line: str) -> None:
        if self.ml01_parser is None or self.ml01_windower is None:
            raise SensorConfigError("ML-01 is not configured")
        reading = self.ml01_parser.feed_line(line)
        if reading is not None:
            self._accept(self.ml01_windower.add(reading))

    def resume_live(self) -> None:
        """After a restart: settle Airmar windows the live sink never saw (they are stale)."""
        if self.live is None:
            return
        for w in self.spool.pending(SINK_LIVE, stream=AIRMAR_STREAM):
            self.offer_live(w)

    def drain(self, *, force: bool = False) -> ArchiveReport | None:
        return self.archive_sink.drain(force=force) if self.archive_sink else None

    def finish(self) -> ArchiveReport | None:
        """End of input: close open windows; replay also archives the last partial interval."""
        if self.airmar_windower is not None:
            self._accept(self.airmar_windower.flush())
        if self.ml01_windower is not None:
            self._accept(self.ml01_windower.flush())
        report = self.drain(force=self.replay)
        if self.live is not None:
            self.live.end()
        return report


DRAIN_EVERY_S = 5.0


async def _pump(command: str, feed: Callable[[str], None], name: str) -> None:
    """Run ``command`` and feed each stdout line; a line that fails is logged and skipped."""
    proc = await asyncio.create_subprocess_shell(command, stdout=asyncio.subprocess.PIPE)
    assert proc.stdout is not None
    try:
        while line := await proc.stdout.readline():
            try:
                feed(line.decode("utf-8", errors="replace"))
            except SensorConfigError:
                raise
            except Exception:
                log.exception("%s: could not ingest a line", name)
        code = await proc.wait()
        log.error("%s: reader process exited with code %s", name, code)
    finally:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()


async def run_live(settings: Settings, store: StateStore, config: OnboardConfig) -> None:
    """Ingest the configured sensors until cancelled. Closes the spool on the way out."""
    from rwi.archive.store import open_archive
    from rwi.telemetry.coordinator import TelemetryCoordinator

    spool = SensorSpool(config.spool_path)
    pipeline = OnboardPipeline(config, spool, open_archive(settings), replay=False)
    pipeline.resume_live()
    tasks: list[asyncio.Task[object]] = []
    if config.airmar is not None:
        if not config.airmar.command:
            raise SensorConfigError("RWI_AIRMAR_CANBOAT_COMMAND is required for live ingest")
        tasks.append(
            asyncio.create_task(_pump(config.airmar.command, pipeline.feed_canboat, AIRMAR_STREAM))
        )
    if config.ml01 is not None:
        if not config.ml01.command:
            raise SensorConfigError("RWI_ML01_COMMAND is required for live ingest")
        tasks.append(
            asyncio.create_task(_pump(config.ml01.command, pipeline.feed_ml01, ML01_STREAM))
        )
    if pipeline.live is not None:
        tasks.append(asyncio.create_task(TelemetryCoordinator(store).consume(pipeline.live)))
    try:
        while True:
            await asyncio.sleep(DRAIN_EVERY_S)
            await asyncio.to_thread(pipeline.drain)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        spool.close()


__all__ = [
    "LIVE_MAX_AGE_S",
    "ONBOARD_PROVIDER",
    "SINK_ARCHIVE",
    "SINK_LIVE",
    "ArchiveReport",
    "ArchiveSink",
    "GapTracker",
    "IngestStats",
    "OnboardConfig",
    "OnboardLiveReader",
    "OnboardPipeline",
    "run_live",
]
