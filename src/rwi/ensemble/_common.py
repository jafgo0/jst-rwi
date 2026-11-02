"""Shared helpers for :mod:`rwi.ensemble` (private).

Two things live here because several modules need them and they must agree:

* which variables are *circular* (angles on a circle, where 359 deg and 1 deg
  are 2 deg apart, not 358), and the circular statistics for them;
* how member values at a valid time are gathered (``member_values_at``), so
  that ``summarise`` and ``probability_*`` count members the same way.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime

import numpy as np

from rwi.contracts import EnsembleForecast
from rwi.core.errors import RwiError
from rwi.core.timebase import ensure_utc
from rwi.core.variables import spec_for

__all__ = [
    "CIRCULAR_VARIABLES",
    "CircularStats",
    "EnsembleDataError",
    "circular_stats",
    "is_circular",
    "member_values_at",
    "values_by_time",
]

class EnsembleDataError(RwiError, ValueError):
    """The ensemble has no usable members for the request.

    Raised instead of returning 0.0: "no data" and "zero of N members" are
    different statements and the first must never look like the second.
    """


CIRCULAR_VARIABLES: frozenset[str] = frozenset({"wind_direction_10m"})
"""Canonical variables whose values are angles in degrees on a circle.

The variable registry has no ``circular`` flag (``rwi.core.variables`` is not
owned by this module), so the set is declared here. A new angular variable must
be added here or it will silently get linear statistics.
"""


def is_circular(variable: str) -> bool:
    """True when ``variable`` is an angle on a circle (validates the name)."""
    spec_for(variable)  # raises UnknownVariableError for a non-canonical name
    return variable in CIRCULAR_VARIABLES


def values_by_time(forecast: EnsembleForecast, variable: str) -> dict[datetime, list[float]]:
    """Finite, usable member values grouped by *exact* valid time (UTC).

    A member that has no usable value at a time simply does not appear in that
    time's list; nothing is substituted for it.
    """
    out: dict[datetime, list[float]] = {}
    for member in forecast.members:
        for step in member.steps:
            v = step.value(variable)
            if v is not None and math.isfinite(v):
                out.setdefault(ensure_utc(step.valid_time_utc), []).append(float(v))
    return out


def member_values_at(
    forecast: EnsembleForecast, variable: str, when: datetime, *, tolerance_s: float
) -> list[float]:
    """Finite usable member values at ``when`` (nearest step within ``tolerance_s``)."""
    spec_for(variable)
    return [
        v
        for v in forecast.member_values(variable, ensure_utc(when), tolerance_s=tolerance_s)
        if math.isfinite(v)
    ]


class CircularStats:
    """Vector statistics for a sample of directions in degrees."""

    __slots__ = ("arc_end_deg", "arc_start_deg", "mean_deg", "median_deg",
                 "resultant_length", "std_deg")

    def __init__(
        self,
        mean_deg: float,
        std_deg: float,
        resultant_length: float,
        median_deg: float,
        arc_start_deg: float,
        arc_end_deg: float,
    ) -> None:
        self.mean_deg = mean_deg
        self.std_deg = std_deg
        self.resultant_length = resultant_length
        self.median_deg = median_deg
        self.arc_start_deg = arc_start_deg
        self.arc_end_deg = arc_end_deg


_MIN_RESULTANT = 1e-6
"""Floor on R so that the circular standard deviation stays finite."""


def _wrap(deg: float) -> float:
    """Wrap into [0, 360), snapping values within 1e-9 deg of 360 to 0."""
    wrapped = deg % 360.0
    return 0.0 if abs(wrapped - 360.0) < 1e-9 else wrapped


def _angular_distance(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def circular_stats(values_deg: Sequence[float]) -> CircularStats:
    """Circular mean, circular standard deviation, median and containing arc.

    * mean: ``atan2(mean sin, mean cos)`` mapped to [0, 360);
    * resultant length ``R = |mean unit vector|`` in [0, 1] (1 = all identical);
    * standard deviation (Mardia & Jupp): ``sqrt(-2 ln R)`` radians, in degrees.
      It is a population-style quantity (no small-sample correction) and grows
      without bound as the members fan out around the circle (R -> 0); ``R`` is
      floored at 1e-6 to keep it finite;
    * median: the member direction minimising the sum of angular distances to
      all members (ties go to the one closest to the circular mean);
    * arc: the endpoints (clockwise start, clockwise end) of the smallest arc
      containing every member. ``arc_end_deg < arc_start_deg`` means the arc
      crosses north.
    """
    if not values_deg:
        raise ValueError("circular_stats needs at least one value")
    theta = np.deg2rad(np.asarray(values_deg, dtype=float))
    s = float(np.mean(np.sin(theta)))
    c = float(np.mean(np.cos(theta)))
    r = min(1.0, math.hypot(s, c))
    mean = _wrap(math.degrees(math.atan2(s, c)))
    std = math.degrees(math.sqrt(max(0.0, -2.0 * math.log(max(r, _MIN_RESULTANT)))))
    if len(values_deg) == 1:
        std = 0.0

    wrapped = sorted(_wrap(float(v)) for v in values_deg)
    candidates = sorted(set(wrapped))
    median = min(
        candidates,
        key=lambda cand: (
            sum(_angular_distance(cand, w) for w in wrapped),
            _angular_distance(cand, mean),
        ),
    )

    # Smallest containing arc = circle minus the largest gap between neighbours.
    n = len(wrapped)
    best_gap = -1.0
    start = wrapped[0]
    end = wrapped[-1]
    for i in range(n):
        nxt = wrapped[(i + 1) % n]
        gap = (nxt - wrapped[i]) % 360.0 if n > 1 else 360.0
        if gap > best_gap:
            best_gap = gap
            start = nxt
            end = wrapped[i]
    if len(set(wrapped)) == 1:
        start = end = wrapped[0]
    return CircularStats(mean, std, r, median, start, end)
