"""Attach ensemble spread to a deterministic series without touching its values."""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from datetime import datetime

from rwi.contracts import EnsembleSummary, WeatherSeries, WeatherStep
from rwi.core.geo import geodesic_distance_m
from rwi.core.provenance import DataKind, Measured
from rwi.core.timebase import ensure_utc
from rwi.ensemble._common import is_circular

__all__ = [
    "DEFAULT_MAX_DISTANCE_M",
    "DEFAULT_TOLERANCE_S",
    "attach_uncertainty",
    "spread_skill_note",
]

DEFAULT_TOLERANCE_S = 1800.0
"""Half an hour: half the hourly step the providers deliver. Matching is to the
*nearest* summary within this tolerance; a series step further away from every
summary keeps ``uncertainty=None``."""

DEFAULT_MAX_DISTANCE_M = 30_000.0
"""A summary computed for a place further than this from the series point is not
used (an ensemble grid cell is 25 km; spread at another location is not this
location's spread)."""


def attach_uncertainty(
    series: WeatherSeries,
    summaries: Sequence[EnsembleSummary],
    *,
    tolerance_s: float = DEFAULT_TOLERANCE_S,
    max_distance_m: float = DEFAULT_MAX_DISTANCE_M,
    overwrite: bool = False,
) -> WeatherSeries:
    """Return a **new** series whose values carry ensemble :class:`Uncertainty`.

    Nothing else changes. Every ``Measured`` keeps the very same ``value``,
    ``unit``, ``quality`` and ``provenance`` (the copy only sets the
    ``uncertainty`` field); steps, ordering and series metadata are untouched and
    the input series is not mutated.

    Matching is by variable and valid time: the summary for the same variable
    whose ``valid_time_utc`` is nearest to the step's time, provided it is within
    ``tolerance_s`` seconds (ties go to the earlier summary) and its ``point`` is
    within ``max_distance_m`` of the series point. A value with no such summary
    keeps ``uncertainty=None`` - an honest "unknown" rather than a default error
    bar. Values that are themselves missing (``value is None``) get no
    uncertainty. An uncertainty already present (for example from verification)
    is kept unless ``overwrite=True``.

    The uncertainty is ``summary.as_uncertainty()`` (p10-p90 interval with
    ``interval_probability`` 0.8 plus the ensemble standard deviation, method
    ``ensemble_spread``). It describes ensemble *spread*, not verified forecast
    error; pair it with :func:`spread_skill_note`. If the series is from a
    different model than the ensemble (e.g. a high-resolution deterministic run
    versus the 0.25 degree ensemble), the ``method`` string names the ensemble
    model so that this is visible.

    Only ``DataKind.FORECAST`` series are accepted: ensemble spread says nothing
    about an observation or a clear-sky bound. Raises ``ValueError`` otherwise.
    """
    if series.kind is not DataKind.FORECAST:
        raise ValueError(
            f"ensemble uncertainty can only be attached to FORECAST series, not {series.kind}"
        )
    if tolerance_s < 0:
        raise ValueError("tolerance_s must be >= 0")

    index: dict[str, tuple[list[datetime], list[EnsembleSummary]]] = {}
    by_var: dict[str, list[EnsembleSummary]] = {}
    for s in summaries:
        if geodesic_distance_m(s.point, series.point) <= max_distance_m:
            by_var.setdefault(s.variable, []).append(s)
    for var, items in by_var.items():
        items.sort(key=lambda s: ensure_utc(s.valid_time_utc))
        index[var] = ([ensure_utc(s.valid_time_utc) for s in items], items)

    new_steps: list[WeatherStep] = []
    for step in series.steps:
        when = ensure_utc(step.valid_time_utc)
        values: dict[str, Measured] = {}
        for name, measured in step.values.items():
            values[name] = _with_uncertainty(
                measured, index.get(name), when, tolerance_s, overwrite
            )
        new_steps.append(WeatherStep(valid_time_utc=step.valid_time_utc, values=values))
    return series.model_copy(update={"steps": new_steps})


def _with_uncertainty(
    measured: Measured,
    candidates: tuple[list[datetime], list[EnsembleSummary]] | None,
    when: datetime,
    tolerance_s: float,
    overwrite: bool,
) -> Measured:
    if candidates is None or measured.value is None:
        return measured
    if measured.uncertainty is not None and not overwrite:
        return measured
    times, items = candidates
    idx = bisect.bisect_left(times, when)
    options = [i for i in (idx - 1, idx) if 0 <= i < len(times)]
    best = min(options, key=lambda i: abs((times[i] - when).total_seconds()))
    if abs((times[best] - when).total_seconds()) > tolerance_s:
        return measured
    summary = items[best]
    if summary.unit != measured.unit:
        return measured
    return measured.model_copy(update={"uncertainty": summary.as_uncertainty()})


def spread_skill_note(summary: EnsembleSummary) -> str:
    """One plain sentence naming the spread and what it does and does not mean."""
    unit = summary.unit
    model = summary.model_name or summary.provider
    circular = is_circular(summary.variable)
    if summary.member_count == 1:
        return (
            f"Only one {model} member exists for {summary.variable} at this time, so there is "
            "no spread information: one member carries none, and a spread of 0 means "
            "'unknown', not 'certain'."
        )
    spread_name = "circular standard deviation" if circular else "standard deviation"
    spread = f"{spread_name} {summary.standard_deviation:.3g} {unit}"
    p10, p90 = summary.percentiles.get("10"), summary.percentiles.get("90")
    interval = f", 10th-90th percentile {p10:.4g} to {p90:.4g} {unit}" if (
        p10 is not None and p90 is not None
    ) else ""
    return (
        f"Spread of the {summary.member_count} {model} members for {summary.variable}: "
        f"{spread}{interval}; this measures disagreement between ensemble members and equals "
        "forecast uncertainty only if the ensemble is reliable, which has not been verified "
        "against observations here (ensembles are often under-dispersive, so real errors can "
        "exceed it)."
    )
