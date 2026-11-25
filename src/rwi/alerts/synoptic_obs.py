"""Station observations as the reference for the front detector (#10, #14, step B).

Pure functions, no I/O. The calibration script
(``scripts/validation/synoptic_obs_calibration.py``) reads NOAA ISD
"global-hourly" CSV rows. It uses these functions to turn them into the hourly
``{time: {variable: value}}`` grid that ``detect_front_passages`` needs.

* :func:`parse_isd_row`: decodes one ISD row. It handles the scale factors
  (TMP, DEW and SLP are tenths; wind speed is tenths of m/s), the quality flags,
  the ``9999``-style missing codes, and calm or variable wind.
* :func:`hourly_grid`: for each top of the hour and each variable, takes the
  value from the report closest to that hour, within a tolerance. Synoptic
  (FM-12) reports win ties because only they carry sea-level pressure. Hours
  without a value stay missing. Nothing is interpolated.

Format reference: NOAA NCEI, *Federal Climate Complex Data Documentation for
Integrated Surface Data* (ISD format document), sections "WND", "TMP", "DEW",
"SLP" and the quality-code tables. Quality codes 2, 3, 6 and 7 (suspect or
erroneous) are rejected.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

__all__ = ["IsdReport", "hourly_grid", "parse_isd_row"]

_BAD_QC = frozenset("2367")
_SYNOP = "FM-12"


@dataclass(frozen=True)
class IsdReport:
    time_utc: datetime
    report_type: str
    values: Mapping[str, float]
    """Keys from ``SYNOPTIC_VARIABLES``: Pa, degC, m/s, deg. Missing variables are absent."""


def _field(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [p.strip() for p in raw.split(",")]


def _scaled(raw: str | None, missing: str, scale: float) -> float | None:
    """``"+0271,1"`` -> 27.1 for scale 10. None if missing or QC-flagged bad."""
    parts = _field(raw)
    if parts is None or len(parts) < 2:
        return None
    val, qc = parts[0], parts[1]
    if qc in _BAD_QC or val.lstrip("+-") == missing:
        return None
    try:
        return int(val) / scale
    except ValueError:
        return None


def _wind(raw: str | None) -> tuple[float | None, float | None]:
    """WND ``ddd,q,type,ssss,q`` -> (direction deg, speed m/s).

    Calm (type C) gives speed 0 and no direction. Variable (type V) or a 999
    direction gives the speed only.
    """
    parts = _field(raw)
    if parts is None or len(parts) < 5:
        return None, None
    d_raw, d_qc, typ, s_raw, s_qc = parts[:5]
    speed = None
    if s_qc not in _BAD_QC and s_raw != "9999":
        try:
            speed = int(s_raw) / 10.0
        except ValueError:
            speed = None
    if typ == "C":
        return None, 0.0 if speed is None else speed
    direction = None
    if typ != "V" and d_qc not in _BAD_QC and d_raw != "999":
        try:
            d = int(d_raw)
        except ValueError:
            d = -1
        if 1 <= d <= 360:
            direction = float(d)
    return direction, speed


def parse_isd_row(row: Mapping[str, str]) -> IsdReport | None:
    """One ISD global-hourly CSV row -> report, or None if it has no usable value."""
    try:
        t = datetime.fromisoformat(row["DATE"]).replace(tzinfo=UTC)
    except (KeyError, ValueError):
        return None
    vals: dict[str, float] = {}
    slp = _scaled(row.get("SLP"), "99999", 10.0)
    if slp is not None:
        vals["pressure_msl"] = slp * 100.0  # hPa -> Pa
    tmp = _scaled(row.get("TMP"), "9999", 10.0)
    if tmp is not None:
        vals["temperature_2m"] = tmp
    dew = _scaled(row.get("DEW"), "9999", 10.0)
    if dew is not None:
        vals["dew_point_2m"] = dew
    d, s = _wind(row.get("WND"))
    if d is not None:
        vals["wind_direction_10m"] = d
    if s is not None:
        vals["wind_speed_10m"] = s
    if not vals:
        return None
    return IsdReport(t, (row.get("REPORT_TYPE") or "").strip(), vals)


def hourly_grid(
    reports: Iterable[IsdReport], tolerance: timedelta = timedelta(minutes=10)
) -> dict[datetime, dict[str, float]]:
    """Per top-of-hour and variable, the closest report within ``tolerance``.

    Ties go to the synoptic (FM-12) report, then to the earlier report. Each
    variable is chosen independently, so pressure can come from the SYNOP while
    wind comes from a METAR at the same minute.
    """
    best: dict[tuple[datetime, str], tuple[tuple[float, int, datetime], float]] = {}
    for r in reports:
        hour = r.time_utc.replace(minute=0, second=0, microsecond=0)
        for anchor in (hour, hour + timedelta(hours=1)):
            off = abs(r.time_utc - anchor)
            if off > tolerance:
                continue
            key = (off.total_seconds(), 0 if r.report_type == _SYNOP else 1, r.time_utc)
            for var, val in r.values.items():
                cur = best.get((anchor, var))
                if cur is None or key < cur[0]:
                    best[(anchor, var)] = (key, val)
    out: dict[datetime, dict[str, float]] = {}
    for (anchor, var), (_, val) in best.items():
        out.setdefault(anchor, {})[var] = val
    return dict(sorted(out.items()))
