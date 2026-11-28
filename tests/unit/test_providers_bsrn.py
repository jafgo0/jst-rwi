"""BSRN station-to-archive reader, QC and hourly series on synthetic files (ADR 0017)."""

import gzip
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from rwi.core.provenance import DataKind, QualityFlag
from rwi.providers.bsrn import (
    hourly_series,
    physically_possible_limits,
    quality_control,
    read_bsrn_files,
)

# Alice Springs as published in its LR0004 (approximately).
ASP = (-23.798, 133.888, 547)
MTIME = datetime(2026, 11, 1, tzinfo=UTC)
# LR0100 layout: two lines per minute, fixed-width fields (pvlib BSRN_LR0100_COL_SPECS).
COLSPECS = [
    (0, 3),
    (4, 9),
    (10, 16),
    (16, 22),
    (22, 27),
    (27, 32),
    (32, 39),
    (39, 45),
    (45, 50),
    (50, 55),
    (55, 64),
    (64, 70),
    (70, 75),
]

Minute = tuple[float, float, float]  # ghi, dni, dhi


def _line(fields: list[float | None]) -> str:
    out = [" "] * COLSPECS[-1][1]
    for (_a, b), v in zip(COLSPECS, fields, strict=False):
        if v is None:
            continue
        text = f"{v:.0f}" if float(v).is_integer() else f"{v:.1f}"
        out[b - len(text) : b] = text
    return "".join(out).rstrip()


def _write_bsrn(
    path: Path,
    minutes: dict[tuple[int, int], Minute],
    *,
    year: int = 2024,
    month: int = 8,
    point: tuple[float, float, int] = ASP,
    with_lr0100: bool = True,
) -> Path:
    """A minimal BSRN station-to-archive file that pvlib's reader accepts."""
    lat, lon, alt = point
    lines = [
        "*U0001",
        f"{1:3d}{month:3d} {year:4d} {1:3d}",
        "*U0004",
        "01/01/24",
        " 11 1",
        "Alice Springs, NT, Australia",
        f"{'+61 8 0000 0000':<20}+61 8 0000 0001",
        f"{'0.0.0.0':<15}station@example.org",
        f"{lat + 90:7.3f} {lon + 180:7.3f} {alt:4d} 94326",
        "01/01/24",
        "  0  0 -1 -1",
    ]
    if with_lr0100:
        lines.append("*U0100")
        for (day, minute), (ghi, dni, dhi) in sorted(minutes.items()):
            lines.append(_line([day, minute, ghi, 1, ghi, ghi, dni, 1, dni, dni]))
            lines.append(_line([None, None, dhi, 1, dhi, dhi, 380, 1, 380, 380, 20.0, 30.0, 950]))
    path.write_bytes(gzip.compress(("\n".join(lines) + "\n").encode()))
    os.utime(path, (MTIME.timestamp(), MTIME.timestamp()))
    return path


def _hour(day: int, hour_utc: int, value: Callable[[int], Minute]) -> dict[tuple[int, int], Minute]:
    start = hour_utc * 60
    return {(day, m): value(m - start) for m in range(start, start + 60)}


def test_constant_midday_hour_is_its_mean_labelled_at_the_end(tmp_path: Path) -> None:
    f = _write_bsrn(tmp_path / "asp0824.dat.gz", _hour(22, 3, lambda _: (900, 950, 100)))
    record = read_bsrn_files("ASP", [f])
    series = hourly_series(record)
    (step,) = series.steps
    assert step.valid_time_utc == datetime(2024, 8, 22, 4, tzinfo=UTC)
    assert step.value("ghi") == 900.0 and step.value("dni") == 950.0
    assert step.value("dhi") == 100.0
    ghi = step.values["ghi"]
    assert ghi.provenance.kind is DataKind.OBSERVATION
    assert ghi.provenance.observation_time_utc == step.valid_time_utc
    assert ghi.provenance.retrieved_at_utc == MTIME
    assert ghi.provenance.source_url == "ftp://ftp.bsrn.awi.de/asp/"
    assert "60/60" in (ghi.provenance.processing or "")
    assert series.provider == "bsrn" and series.kind is DataKind.OBSERVATION


def test_station_point_comes_from_the_file(tmp_path: Path) -> None:
    f = _write_bsrn(tmp_path / "asp0824.dat.gz", _hour(22, 3, lambda _: (900, 950, 100)))
    record = read_bsrn_files("asp", [f])
    assert record.point.latitude_deg == pytest.approx(ASP[0])
    assert record.point.longitude_deg == pytest.approx(ASP[1])
    assert record.point.elevation_m == ASP[2]
    assert record.station_number == 1 and record.files == ("asp0824.dat.gz",)


def test_minute_above_the_physical_limit_is_rejected(tmp_path: Path) -> None:
    minutes = _hour(22, 3, lambda m: (2000 if m == 10 else 900, 950, 100))
    record = read_bsrn_files("asp", [_write_bsrn(tmp_path / "a.dat.gz", minutes)])
    qc = quality_control(record)
    assert int(qc["ghi"].isna().sum()) == 1
    strict = hourly_series(record).steps[0].values["ghi"]
    assert strict.value is None and strict.quality is QualityFlag.MISSING
    lenient = hourly_series(record, min_minutes=59).steps[0]
    assert lenient.value("ghi") == pytest.approx(900.0)
    assert lenient.value("dni") == 950.0  # other components are untouched


def test_night_minutes_within_limits_become_exactly_zero(tmp_path: Path) -> None:
    # 12-13 UTC is 21:30-22:30 ACST at Alice Springs.
    f = _write_bsrn(tmp_path / "a.dat.gz", _hour(22, 12, lambda _: (-2, 0, -2)))
    record = read_bsrn_files("asp", [f])
    qc = quality_control(record)
    assert (qc["apparent_elevation_deg"] < 0).all()
    assert (qc[["ghi", "dni", "dhi"]] == 0.0).all().all()
    (step,) = hourly_series(record).steps
    assert step.value("ghi") == 0.0 and step.value("dhi") == 0.0


def test_night_minute_below_the_lower_limit_is_rejected(tmp_path: Path) -> None:
    f = _write_bsrn(tmp_path / "a.dat.gz", _hour(22, 12, lambda m: (-10 if m == 0 else -1, 0, 0)))
    (step,) = hourly_series(read_bsrn_files("asp", [f])).steps
    assert step.values["ghi"].quality is QualityFlag.MISSING
    assert step.value("dhi") == 0.0


def test_files_are_joined_sorted_and_gaps_are_missing(tmp_path: Path) -> None:
    a = _write_bsrn(tmp_path / "a.dat.gz", _hour(22, 5, lambda _: (700, 800, 120)))
    b = _write_bsrn(tmp_path / "b.dat.gz", _hour(22, 3, lambda _: (900, 950, 100)))
    steps = hourly_series(read_bsrn_files("asp", [a, b])).steps
    assert [s.valid_time_utc.hour for s in steps] == [4, 5, 6]
    assert steps[0].value("ghi") == 900.0 and steps[2].value("ghi") == 700.0
    assert steps[1].values["ghi"].quality is QualityFlag.MISSING


def test_file_without_minute_record_is_refused(tmp_path: Path) -> None:
    f = _write_bsrn(tmp_path / "a.dat.gz", {}, with_lr0100=False)
    with pytest.raises(ValueError, match="LR0100"):
        read_bsrn_files("asp", [f])


def test_files_from_different_places_are_refused(tmp_path: Path) -> None:
    a = _write_bsrn(tmp_path / "a.dat.gz", _hour(22, 3, lambda _: (900, 950, 100)))
    b = _write_bsrn(
        tmp_path / "b.dat.gz", _hour(22, 5, lambda _: (900, 950, 100)), point=(-30.0, 133.888, 547)
    )
    with pytest.raises(ValueError, match="differs"):
        read_bsrn_files("asp", [a, b])


def test_rejects_bad_min_minutes(tmp_path: Path) -> None:
    f = _write_bsrn(tmp_path / "a.dat.gz", _hour(22, 3, lambda _: (900, 950, 100)))
    with pytest.raises(ValueError, match="min_minutes"):
        hourly_series(read_bsrn_files("asp", [f]), min_minutes=0)


@pytest.mark.science
def test_physically_possible_limits_match_long_and_shi() -> None:
    # Long & Shi (2008), Open Atmos. Sci. J. 2, 23-37, Table 1, evaluated at S0 = 1361 W/m2
    # and the sun overhead: GHI 1.5*S0 + 100, DHI 0.95*S0 + 50, DNI S0. Below the horizon the
    # cosine term vanishes.
    lim = physically_possible_limits(np.array([1361.0, 1361.0]), np.array([1.0, -0.2]))
    assert lim["ghi"] == pytest.approx([2141.5, 100.0])
    assert lim["dhi"] == pytest.approx([1342.95, 50.0])
    assert lim["dni"] == pytest.approx([1361.0, 1361.0])
