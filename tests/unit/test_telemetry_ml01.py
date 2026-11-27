"""EKO ML-01: required calibration config, parsing, windows and the level gate (ADR 0010)."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from rwi.telemetry.ml01 import (
    ML01_SENSOR_NOTE,
    Ml01Config,
    Ml01InputUnit,
    Ml01Parser,
    Ml01Reading,
    Ml01Windower,
    build_ml01_window,
    calibration_is_old,
    level_verdict,
    provenance_notes,
)
from rwi.telemetry.onboard import SensorConfigError

FIXTURE = Path(__file__).parents[1] / "fixtures" / "onboard" / "ml01.csv"
T0 = datetime(2027, 8, 22, 3, 15, tzinfo=UTC)


def _config(**kw: object) -> Ml01Config:
    values: dict[str, object] = {
        "sensitivity_uv_per_wm2": 7.0,
        "serial": "ML01-TEST",
        "calibration_date": date(2026, 3, 1),
        "input_unit": "mv",
        "mount": "support_car",
    }
    values.update(kw)
    return Ml01Config.from_values(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# configuration: no guessed calibration constants


def test_missing_config_names_every_key() -> None:
    with pytest.raises(SensorConfigError) as exc:
        Ml01Config.from_values(
            sensitivity_uv_per_wm2=None,
            serial=None,
            calibration_date=None,
            input_unit=None,
            mount=None,
        )
    msg = str(exc.value)
    assert msg.startswith("ML-01 configuration incomplete")
    for key in ("SENSITIVITY", "SERIAL", "CALIBRATION_DATE", "INPUT_UNIT", "MOUNT"):
        assert f"RWI_ML01_{key}" in msg


@pytest.mark.parametrize("sens", [0.0, -7.0, float("nan"), float("inf")])
def test_sensitivity_must_be_positive_and_finite(sens: float) -> None:
    with pytest.raises(SensorConfigError, match="positive"):
        _config(sensitivity_uv_per_wm2=sens)


def test_bad_unit_mount_and_tolerance() -> None:
    with pytest.raises(SensorConfigError) as exc:
        _config(input_unit="volts", mount="roof", level_tolerance_deg=-1.0)
    msg = str(exc.value)
    assert "'volts' is not mv|wm2" in msg
    assert "'roof'" in msg
    assert "must be >= 0" in msg


def test_shared_mount_needs_a_level_tolerance() -> None:
    with pytest.raises(SensorConfigError, match="required when tilt comes from the Airmar"):
        _config(mount="shared_with_airmar")
    assert _config(mount="shared_with_airmar", level_tolerance_deg=3.0).level_tolerance_deg == 3.0


@pytest.mark.science
def test_unit_conversion_uses_the_certificate_sensitivity() -> None:
    # E = V / S: 5.60 mV / 7.00 uV per W/m2 = 800 W/m2 (EKO ML-01 manual, sec. 6).
    mv = _config()
    assert mv.to_wm2(5.60) == pytest.approx(800.0)
    assert mv.to_mv(5.60) == 5.60
    wm2 = _config(input_unit="wm2")
    assert wm2.input_unit is Ml01InputUnit.WM2
    assert wm2.to_wm2(800.0) == 800.0
    assert wm2.to_mv(800.0) == pytest.approx(5.60)


# --------------------------------------------------------------------------
# calibration age


def test_calibration_age_warning() -> None:
    race = date(2027, 8, 22)
    assert not calibration_is_old(date(2026, 3, 1), race)
    assert calibration_is_old(date(2025, 8, 1), race)
    assert not calibration_is_old(date(2025, 8, 22), race)  # exactly two years is not older
    assert calibration_is_old(date(2024, 2, 29), date(2026, 3, 1))
    assert not calibration_is_old(date(2024, 2, 29), date(2026, 2, 28))


def test_provenance_notes_carry_sensor_and_calibration() -> None:
    notes = provenance_notes(_config(), date(2027, 8, 22))
    assert notes[0] == ML01_SENSOR_NOTE and "silicon" in ML01_SENSOR_NOTE
    assert "serial=ML01-TEST" in notes
    assert "calibration_date=2026-03-01" in notes
    assert "sensitivity_uv_per_wm2=7" in notes
    assert not any("older than" in n for n in notes)
    old = provenance_notes(_config(calibration_date=date(2025, 1, 1)), date(2027, 8, 22))
    assert old[-1] == "calibration older than 2 years on 2027-08-22"


# --------------------------------------------------------------------------
# level gate


@pytest.mark.science
def test_level_gate() -> None:
    # Tilt of a plane from pitch and roll: cos(tilt) = cos(pitch) * cos(roll).
    v = level_verdict(pitch_deg=1.0027, roll_deg=-2.0, tolerance_deg=3.0, assume_level=False)
    assert v.level and v.tilt_deg == pytest.approx(2.2372, abs=1e-3)
    v = level_verdict(pitch_deg=1.0027, roll_deg=-2.0, tolerance_deg=1.0, assume_level=False)
    assert not v.level and "> tolerance" in v.reason


def test_unknown_tilt_passes_only_when_assumed_level() -> None:
    v = level_verdict(pitch_deg=None, roll_deg=None, tolerance_deg=None, assume_level=False)
    assert not v.level and v.tilt_deg is None
    v = level_verdict(pitch_deg=None, roll_deg=1.0, tolerance_deg=None, assume_level=True)
    assert v.level and "assumed" in v.reason


def test_known_tilt_without_tolerance_raises() -> None:
    with pytest.raises(SensorConfigError, match="TOLERANCE"):
        level_verdict(pitch_deg=0.0, roll_deg=0.0, tolerance_deg=None, assume_level=True)


# --------------------------------------------------------------------------
# parsing and windows


def _parse_fixture() -> tuple[Ml01Parser, list[Ml01Reading]]:
    p = Ml01Parser()
    readings = [r for line in FIXTURE.read_text().splitlines() if (r := p.feed_line(line))]
    return p, readings


def test_parser_counts_bad_lines() -> None:
    p, readings = _parse_fixture()
    assert (p.stats.lines, p.stats.readings, p.stats.malformed, p.stats.header) == (7, 5, 1, 1)
    assert readings[2].pitch_deg == 0.5 and readings[3].pitch_deg is None
    assert all(r.time_utc.tzinfo is not None for r in readings)


@pytest.mark.parametrize(
    "line",
    [
        "2027-08-22T03:15:00,5.6",  # no offset
        "2027-08-22T03:15:00Z,",  # empty value
        "2027-08-22T03:15:00Z,inf",
        "2027-08-22T03:15:00Z,5.6,1.0",  # three columns
    ],
)
def test_parser_refuses(line: str) -> None:
    p = Ml01Parser()
    assert p.feed_line(line) is None
    assert p.stats.malformed == 1


def test_fixture_windows() -> None:
    _, readings = _parse_fixture()
    windower = Ml01Windower(_config(), late_tolerance_s=0.5)
    windows = [w for r in readings for w in windower.add(r)] + windower.flush()
    assert [w.start_utc for w in windows] == [T0 + timedelta(seconds=s) for s in range(3)]
    assert [w.values["irradiance_wm2"] for w in windows] == pytest.approx([800.0, 820.0, 780.0])
    assert windows[0].values["output_mv_max"] == pytest.approx(5.60)
    assert "pitch_deg" not in windows[0].values
    assert windows[1].values["pitch_deg"] == 0.5 and windows[1].values["roll_deg"] == 0.5
    assert [w.sample_count for w in windows] == [2, 2, 1]


def test_out_of_range_output_is_kept_not_clamped() -> None:
    w = build_ml01_window(_config(), T0, [Ml01Reading(T0, 120.0)])
    assert w.values["output_mv_max"] == 120.0
    assert w.values["irradiance_wm2"] == pytest.approx(120.0 * 1000 / 7.0)
