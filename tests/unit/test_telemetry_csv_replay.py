"""CSV replay reader against the team's own export format.

The scalings in rwi.telemetry.frames were verified empirically against real
BWSC 2025 data on 2026-11-03: pack voltage 130 V from raw 130014 (so 0.001
V/count), not 1477 V from the dictionary's 0.01136 figure which applies to a
different frame. The tests pin those scalings and the shape of the output.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pytest

from rwi.contracts import (
    ArrayTelemetry,
    BatteryTelemetry,
    MotorTelemetry,
    TelemetrySource,
    VehicleTelemetrySample,
)
from rwi.core.position import FixQuality, PositionMode
from rwi.telemetry import CsvReplayReader, ReplayConfig, ReplayError
from rwi.telemetry.csv_replay import _parse_timestamp

# --------------------------------------------------------------------------
# timestamp parsing - the Mongo extended-JSON format


def test_mongo_extended_json_timestamp_parses_as_aware_utc() -> None:
    """The exporter writes `[{"$date":"..."}]` wrappings. A reader that
    treats them as strings-of-arbitrary-shape would silently drop every row."""
    dt = _parse_timestamp('[{"$date":"2025-07-08T15:47:02.175Z"}]')
    assert dt.year == 2025 and dt.month == 7 and dt.day == 8
    assert dt.hour == 15 and dt.minute == 47
    assert dt.utcoffset() is not None
    assert dt.utcoffset().total_seconds() == 0


def test_a_bare_iso_timestamp_also_works_as_a_fallback() -> None:
    """A simpler export format might emit just the string. Accept both."""
    dt = _parse_timestamp("2025-07-08T15:47:02.175Z")
    assert dt.year == 2025 and dt.hour == 15


def test_unparseable_timestamp_raises_rather_than_defaulting_to_now() -> None:
    """Silently defaulting a broken timestamp to 'now' would stamp a
    years-old sample with today's time and corrupt every verification."""
    for bad in ("", "not a date", "[]", "[{}]", "[{\"$date\": 42}]"):
        with pytest.raises(ReplayError):
            _parse_timestamp(bad)


# --------------------------------------------------------------------------
# full-stack replay from a hand-built fixture


def _write(path: Path, header: list[str], rows: list[list[Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _mongo_ts(seconds: int) -> str:
    base = f"2025-07-08T15:47:{seconds:02d}.000Z"
    return f'[{{"$date":"{base}"}}]'


def test_a_minimal_fixture_produces_a_stitched_sample(tmp_path: Path) -> None:
    """One row of each known frame type at the same second must stitch into
    exactly one VehicleTelemetrySample with every subsystem populated."""
    _write(tmp_path / "battery_data.csv",
           ["_id", "bat_curr", "bat_volt", "timestamp"],
           [["ObjectId(x)", "-87", "130014", _mongo_ts(2)]])
    _write(tmp_path / "BMU_SoC.csv",
           ["_id", "soc_perc", "soc_ah", "timestamp"],
           [["ObjectId(y)", "0.9990", "0.0273", _mongo_ts(2)]])
    _write(tmp_path / "MPPT1_output_data.csv",
           ["_id", "output_curr", "output_volt", "timestamp"],
           [["ObjectId(z)", "2.5", "140.0", _mongo_ts(2)]])
    _write(tmp_path / "MPPT2_output_data.csv",
           ["_id", "output_curr", "output_volt", "timestamp"],
           [["ObjectId(z)", "2.0", "140.0", _mongo_ts(2)]])
    _write(tmp_path / "gps_nav.csv",
           ["_id", "lat", "lon", "timestamp"],
           [["ObjectId(a)", "-165758450", "1317971667", _mongo_ts(2)]])
    _write(tmp_path / "gps_speed.csv",
           ["_id", "headMot", "gSpeed", "timestamp"],
           [["ObjectId(b)", "9000000", "22", _mongo_ts(2)]])
    _write(tmp_path / "MotorConFrm0.csv",
           ["_id", "regen", "duty", "rpm", "motor_temp", "ctrl_temp",
            "motor_curr_dir", "motor_curr", "bat_curr_dir", "bat_curr",
            "bat_volt", "timestamp"],
           [["ObjectId(c)", "0", "0", "500", "25", "28",
             "0", "5", "0", "9", "130", _mongo_ts(2)]])

    import asyncio

    async def _run() -> list[VehicleTelemetrySample]:
        reader = CsvReplayReader(ReplayConfig(root=tmp_path, stitch_window_s=1.0))
        out: list[VehicleTelemetrySample] = []
        async for sample in reader.stream():
            out.append(sample)
        await reader.close()
        return out

    samples = asyncio.run(_run())
    assert len(samples) == 1
    sample = samples[0]
    assert sample.source is TelemetrySource.FILE_REPLAY

    # Battery: 130.014 V from the empirical scaling, -8.7 A at 0.1 A/count.
    assert isinstance(sample.battery, BatteryTelemetry)
    assert sample.battery.pack_voltage_v == pytest.approx(130.014, abs=1e-6)
    assert sample.battery.pack_current_a == pytest.approx(-8.7, abs=1e-6)
    assert sample.battery.state_of_charge_fraction == pytest.approx(0.9990)
    # And pack power is computed from the two, consistent sign.
    assert sample.battery.pack_power_w == pytest.approx(-8.7 * 130.014, abs=1e-6)

    # Array: two groups at 350 W and 280 W.
    assert isinstance(sample.array, ArrayTelemetry)
    assert sample.array.per_group_power_w == {
        "mppt1": pytest.approx(350.0, abs=1e-6),
        "mppt2": pytest.approx(280.0, abs=1e-6),
    }
    assert sample.array.total_power_w == pytest.approx(630.0, abs=1e-6)

    # Motor: 500 rpm, 5 A, bus power from the motor-side voltages.
    assert isinstance(sample.motor, MotorTelemetry)
    assert sample.motor.motor_rpm == pytest.approx(500.0)
    assert sample.motor.motor_current_a == pytest.approx(5.0)
    assert sample.motor.bus_power_w == pytest.approx(9.0 * 130.0, abs=1e-6)
    # GPS speed is 22 counts of 0.001 m/s = 0.022 m/s. Not fast, but real.
    assert sample.motor.speed_mps == pytest.approx(0.022, abs=1e-6)

    # Position: 1e-7 scaling. GPS stays in HISTORICAL_REPLAY mode.
    assert sample.position is not None
    assert sample.position.point.latitude_deg == pytest.approx(-16.5758450, abs=1e-7)
    assert sample.position.point.longitude_deg == pytest.approx(131.7971667, abs=1e-7)
    assert sample.position.mode is PositionMode.HISTORICAL_REPLAY
    assert sample.position.fix_quality is FixQuality.SIMULATED


def test_frames_more_than_one_stitch_window_apart_become_separate_samples(
    tmp_path: Path,
) -> None:
    """Second 2 and second 10 are 8 s apart, which must not merge under the
    default half-second window."""
    _write(tmp_path / "battery_data.csv",
           ["_id", "bat_curr", "bat_volt", "timestamp"],
           [["x", "-10", "130000", _mongo_ts(2)],
            ["y", "-15", "131000", _mongo_ts(10)]])

    import asyncio

    async def _run() -> list[VehicleTelemetrySample]:
        reader = CsvReplayReader(ReplayConfig(root=tmp_path))
        out: list[VehicleTelemetrySample] = []
        async for sample in reader.stream():
            out.append(sample)
        return out

    samples = asyncio.run(_run())
    assert len(samples) == 2
    assert samples[0].time_utc.second == 2
    assert samples[1].time_utc.second == 10


def test_a_long_silence_registers_as_a_telemetry_gap(tmp_path: Path) -> None:
    """A 30-second silence should be reported as a TelemetryGap, not quietly
    bridged. A dashboard that catches up with no mention of the dropout is
    the failure mode this guards against."""
    _write(tmp_path / "battery_data.csv",
           ["_id", "bat_curr", "bat_volt", "timestamp"],
           [["x", "-10", "130000", _mongo_ts(0)],
            ["y", "-15", "131000", _mongo_ts(30)]])
    import asyncio

    async def _run() -> tuple[int, int]:
        reader = CsvReplayReader(ReplayConfig(root=tmp_path, gap_threshold_s=5.0))
        count = 0
        async for _ in reader.stream():
            count += 1
        gaps = await reader.gaps_so_far()
        return count, len(gaps)

    sample_count, gap_count = asyncio.run(_run())
    assert sample_count == 2
    assert gap_count == 1


def test_an_empty_directory_is_refused_loudly(tmp_path: Path) -> None:
    """A reader built against an empty or wrong directory would succeed
    silently and never yield - which looks exactly like a working radio
    link with no coverage."""
    with pytest.raises(ReplayError, match="no known frame CSVs"):
        CsvReplayReader(ReplayConfig(root=tmp_path))


def test_a_directory_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ReplayError, match="not a directory"):
        CsvReplayReader(ReplayConfig(root=tmp_path / "ghost"))


def test_unmapped_files_are_skipped_without_crashing(tmp_path: Path) -> None:
    """A new frame type the team adds must not stop us reading the ones we
    already understand."""
    _write(tmp_path / "battery_data.csv",
           ["_id", "bat_curr", "bat_volt", "timestamp"],
           [["x", "-10", "130000", _mongo_ts(2)]])
    _write(tmp_path / "something_new.csv",
           ["_id", "mystery", "timestamp"],
           [["x", "42", _mongo_ts(2)]])
    reader = CsvReplayReader(ReplayConfig(root=tmp_path))
    assert "battery_data.csv" in reader.mapped_frames
    assert "something_new.csv" not in reader.mapped_frames


def test_mapped_frames_matches_the_catalogue() -> None:
    """A regression test on the catalogue itself: if a frame disappears
    from frames.py we want to notice before the first replay silently
    drops the subsystem that frame fed."""
    from rwi.telemetry.frames import KNOWN_FRAMES

    expected = {
        "BMU_SoC.csv",
        "battery_data.csv",
        "cell_max_min_temp.csv",
        "gps_nav.csv",
        "gps_speed.csv",
        "MotorConFrm0.csv",
        "MPPT1_output_data.csv",
        "MPPT2_output_data.csv",
        "MPPT3_output_data.csv",
        "MPPT4_output_data.csv",
    }
    assert set(KNOWN_FRAMES) == expected
