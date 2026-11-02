"""Member frequencies from an ensemble: "what fraction of members exceed X".

HONESTY. Everything returned here is a **member frequency**: the count of
ensemble members meeting a condition divided by the number of members that had
a value. It is *not* a calibrated probability:

* with N members the only possible values are k/N; for the 31-member GEFS or
  the 51-member IFS ensemble the resolution is 3.2 or 2.0 percentage points,
  and 0 / 1 mean "no / every member", not "impossible / certain";
* the members are a small sample, so the frequency itself carries sampling
  noise of order sqrt(p(1-p)/N) (about 9 percentage points at p=0.5, N=31);
* an ensemble is not automatically reliable: it is routinely under-dispersive,
  so "30% of members" may correspond to a very different real-world frequency.
  Establishing the mapping needs a verification history of past ensemble
  forecasts against observations (reliability diagram / calibration), which the
  archive does not have yet - see ``rwi.verification``.

Callers must display :func:`probability_note` next to any number from here, and
must not round it to a more confident-looking figure.

Boundary convention (fixed, documented, tested): ``probability_above`` counts
members **strictly greater** than the threshold, ``probability_below`` counts
members **strictly less**. A member exactly equal to the threshold is in neither,
so ``above + below + equal == 1``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from rwi.contracts import EnsembleForecast
from rwi.core.timebase import ensure_utc
from rwi.core.variables import RADIATION_VARIABLES, spec_for
from rwi.ensemble._common import (
    EnsembleDataError,
    is_circular,
    member_values_at,
    values_by_time,
)

__all__ = [
    "DEFAULT_TOLERANCE_S",
    "MemberFrequency",
    "irradiance_stays_above",
    "member_frequency_throughout",
    "probability_above",
    "probability_below",
    "probability_in_sector",
    "probability_note",
]

DEFAULT_TOLERANCE_S = 1800.0
"""Default time tolerance for matching a requested instant to a member step;
the same default as ``EnsembleForecast.member_values``."""


def _require_linear(variable: str) -> None:
    if is_circular(variable):
        raise ValueError(
            f"{variable} is a circular variable: 'above/below a threshold' is not defined "
            "on a circle; use probability_in_sector instead"
        )


def _values(
    forecast: EnsembleForecast, variable: str, when: datetime, tolerance_s: float
) -> list[float]:
    values = member_values_at(forecast, variable, when, tolerance_s=tolerance_s)
    if not values:
        raise EnsembleDataError(
            f"no ensemble member has a usable {variable} value within {tolerance_s:g} s of "
            f"{ensure_utc(when).isoformat()}; refusing to answer 0.0 for 'no data'"
        )
    return values


def probability_above(
    forecast: EnsembleForecast,
    *,
    variable: str,
    threshold: float,
    valid_time_utc: datetime,
    tolerance_s: float = DEFAULT_TOLERANCE_S,
) -> float:
    """Fraction of members **strictly above** ``threshold`` at ``valid_time_utc``.

    This is a *member frequency*, not a calibrated probability (see the module
    docstring): resolution is 1/N (about 3 percentage points for 30 members),
    sampling noise is of the same order or larger, and the ensemble has not been
    verified against outcomes. Display :func:`probability_note` beside it.

    Only members with a usable value within ``tolerance_s`` of ``valid_time_utc``
    are counted (denominator = those members). A member equal to ``threshold``
    is not above it. The result is the exact ratio ``k / n`` - never rounded.

    Raises :class:`EnsembleDataError` when no member has a value (not 0.0), and
    ``ValueError`` for a circular variable or non-finite threshold.
    """
    _require_linear(variable)
    _finite(threshold)
    values = _values(forecast, variable, valid_time_utc, tolerance_s)
    return sum(1 for v in values if v > threshold) / len(values)


def probability_below(
    forecast: EnsembleForecast,
    *,
    variable: str,
    threshold: float,
    valid_time_utc: datetime,
    tolerance_s: float = DEFAULT_TOLERANCE_S,
) -> float:
    """Fraction of members **strictly below** ``threshold``; same caveats as
    :func:`probability_above` (member frequency, not calibrated probability)."""
    _require_linear(variable)
    _finite(threshold)
    values = _values(forecast, variable, valid_time_utc, tolerance_s)
    return sum(1 for v in values if v < threshold) / len(values)


def probability_in_sector(
    forecast: EnsembleForecast,
    *,
    variable: str,
    centre_deg: float,
    half_width_deg: float,
    valid_time_utc: datetime,
    tolerance_s: float = DEFAULT_TOLERANCE_S,
) -> float:
    """Fraction of members whose direction is within ``half_width_deg`` (inclusive)
    of ``centre_deg``, using circular distance. Member frequency, not calibrated.

    Only meaningful for circular variables such as ``wind_direction_10m``.
    """
    if not is_circular(variable):
        raise ValueError(f"{variable} is not a circular variable; use probability_above/below")
    _finite(centre_deg)
    _finite(half_width_deg)
    if not 0.0 <= half_width_deg <= 180.0:
        raise ValueError("half_width_deg must be within [0, 180]")
    values = _values(forecast, variable, valid_time_utc, tolerance_s)
    inside = 0
    for v in values:
        d = abs(v - centre_deg) % 360.0
        if min(d, 360.0 - d) <= half_width_deg:
            inside += 1
    return inside / len(values)


@dataclass(frozen=True)
class MemberFrequency:
    """A member frequency together with the denominator that produced it."""

    frequency: float
    """Exact ``members_meeting / members_counted``; not rounded, not calibrated."""
    members_meeting: int
    members_counted: int
    """Members that had a usable value at every valid time in the window."""
    members_total: int
    """Members in the ensemble."""
    valid_times: int
    """Number of distinct valid times that had to be satisfied."""

    @property
    def note(self) -> str:
        excluded = self.members_total - self.members_counted
        extra = (
            f" {excluded} of {self.members_total} members were excluded because they lacked "
            "a value at some time in the window."
            if excluded
            else ""
        )
        return (
            f"Member frequency, not a calibrated probability: {self.members_meeting} of "
            f"{self.members_counted} members met the condition at all {self.valid_times} "
            f"valid times (resolution about {100.0 / self.members_counted:.1f} percentage "
            f"points; ensemble not verified against observations).{extra}"
        )


def member_frequency_throughout(
    forecast: EnsembleForecast,
    *,
    variable: str,
    threshold: float,
    start_utc: datetime,
    end_utc: datetime,
    above: bool = True,
) -> MemberFrequency:
    """Fraction of members that stay above (or below) ``threshold`` at **every**
    valid time in the closed window ``[start_utc, end_utc]``.

    The valid times are those present in at least one member inside the window.
    A member is counted only if it has a usable value at *all* of them (a member
    with a gap cannot be said to have stayed above anything); the result reports
    how many were counted and how many were excluded. Same strict-inequality
    convention and the same honesty caveat as :func:`probability_above`: this is
    the joint member frequency over the window, not a calibrated probability.

    Raises :class:`EnsembleDataError` if the window holds no valid time or no
    member covers it completely.
    """
    _require_linear(variable)
    _finite(threshold)
    start, end = ensure_utc(start_utc), ensure_utc(end_utc)
    if end < start:
        raise ValueError("end_utc must not precede start_utc")
    times = sorted(t for t in values_by_time(forecast, variable) if start <= t <= end)
    if not times:
        raise EnsembleDataError(f"no {variable} valid times in [{start.isoformat()}, "
                                f"{end.isoformat()}]")
    meeting = 0
    counted = 0
    for member in forecast.members:
        by_time = {
            ensure_utc(s.valid_time_utc): s.value(variable) for s in member.steps
        }
        series: list[float] = []
        for t in times:
            v = by_time.get(t)
            if v is None or not math.isfinite(v):
                break
            series.append(v)
        else:
            counted += 1
            if all((x > threshold) if above else (x < threshold) for x in series):
                meeting += 1
    if counted == 0:
        raise EnsembleDataError(
            f"no member has a usable {variable} value at all {len(times)} valid times in the window"
        )
    return MemberFrequency(
        frequency=meeting / counted,
        members_meeting=meeting,
        members_counted=counted,
        members_total=len(forecast.members),
        valid_times=len(times),
    )


def irradiance_stays_above(
    forecast: EnsembleForecast,
    *,
    threshold_w_m2: float,
    start_utc: datetime,
    end_utc: datetime,
    variable: str = "ghi",
) -> MemberFrequency:
    """Member frequency that irradiance stays above ``threshold_w_m2`` throughout
    the window, e.g. "GHI stays above 500 W/m2 over the next 3 hours".

    ``variable`` must be a radiation variable in W/m2 according to the variable
    registry (``ghi``, ``dni``, ``dhi``, ``terrestrial_radiation``); ``uv_index``
    and everything else is rejected. Member frequency, not calibrated.
    """
    spec = spec_for(variable)
    if variable not in RADIATION_VARIABLES or spec.unit != "W/m2":
        raise ValueError(f"{variable} is not an irradiance variable in W/m2")
    return member_frequency_throughout(
        forecast,
        variable=variable,
        threshold=threshold_w_m2,
        start_utc=start_utc,
        end_utc=end_utc,
        above=True,
    )


def probability_note(forecast: EnsembleForecast, variable: str) -> str:
    """The sentence the UI must display beside any number from this module.

    States that the number is a member frequency, how many members stand behind
    it, the resulting resolution, and that the ensemble is unverified.
    """
    spec_for(variable)
    members = sum(
        1
        for m in forecast.members
        if any(s.value(variable) is not None for s in m.steps)
    )
    model = forecast.model_name or forecast.provider
    if members == 0:
        return (
            f"No {model} ensemble member has a {variable} value, so no member frequency "
            "can be given."
        )
    resolution = 100.0 / members
    return (
        f"Member frequency, not a calibrated probability: the share of the {members} "
        f"{model} ensemble members that meet the condition (resolution about "
        f"{resolution:.1f} percentage points); the ensemble has not been verified against "
        "observations, so the real-world chance may differ."
    )


def _finite(x: float) -> None:
    if not math.isfinite(x):
        raise ValueError(f"value must be finite, got {x!r}")
