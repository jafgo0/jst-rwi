"""canboat ``analyzer -json -si`` decoding for the Airmar 200WX PGNs (ADR 0009)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rwi.telemetry.nmea2000 import (
    AttitudeRecord,
    CanboatDecoder,
    CanboatError,
    CogSogRecord,
    GnssRecord,
    HeadingRecord,
    HumidityRecord,
    PositionRecord,
    PressureRecord,
    TemperatureRecord,
    WindRecord,
    parse_canboat_timestamp,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "onboard" / "airmar_canboat.jsonl"
BANNER = '{"version":"6.1.0","units":"si"}'


def _decoder(**kw: bool) -> CanboatDecoder:
    d = CanboatDecoder(**kw)
    assert d.feed_line(BANNER) == []
    return d


def _line(pgn: int, fields: dict[str, object], ts: str = "2027-08-22T03:15:00.100Z") -> str:
    return json.dumps({"timestamp": ts, "src": 3, "pgn": pgn, "fields": fields})


# --------------------------------------------------------------------------
# banner


def test_stream_must_start_with_json_banner() -> None:
    with pytest.raises(CanboatError, match="JSON banner"):
        CanboatDecoder().feed_line("2027-08-22-03:15:00.000,2,130306,3,255,8,00,...")


def test_banner_without_si_units_is_refused() -> None:
    with pytest.raises(CanboatError, match="-si"):
        CanboatDecoder().feed_line('{"version":"6.1.0","units":"canboat"}')


def test_banner_records_version_and_blank_lines_are_ignored() -> None:
    d = CanboatDecoder()
    assert d.feed_line("   ") == []
    d.feed_line(BANNER)
    assert d.banner_seen and d.banner_version == "6.1.0"
    assert d.stats.lines == 1


# --------------------------------------------------------------------------
# the fixture stream


def test_fixture_decodes_every_supported_pgn() -> None:
    d = CanboatDecoder()
    records = [r for line in FIXTURE.read_text().splitlines() for r in d.feed_line(line)]
    assert d.stats.malformed == 0
    assert d.stats.unused_pgn == 1  # 126992 System Time
    assert d.stats.no_timestamp == 0
    kinds = {type(r) for r in records}
    assert kinds == {
        WindRecord,
        CogSogRecord,
        HeadingRecord,
        AttitudeRecord,
        TemperatureRecord,
        HumidityRecord,
        PressureRecord,
        PositionRecord,
        GnssRecord,
    }
    assert all(r.time_utc.tzinfo is UTC for r in records)


def test_si_units_are_converted() -> None:
    d = _decoder()
    [wind] = d.feed_line(
        _line(130306, {"Wind Speed": 4.2, "Wind Angle": 1.5707963, "Reference": "Apparent"})
    )
    assert isinstance(wind, WindRecord)
    assert wind.speed_mps == 4.2
    assert wind.angle_deg == pytest.approx(90.0)
    assert wind.reference == "Apparent"

    temp, hum, pres = d.feed_line(
        _line(
            130311,
            {
                "Temperature": 303.15,
                "Temperature Source": "Outside Temperature",
                "Humidity": 0.4,
                "Humidity Source": "Outside",
                "Atmospheric Pressure": 100800,
            },
        )
    )
    assert isinstance(temp, TemperatureRecord) and temp.temperature_c == pytest.approx(30.0)
    assert isinstance(hum, HumidityRecord) and hum.relative_humidity_percent == pytest.approx(40)
    assert isinstance(pres, PressureRecord) and pres.pressure_pa == 100800.0


def test_angles_wrap_to_360_but_attitude_stays_signed() -> None:
    d = _decoder()
    [heading] = d.feed_line(
        _line(127250, {"Heading": -0.1, "Deviation": -0.01, "Variation": 0.05, "Reference": "True"})
    )
    assert isinstance(heading, HeadingRecord)
    assert 0.0 <= heading.heading_deg < 360.0  # type: ignore[operator]
    assert heading.heading_deg == pytest.approx(360.0 - 5.7296, abs=1e-3)
    assert heading.deviation_deg == pytest.approx(-0.5730, abs=1e-3)
    [att] = d.feed_line(_line(127257, {"Yaw": 3.0, "Pitch": -0.0175, "Roll": 0.0349}))
    assert isinstance(att, AttitudeRecord)
    assert att.pitch_deg == pytest.approx(-1.0027, abs=1e-3)
    assert att.roll_deg == pytest.approx(1.9996, abs=1e-3)


def test_gnss_position_data_gives_position_and_fix_details() -> None:
    d = _decoder()
    pos, gnss = d.feed_line(
        _line(
            129029,
            {
                "Date": "2027.08.22",
                "Time": "03:15:00.25",
                "Latitude": -16.25,
                "Longitude": 133.37,
                "Altitude": 210.0,
                "Method": "GNSS fix",
                "Number of SVs": 9,
                "HDOP": 0.9,
            },
        )
    )
    assert isinstance(pos, PositionRecord)
    assert (pos.latitude_deg, pos.longitude_deg, pos.altitude_m) == (-16.25, 133.37, 210.0)
    assert isinstance(gnss, GnssRecord)
    assert gnss.gnss_time_utc == datetime(2027, 8, 22, 3, 15, 0, 250000, tzinfo=UTC)
    assert (gnss.method, gnss.satellites, gnss.hdop) == ("GNSS fix", 9, 0.9)


def test_lookup_values_may_arrive_wrapped_in_a_dict() -> None:
    d = _decoder()
    [cs] = d.feed_line(_line(129026, {"COG": 0.0, "SOG": 3.0, "COG Reference": {"name": "True"}}))
    assert isinstance(cs, CogSogRecord) and cs.reference == "True"


# --------------------------------------------------------------------------
# failure modes are counted, not fatal


def test_bad_lines_after_banner_are_counted() -> None:
    d = _decoder()
    assert d.feed_line("not json") == []
    assert d.feed_line("[1, 2]") == []
    assert d.feed_line(json.dumps({"pgn": "130306"})) == []
    assert d.feed_line(_line(130306, {}, ts="yesterday")) == []
    assert d.feed_line(json.dumps({"pgn": 130306, "fields": {}})) == []
    assert d.feed_line(json.dumps({"pgn": 130306, "timestamp": "2027-08-22T03:15:00Z"})) == []
    assert d.stats.malformed == 5
    assert d.stats.no_timestamp == 1


def test_missing_field_is_counted_and_left_none() -> None:
    d = _decoder()
    [wind] = d.feed_line(_line(130306, {"Wind Speed": 4.0, "Reference": "Apparent"}))
    assert isinstance(wind, WindRecord) and wind.angle_deg is None
    assert d.stats.missing_fields is not None
    assert d.stats.missing_fields["130306:Wind Angle"] == 1


def test_non_finite_numbers_become_none() -> None:
    d = _decoder()
    [wind] = d.feed_line(
        '{"timestamp":"2027-08-22T03:15:00Z","pgn":130306,'
        '"fields":{"Wind Speed":NaN,"Wind Angle":0.0,"Reference":"Apparent"}}'
    )
    assert isinstance(wind, WindRecord) and wind.speed_mps is None


# --------------------------------------------------------------------------
# timestamps


def test_naive_timestamp_is_refused_unless_declared_utc() -> None:
    with pytest.raises(CanboatError, match="NAIVE_UTC"):
        _decoder().feed_line(_line(130306, {}, ts="2027-08-22T03:15:00.100"))
    [wind] = _decoder(naive_is_utc=True).feed_line(
        _line(
            130306,
            {"Wind Speed": 1.0, "Wind Angle": 0.0, "Reference": "Apparent"},
            ts="2027-08-22T03:15:00.100",
        )
    )
    assert wind.time_utc == datetime(2027, 8, 22, 3, 15, 0, 100000, tzinfo=UTC)


def test_offset_timestamp_is_converted_to_utc() -> None:
    when = parse_canboat_timestamp("2027-08-22T12:45:00+09:30", naive_is_utc=False)
    assert when == datetime(2027, 8, 22, 3, 15, tzinfo=UTC)


def test_legacy_actisense_timestamp_form() -> None:
    when = parse_canboat_timestamp("2024-08-22-03:15:00.123", naive_is_utc=True)
    assert when == datetime(2024, 8, 22, 3, 15, 0, 123000, tzinfo=UTC)
