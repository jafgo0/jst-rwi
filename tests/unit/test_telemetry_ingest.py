"""Onboard pipeline: spool -> archive/live sinks, backfill, replay and the CLI (ADR 0010)."""

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from rwi.archive.store import SqliteArchive
from rwi.config import Settings
from rwi.contracts import TelemetrySource, VehicleTelemetrySample, WeatherSeries
from rwi.core.position import PositionMode
from rwi.core.provenance import DataKind
from rwi.telemetry import onboard_cli
from rwi.telemetry.airmar import AIRMAR_STREAM
from rwi.telemetry.ingest import (
    ONBOARD_PROVIDER,
    SINK_ARCHIVE,
    SINK_LIVE,
    OnboardConfig,
    OnboardPipeline,
)
from rwi.telemetry.ml01 import ML01_STREAM
from rwi.telemetry.onboard import SensorConfigError, SensorWindow
from rwi.telemetry.spool import SensorSpool

FIXTURES = Path(__file__).parents[1] / "fixtures" / "onboard"
CANBOAT = FIXTURES / "airmar_canboat.jsonl"
ML01_CSV = FIXTURES / "ml01.csv"
T0 = datetime(2027, 8, 22, 3, 15, tzinfo=UTC)

AIRMAR = {
    "airmar_serial": "AIRMAR-TEST",
    "airmar_mount": "support_car",
    "airmar_height_m": 1.6,
    "airmar_canboat_command": "cat airmar.jsonl",
}
ML01 = {
    "ml01_sensitivity_uv_per_wm2": 7.0,
    "ml01_serial": "ML01-TEST",
    "ml01_calibration_date": date(2026, 3, 1),
    "ml01_input_unit": "mv",
    "ml01_mount": "support_car",
    "ml01_command": "cat ml01.csv",
}


def _settings(tmp_path: Path, **kw: object) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        cache_dir=tmp_path,
        archive_path=tmp_path / "archive.sqlite3",
        sensor_spool_path=tmp_path / "spool.sqlite3",
        **kw,
    )


def _config(tmp_path: Path, **kw: object) -> OnboardConfig:
    return OnboardConfig.from_settings(_settings(tmp_path, **kw))


class FakeArchive:
    """Collects series; ``fail`` makes every write raise like a locked database."""

    def __init__(self, *, fail: bool = False) -> None:
        self.series: list[WeatherSeries] = []
        self.fail = fail

    def record_series(self, series: WeatherSeries) -> int:
        if self.fail:
            raise OSError("database is locked")
        self.series.append(series)
        return sum(len(step.values) for step in series.steps)

    def by_dataset(self, dataset: str) -> WeatherSeries:
        [series] = [s for s in self.series if s.dataset == dataset]
        return series


def _feed(pipeline: OnboardPipeline, *, canboat: bool = True, ml01: bool = True) -> None:
    if canboat:
        for line in CANBOAT.read_text().splitlines():
            pipeline.feed_canboat(line)
    if ml01:
        for line in ML01_CSV.read_text().splitlines():
            pipeline.feed_ml01(line)


def _replay(
    tmp_path: Path, archive: FakeArchive | None, **kw: object
) -> tuple[OnboardPipeline, SensorSpool]:
    config = _config(tmp_path, **kw)
    spool = SensorSpool(config.spool_path)
    pipeline = OnboardPipeline(config, spool, archive, replay=True)  # type: ignore[arg-type]
    return pipeline, spool


# --------------------------------------------------------------------------
# configuration


def test_no_sensor_configured_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SensorConfigError, match="neither"):
        _config(tmp_path)


def test_partial_sensor_config_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(SensorConfigError, match="RWI_ML01_SERIAL"):
        _config(tmp_path, ml01_sensitivity_uv_per_wm2=7.0)


def test_shared_mount_needs_the_airmar(tmp_path: Path) -> None:
    with pytest.raises(SensorConfigError, match="shared_with_airmar"):
        _config(
            tmp_path, **{**ML01, "ml01_mount": "shared_with_airmar"}, ml01_level_tolerance_deg=2
        )


def test_archive_interval_must_cover_a_window(tmp_path: Path) -> None:
    with pytest.raises(SensorConfigError, match="INTERVAL"):
        _config(tmp_path, **AIRMAR, sensor_archive_interval_s=0)


# --------------------------------------------------------------------------
# archive sink


def test_replay_archives_airmar_interval_as_observation(tmp_path: Path) -> None:
    archive = FakeArchive()
    pipeline, spool = _replay(tmp_path, archive, **AIRMAR)
    _feed(pipeline, ml01=False)
    # Interval 60 s, 3 s of data: nothing is ready until the end of input.
    assert pipeline.drain().series_written == 0  # type: ignore[union-attr]
    report = pipeline.finish()
    assert report is not None and report.series_written == 1 and not report.failed
    assert report.outcomes == {"archived": 3}

    series = archive.by_dataset(AIRMAR_STREAM)
    assert series.kind is DataKind.OBSERVATION
    assert series.provider == ONBOARD_PROVIDER
    assert series.model_name == "AIRMAR-TEST"
    [step] = series.steps
    assert step.valid_time_utc == T0 + timedelta(seconds=60)
    assert step.value("wind_speed_sensor") == pytest.approx(5.0, abs=0.01)
    assert step.value("wind_direction_sensor") == pytest.approx(180.0, abs=0.1)
    assert step.value("temperature_2m") == pytest.approx(30.0)
    assert step.value("relative_humidity_2m") == pytest.approx(40.0)
    assert step.value("surface_pressure") == pytest.approx(100800.0)
    for m in step.values.values():
        assert m.provenance.kind is DataKind.OBSERVATION
        assert m.provenance.point is not None
        assert "serial=AIRMAR-TEST" in (m.provenance.notes or "")
        assert "windows=3/60" in (m.provenance.notes or "")
    wind = step.values["wind_speed_sensor"].provenance.processing or ""
    assert wind.startswith("true_wind_from_apparent+cog_sog")
    assert spool.pending(SINK_ARCHIVE) == []
    spool.close()


def test_airmar_off_screen_height_keeps_wind_but_not_temperature(tmp_path: Path) -> None:
    archive = FakeArchive()
    pipeline, spool = _replay(tmp_path, archive, **{**AIRMAR, "airmar_height_m": 0.9})
    _feed(pipeline, ml01=False)
    pipeline.finish()
    [step] = archive.by_dataset(AIRMAR_STREAM).steps
    assert "temperature_2m" not in step.values
    assert "relative_humidity_2m" not in step.values
    assert step.value("wind_speed_sensor") is not None
    spool.close()


@pytest.mark.science
@pytest.mark.parametrize(("tolerance", "ghi"), [(3.0, 800.0), (1.0, 820.0)])
def test_ml01_level_gate_uses_airmar_attitude_on_shared_mount(
    tmp_path: Path, tolerance: float, ghi: float
) -> None:
    # Airmar attitude in the fixture: pitch 1.0027 deg, roll -1.9996 deg
    # -> tilt acos(cos p * cos r) = 2.2372 deg. Second 1 of the ML-01 log
    # carries its own 0.5/0.5 deg (tilt 0.707 deg), which wins over the Airmar.
    # 5.60/5.74/5.46 mV at 7.00 uV/(W/m2) -> 800/820/780 W/m2.
    archive = FakeArchive()
    pipeline, spool = _replay(
        tmp_path,
        archive,
        **AIRMAR,
        **{**ML01, "ml01_mount": "shared_with_airmar"},
        ml01_level_tolerance_deg=tolerance,
    )
    _feed(pipeline)
    pipeline.finish()
    series = archive.by_dataset(ML01_STREAM)
    [step] = series.steps
    m = step.values["ghi"]
    assert m.value == pytest.approx(ghi)
    assert m.provenance.kind is DataKind.OBSERVATION
    assert m.provenance.point is not None
    notes = m.provenance.notes or ""
    assert "serial=" in notes or "ML01-TEST" in notes
    level = 3 if tolerance == 3.0 else 1
    assert f"level windows={level}/3" in notes
    spool.close()


def test_ml01_without_position_on_its_vehicle_is_not_archived(tmp_path: Path) -> None:
    archive = FakeArchive()
    pipeline, spool = _replay(tmp_path, archive, **{**AIRMAR, "airmar_mount": "solar_car"}, **ML01)
    _feed(pipeline)
    report = pipeline.finish()
    assert report is not None
    assert report.outcomes["no_position"] == 3
    assert [s.dataset for s in archive.series] == [AIRMAR_STREAM]
    assert spool.count(ML01_STREAM) == 3  # kept in the spool, never dropped
    spool.close()


def test_failed_archive_write_keeps_windows_pending_for_backfill(tmp_path: Path) -> None:
    pipeline, spool = _replay(tmp_path, FakeArchive(fail=True), **AIRMAR)
    _feed(pipeline, ml01=False)
    report = pipeline.finish()
    assert report is not None and report.failed and report.series_written == 0
    assert len(spool.pending(SINK_ARCHIVE)) == 3
    spool.close()

    # Next process: same spool file, archive back. Nothing lost, nothing interpolated.
    good = FakeArchive()
    pipeline, spool = _replay(tmp_path, good, **AIRMAR)
    report = pipeline.drain(force=True)
    assert report is not None and report.series_written == 1
    assert report.outcomes == {"archived": 3}
    assert "windows=3/60" in (
        good.by_dataset(AIRMAR_STREAM).steps[0].values["wind_speed_sensor"].provenance.notes or ""
    )
    assert spool.pending(SINK_ARCHIVE) == []
    spool.close()


def test_window_arriving_after_its_interval_was_archived_is_marked_late(
    tmp_path: Path,
) -> None:
    archive = FakeArchive()
    pipeline, spool = _replay(tmp_path, archive, **AIRMAR)
    _feed(pipeline, ml01=False)
    pipeline.finish()
    late = SensorWindow(
        stream=AIRMAR_STREAM,
        start_utc=T0 + timedelta(seconds=10),
        values={"pressure_pa": 100700.0},
        sample_count=1,
    )
    assert spool.put([late]) == 1
    report = pipeline.drain(force=True)
    assert report is not None
    assert report.outcomes == {"late_after_archive": 1}
    assert report.series_written == 0
    assert len(archive.series) == 1
    assert spool.outcomes(SINK_ARCHIVE)["late_after_archive"] == 1
    spool.close()


def test_repeated_input_is_idempotent(tmp_path: Path) -> None:
    archive = FakeArchive()
    pipeline, spool = _replay(tmp_path, archive, **AIRMAR)
    _feed(pipeline, ml01=False)
    pipeline.finish()
    pipeline2 = OnboardPipeline(pipeline.config, spool, archive, replay=True)  # type: ignore[arg-type]
    _feed(pipeline2, ml01=False)
    pipeline2.finish()
    assert pipeline2.stats.windows_new == 0
    assert pipeline2.stats.windows_repeated == 3
    assert len(archive.series) == 1
    spool.close()


def test_real_sqlite_archive_takes_the_series(tmp_path: Path) -> None:
    archive = SqliteArchive(tmp_path / "archive.sqlite3")
    config = _config(tmp_path, **AIRMAR, **ML01, ml01_level_tolerance_deg=3.0)
    spool = SensorSpool(config.spool_path)
    pipeline = OnboardPipeline(config, spool, archive, replay=True)
    _feed(pipeline)
    report = pipeline.finish()
    assert report is not None and not report.failed
    assert report.series_written == 2
    assert report.values_written >= 6
    archive.close()
    spool.close()


def test_missing_tolerance_found_in_data_keeps_windows_pending(tmp_path: Path) -> None:
    # The ML-01 log carries tilt in second 1 but no tolerance is configured:
    # the archive refuses to guess one, ingest keeps spooling, nothing is lost.
    archive = FakeArchive()
    pipeline, spool = _replay(tmp_path, archive, **AIRMAR, **ML01)
    _feed(pipeline)
    report = pipeline.finish()
    assert report is not None and report.failed
    assert [s.dataset for s in archive.series] == [AIRMAR_STREAM]
    assert len(spool.pending(SINK_ARCHIVE, stream=ML01_STREAM)) == 3
    spool.close()


# --------------------------------------------------------------------------
# live sink (position only)


def test_replay_live_samples_are_historical_replay(tmp_path: Path) -> None:
    pipeline, spool = _replay(tmp_path, None, **AIRMAR, airmar_drive_position=True)
    _feed(pipeline, ml01=False)
    pipeline.finish()
    assert pipeline.live is not None

    async def collect() -> list[VehicleTelemetrySample]:
        assert pipeline.live is not None
        return [s async for s in pipeline.live.stream()]

    samples = asyncio.run(collect())
    assert len(samples) == 3
    s = samples[0]
    assert s.source is TelemetrySource.FILE_REPLAY
    assert s.position is not None and s.position.mode is PositionMode.HISTORICAL_REPLAY
    assert s.provenance is not None and s.provenance.kind is DataKind.OBSERVATION
    assert s.time_utc == T0 + timedelta(seconds=1)
    assert spool.outcomes(SINK_LIVE) == {"sent": 3}
    spool.close()


def test_resume_live_settles_old_windows_as_stale(tmp_path: Path) -> None:
    # A spool written without the live sink, then a live process much later.
    pipeline, spool = _replay(tmp_path, None, **AIRMAR)
    _feed(pipeline, ml01=False)
    pipeline.finish()
    config = _config(tmp_path, **AIRMAR, airmar_drive_position=True)
    later = T0 + timedelta(days=1)
    live = OnboardPipeline(config, spool, None, replay=False, now=lambda: later)
    assert live.live is not None and live.live.source is TelemetrySource.LIVE_ONBOARD_SENSOR
    live.resume_live()
    assert spool.outcomes(SINK_LIVE) == {"live_stale": 3}
    assert live.live.sent == 0
    live.resume_live()  # nothing left to settle
    assert live.stats.live_outcomes == {"live_stale": 3}
    spool.close()


def test_feeding_an_unconfigured_sensor_raises(tmp_path: Path) -> None:
    pipeline, spool = _replay(tmp_path, None, **AIRMAR)
    with pytest.raises(SensorConfigError, match="ML-01"):
        pipeline.feed_ml01("2027-08-22T03:15:00Z,5.6")
    spool.close()


# --------------------------------------------------------------------------
# CLI replay


def test_cli_replay_prints_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _settings(tmp_path, **AIRMAR, **ML01, airmar_drive_position=True)
    monkeypatch.setattr(onboard_cli, "get_settings", lambda: settings)
    code = onboard_cli.main(
        [
            "replay",
            "--canboat",
            str(CANBOAT),
            "--ml01",
            str(ML01_CSV),
            "--spool",
            str(tmp_path / "replay.sqlite3"),
            "--no-archive",
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["windows_new"] == 6
    assert out["spool_windows"] == 6
    assert out["live_samples"] == 3
    assert out["live_outcomes"] == {"sent": 3}
    assert out["series_written"] == 0 and out["archive_failed"] is False


def test_cli_replay_needs_an_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(onboard_cli, "get_settings", lambda: _settings(tmp_path, **AIRMAR))
    assert onboard_cli.main(["replay", "--spool", str(tmp_path / "s.sqlite3")]) == 2
    assert "--canboat" in capsys.readouterr().err


def test_cli_replay_reports_config_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(onboard_cli, "get_settings", lambda: _settings(tmp_path))
    code = onboard_cli.main(
        ["replay", "--canboat", str(CANBOAT), "--spool", str(tmp_path / "s.sqlite3")]
    )
    assert code == 2
    assert "neither" in capsys.readouterr().err
