"""Retrospective validation of this platform's models against real race data.

This package exists so that scripts in ``scripts/validation/`` can import
reusable building blocks instead of duplicating logic between runs. The ONLY
abstraction here is what is genuinely shared across validations:

  * reading telemetry CSVs that match the team's Mongo-export format
  * pulling reanalysis for a given point and hour from Open-Meteo's archive
  * stitching MPPT groups into a per-hour measured power

Everything else (which hours to look at, which array config, how to report)
belongs in the individual script because the choices are the point of the
validation.
"""

from rwi.validation.pv import (
    HourlyComparison,
    RetrospectiveReport,
    build_report,
    read_hourly_headings,
    read_hourly_measured_power,
    read_hourly_positions,
)

__all__ = [
    "HourlyComparison",
    "RetrospectiveReport",
    "build_report",
    "read_hourly_headings",
    "read_hourly_measured_power",
    "read_hourly_positions",
]
