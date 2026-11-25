"""Scoring helpers for calibrating the front detector (#10, #14, step B).

Pure functions, no I/O. The calibration script
(``scripts/validation/synoptic_calibration.py``) feeds them detections from
archived forecasts and from ERA5, which serves as the reference.

* :func:`match_events`: one-to-one matching of forecast and reference event
  times within a tolerance (nearest first), giving hits, misses and false alarms.
* :func:`event_scores`: POD, FAR and CSI from those counts. Undefined ratios
  are ``None``, never 0.
* :func:`local_solar_hour` and :func:`phase_locking`: Rayleigh test of
  detection times on the 24 h local-solar-time circle. Real fronts are not tied
  to local time. Strong clustering means the detector is tracking the diurnal
  cycle (tide, heat trough, sea breeze) instead.

Limits: ERA5 is a reanalysis, not an observation. Agreement with ERA5 shows the
forecast reproduces what the same rules find in the analysis. It does not
show that the rules find real fronts.

References: Wilks, *Statistical Methods in the Atmospheric Sciences* (3rd ed.),
section 8.2 (2x2 contingency scores); Zar, *Biostatistical Analysis* (5th ed.),
section 27.4 (Rayleigh test, p-value approximation).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

__all__ = [
    "EventMatch",
    "EventScores",
    "PhaseLocking",
    "event_scores",
    "local_solar_hour",
    "match_events",
    "phase_locking",
]


_R_EPS = 1e-9


@dataclass(frozen=True)
class EventMatch:
    hits: tuple[tuple[datetime, datetime], ...]
    """(forecast time, reference time) pairs."""
    misses: tuple[datetime, ...]
    """Reference events with no forecast event within tolerance."""
    false_alarms: tuple[datetime, ...]
    """Forecast events with no reference event within tolerance."""

    @property
    def timing_errors_h(self) -> tuple[float, ...]:
        """Forecast minus reference, hours (positive = forecast late)."""
        return tuple((f - r).total_seconds() / 3600.0 for f, r in self.hits)


@dataclass(frozen=True)
class EventScores:
    n_hits: int
    n_misses: int
    n_false_alarms: int
    pod: float | None
    far: float | None
    csi: float | None
    mean_timing_error_h: float | None
    mean_abs_timing_error_h: float | None


@dataclass(frozen=True)
class PhaseLocking:
    n: int
    mean_resultant_length: float | None
    """0 = uniform over the day, 1 = every event at the same local hour."""
    mean_local_hour: float | None
    p_value: float | None
    """Rayleigh test against uniformity; ``None`` below 2 events."""


def match_events(
    forecast: Sequence[datetime], reference: Sequence[datetime], tolerance: timedelta
) -> EventMatch:
    """Greedy one-to-one matching, closest pairs first; ties keep the earlier forecast."""
    cands = sorted(
        (abs(f - r), i, j)
        for i, f in enumerate(forecast)
        for j, r in enumerate(reference)
        if abs(f - r) <= tolerance
    )
    used_f: set[int] = set()
    used_r: set[int] = set()
    hits: list[tuple[datetime, datetime]] = []
    for _, i, j in cands:
        if i in used_f or j in used_r:
            continue
        used_f.add(i)
        used_r.add(j)
        hits.append((forecast[i], reference[j]))
    hits.sort(key=lambda p: p[1])
    return EventMatch(
        hits=tuple(hits),
        misses=tuple(r for j, r in enumerate(reference) if j not in used_r),
        false_alarms=tuple(f for i, f in enumerate(forecast) if i not in used_f),
    )


def _ratio(num: int, den: int) -> float | None:
    return None if den == 0 else num / den


def event_scores(matches: Sequence[EventMatch]) -> EventScores:
    """Pooled POD = H/(H+M), FAR = F/(H+F), CSI = H/(H+M+F) over several matchings."""
    h = sum(len(m.hits) for m in matches)
    mi = sum(len(m.misses) for m in matches)
    fa = sum(len(m.false_alarms) for m in matches)
    errs = [e for m in matches for e in m.timing_errors_h]
    return EventScores(
        n_hits=h,
        n_misses=mi,
        n_false_alarms=fa,
        pod=_ratio(h, h + mi),
        far=_ratio(fa, h + fa),
        csi=_ratio(h, h + mi + fa),
        mean_timing_error_h=sum(errs) / len(errs) if errs else None,
        mean_abs_timing_error_h=sum(abs(e) for e in errs) / len(errs) if errs else None,
    )


def local_solar_hour(t: datetime, longitude_deg: float) -> float:
    """Mean local solar time in hours [0, 24): UTC + longitude/15. Ignores the equation of time."""
    utc_h = t.hour + t.minute / 60.0 + t.second / 3600.0
    return (utc_h + longitude_deg / 15.0) % 24.0


def phase_locking(hours: Sequence[float], harmonic: int = 1) -> PhaseLocking:
    """Rayleigh test of ``hours`` on the 24 h circle (Zar 2010, eq. 27.4 approximation).

    ``harmonic=2`` doubles the angles (period 12 h). That detects bimodal locking
    to the semidiurnal pressure tide, which the first harmonic misses when the two
    modes are about 12 h apart. ``mean_local_hour`` is then one of the two modes, in [0, 12).
    """
    if harmonic < 1:
        raise ValueError("harmonic must be >= 1")
    n = len(hours)
    if n == 0:
        return PhaseLocking(0, None, None, None)
    period = 24.0 / harmonic
    ang = [h / period * 2.0 * math.pi for h in hours]
    c = sum(math.cos(a) for a in ang) / n
    s = sum(math.sin(a) for a in ang) / n
    r = math.hypot(c, s)
    # Below _R_EPS the direction is float noise (e.g. 4.6e-17 for 24 evenly spread hours).
    mean_h = (math.atan2(s, c) / (2.0 * math.pi) * period) % period if r > _R_EPS else None
    if n < 2:
        return PhaseLocking(n, r, mean_h, None)
    z = n * r * r
    p = math.exp(math.sqrt(1.0 + 4.0 * n + 4.0 * (n * n - z * n)) - (1.0 + 2.0 * n))
    return PhaseLocking(n, r, mean_h, min(1.0, max(0.0, p)))
