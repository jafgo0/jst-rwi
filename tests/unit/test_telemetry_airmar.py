"""Airmar 200WX: configuration, true wind and 1 s windows (ADR 0010)."""

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rwi.telemetry.airmar import (
    AirmarConfig,
    AirmarWindower,
    SensorMount,
    build_airmar_window,
    true_wind,
)
from rwi.telemetry.nmea2000 import (
    CanboatDecoder,
    GnssRecord,
    HeadingRecord,
    N2kRecord,
    PositionRecord,
)
from rwi.telemetry.onboard import SensorConfigError

FIXTURE = Path(__file__).parents[1] / "fixtures" / "onboard" / "airmar_canboat.jsonl"
T0 = datetime(2027, 8, 22, 3, 15, tzinfo=UTC)


def _fixture_records() -> list[N2kRecord]:
    d = CanboatDecoder()
    return [r for line in FIXTURE.read_text().splitlines() for r in d.feed_line(line)]


# --------------------------------------------------------------------------
# configuration


def test_config_lists_every_missing_key() -> None:
    with pytest.raises(SensorConfigError) as exc:
        AirmarConfig.from_values(serial=None, mount=None, height_m=None)
    msg = str(exc.value)
    for key in ("RWI_AIRMAR_SERIAL", "RWI_AIRMAR_MOUNT", "RWI_AIRMAR_HEIGHT_M"):
        assert key in msg


def test_config_rejects_bad_mount_and_height() -> None:
    with pytest.raises(SensorConfigError, match="roof") as exc:
        AirmarConfig.from_values(serial="A1", mount="roof", height_m=0.0)
    assert "must be > 0" in str(exc.value)


@pytest.mark.parametrize(
    ("height", "screen"), [(1.0, False), (1.25, True), (1.6, True), (2.0, True), (2.4, False)]
)
def test_screen_height_band(height: float, screen: bool) -> None:
    cfg = AirmarConfig.from_values(serial="A1", mount="support_car", height_m=height)
    assert cfg.mount is SensorMount.SUPPORT_CAR
    assert cfg.at_screen_height is screen


# --------------------------------------------------------------------------
# true wind (vector sum of apparent wind and velocity over ground)


@pytest.mark.science
def test_true_wind_is_calm_when_apparent_wind_is_only_motion() -> None:
    # Driving north at 10 m/s in still air: 10 m/s apparent wind on the bow.
    tw = true_wind(
        apparent_speed_mps=10.0,
        apparent_angle_deg=0.0,
        heading_true_deg=0.0,
        cog_deg=0.0,
        sog_mps=10.0,
    )
    assert tw.speed_mps == pytest.approx(0.0, abs=1e-9)


@pytest.mark.science
def test_true_wind_headwind_case_from_fixture() -> None:
    # Heading south at 20 m/s, 25 m/s apparent on the bow: 5 m/s true from the south.
    tw = true_wind(
        apparent_speed_mps=25.0,
        apparent_angle_deg=0.0,
        heading_true_deg=180.0,
        cog_deg=180.0,
        sog_mps=20.0,
    )
    assert tw.speed_mps == pytest.approx(5.0)
    assert tw.dir_from_deg == pytest.approx(180.0)


@pytest.mark.science
def test_true_wind_crosswind() -> None:
    # Heading north at 10 m/s with a 10 m/s easterly: the apparent wind is
    # 10*sqrt(2) m/s from 45 deg off the starboard bow (textbook vector triangle).
    tw = true_wind(
        apparent_speed_mps=10.0 * math.sqrt(2.0),
        apparent_angle_deg=45.0,
        heading_true_deg=0.0,
        cog_deg=0.0,
        sog_mps=10.0,
    )
    assert tw.speed_mps == pytest.approx(10.0)
    assert tw.dir_from_deg == pytest.approx(90.0)


@pytest.mark.science
def test_true_wind_standing_still_is_apparent_wind_rotated_to_north() -> None:
    tw = true_wind(
        apparent_speed_mps=7.0,
        apparent_angle_deg=90.0,
        heading_true_deg=90.0,
        cog_deg=None,
        sog_mps=0.0,
    )
    assert tw.speed_mps == pytest.approx(7.0)
    assert tw.dir_from_deg == pytest.approx(180.0)


def test_true_wind_needs_cog_when_moving() -> None:
    with pytest.raises(ValueError, match="COG"):
        true_wind(
            apparent_speed_mps=5.0,
            apparent_angle_deg=0.0,
            heading_true_deg=0.0,
            cog_deg=None,
            sog_mps=3.0,
        )


# --------------------------------------------------------------------------
# windows


def test_fixture_gives_three_windows_with_true_wind() -> None:
    windower = AirmarWindower(late_tolerance_s=0.5)
    windows = [w for r in _fixture_records() for w in windower.add(r)] + windower.flush()
    assert [w.start_utc for w in windows] == [T0 + timedelta(seconds=s) for s in range(3)]
    assert windower.late_count == 0
    w = windows[0]
    assert w.values["apparent_wind_speed_mps"] == pytest.approx(25.0)
    assert w.values["true_wind_speed_mps"] == pytest.approx(5.0, abs=0.01)
    assert w.values["true_wind_dir_from_deg"] == pytest.approx(180.0, abs=0.1)
    assert w.values["air_temperature_degc"] == pytest.approx(30.0)
    assert w.values["relative_humidity_percent"] == pytest.approx(40.0)
    assert w.values["pressure_pa"] == 100800.0
    assert w.values["altitude_m"] == 210.0
    assert w.position is not None and w.position.latitude_deg == pytest.approx(-16.25)
    assert (w.fix_method, w.satellites, w.hdop) == ("GNSS fix", 9, 0.9)
    assert "heading_reference=True" in w.notes
    assert windows[2].position is not None
    assert windows[2].position.latitude_deg == pytest.approx(-16.25036)


def _heading(heading: float, ref: str, variation: float | None, deviation: float | None = None):
    return HeadingRecord(
        time_utc=T0,
        src=3,
        heading_deg=heading,
        deviation_deg=deviation,
        variation_deg=variation,
        reference=ref,
    )


def test_magnetic_heading_needs_variation() -> None:
    w = build_airmar_window(T0, [_heading(10.0, "Magnetic", 5.0, deviation=1.0)])
    assert w.values["heading_true_deg"] == pytest.approx(16.0)
    assert "heading_reference=Magnetic+variation" in w.notes

    w = build_airmar_window(T0, [_heading(10.0, "Magnetic", None)])
    assert "heading_true_deg" not in w.values
    assert w.notes == ()


def test_no_gnss_method_drops_the_position() -> None:
    records: list[N2kRecord] = [
        PositionRecord(time_utc=T0, src=3, pgn=129029, latitude_deg=-16.0, longitude_deg=133.0),
        GnssRecord(time_utc=T0, src=3, gnss_time_utc=T0, method="no GNSS", satellites=0, hdop=None),
    ]
    w = build_airmar_window(T0, records)
    assert w.position is None
    assert w.fix_method == "no GNSS"
    assert w.sample_count == 2


def test_empty_window_has_no_values() -> None:
    w = build_airmar_window(T0, [])
    assert w.values == {} and w.position is None and w.sample_count == 0
