"""Per-valid-time summary statistics of an ensemble forecast.

The ensemble spread computed here is the platform's only uncertainty that comes
from model runs that were actually made, rather than from an error bar invented
afterwards. It is *spread*, not *error*: see :func:`rwi.ensemble.attach.spread_skill_note`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime

import numpy as np

from rwi.contracts import EnsembleForecast, EnsembleSummary
from rwi.core.provenance import DataKind, Provenance
from rwi.core.variables import spec_for
from rwi.ensemble._common import circular_stats, is_circular, values_by_time

__all__ = ["DEFAULT_PERCENTILES", "PERCENTILE_METHOD", "STD_DDOF", "summarise"]

DEFAULT_PERCENTILES: tuple[int, ...] = (10, 25, 50, 75, 90)

PERCENTILE_METHOD = "linear"
"""numpy ``method='linear'`` = Hyndman & Fan type 7: the p-th percentile sits at
fractional rank ``p/100 * (n - 1)`` of the sorted members, linearly interpolated
between the two neighbouring order statistics."""

STD_DDOF = 1
"""Sample standard deviation (divide by n-1) for linear variables."""

_SMALL_ENSEMBLE = 10
"""Below this many contributing members the tails are very coarse; we say so."""


def _percentile_key(p: float) -> str:
    return f"{p:g}"


def summarise(
    forecast: EnsembleForecast,
    *,
    variable: str,
    percentiles: Sequence[int] = DEFAULT_PERCENTILES,
) -> list[EnsembleSummary]:
    """One :class:`EnsembleSummary` per valid time at which any member has a value.

    **Which members count.** For each valid time only members that actually have
    a usable (non-missing, finite, quality-usable) value at *exactly* that time
    contribute. ``member_count`` is that number, which may be smaller than
    ``len(forecast.members)``; when it is, the provenance ``notes`` say so. A
    valid time at which no member has a value produces **no** summary (never a
    zero-member one). Results are sorted by valid time.

    **Linear variables** (everything except wind direction):

    * ``mean``, ``median``, ``minimum``, ``maximum`` as usual;
    * ``standard_deviation`` is the **sample** standard deviation (``ddof=1``,
      divide by n-1): the members are a small sample from the underlying forecast
      distribution and we want an estimate of *that* spread, not the spread of
      the 31 numbers themselves. With a single member the spread is unknowable
      and is reported as ``0.0`` with an explicit note that it carries no
      information (it must not be read as "certain");
    * ``percentiles`` use **linear interpolation** (numpy ``method='linear'``,
      Hyndman-Fan type 7): rank ``p/100 * (n-1)`` between sorted members. With
      about 30 members this choice matters in the tails: p10 of 31 members is
      exactly the 4th smallest member, but p5 or p95 interpolate between order
      statistics and can never lie outside [min, max]. Percentile ``50`` is set
      equal to ``median`` exactly. Percentiles are in [0, 100] and are keyed by
      ``f"{p:g}"`` (``"10"``, ``"12.5"``).

    **Circular variables** (``wind_direction_10m``, see
    :data:`rwi.ensemble._common.CIRCULAR_VARIABLES`) use vector statistics:

    * ``mean`` is ``atan2(mean sin, mean cos)`` in [0, 360), so
      [350, 355, 0, 5, 10] gives about 0 deg, not the arithmetic 144 deg;
    * ``standard_deviation`` is the circular standard deviation
      ``sqrt(-2 ln R)`` (radians converted to degrees; R = resultant length);
    * ``median`` is the circular median: the member direction minimising the sum
      of angular distances to all members;
    * ``minimum`` / ``maximum`` are the clockwise start and end of the smallest
      arc containing all members. They are **not** an ordering: ``maximum <
      minimum`` means the arc crosses north (350 -> 10);
    * ``percentiles`` is deliberately **empty**. Linear percentiles on a circle
      are wrong across the 0/360 wrap and no cut point is canonical, so none are
      invented; consequently :meth:`EnsembleSummary.as_uncertainty` carries only
      the circular standard deviation, no lower/upper interval. The provenance
      notes say all of this.

    Raises :class:`rwi.core.errors.UnknownVariableError` for a non-canonical
    variable and ``ValueError`` for a percentile outside [0, 100].
    """
    spec = spec_for(variable)
    circular = is_circular(variable)
    pcts = [float(p) for p in percentiles]
    for p in pcts:
        if not (math.isfinite(p) and 0.0 <= p <= 100.0):
            raise ValueError(f"percentile {p!r} outside [0, 100]")

    grouped = values_by_time(forecast, variable)
    total = len(forecast.members)
    model_init = _common_init_time(forecast)
    reference = next((m for m in forecast.members if m.steps), None)

    out: list[EnsembleSummary] = []
    for when in sorted(grouped):
        values = grouped[when]
        n = len(values)
        if n == 0:  # pragma: no cover - grouped never holds empty lists
            continue
        notes: list[str] = []
        arr = np.asarray(values, dtype=float)
        if circular:
            cs = circular_stats(values)
            mean, median = cs.mean_deg, cs.median_deg
            minimum, maximum = cs.arc_start_deg, cs.arc_end_deg
            std = cs.std_deg
            pct: dict[str, float] = {}
            stat = (
                "circular ensemble statistics: vector mean, circular standard deviation "
                "sqrt(-2 ln R), circular median, containing arc; no percentiles"
            )
            notes.append(
                "Circular variable: mean/spread use sin/cos vector statistics. "
                "minimum/maximum are the clockwise start/end of the smallest arc containing "
                "all members (maximum < minimum means the arc crosses north). No percentiles "
                "are given because linear percentiles are invalid across the 0/360 wrap."
            )
            if cs.resultant_length < 0.1 and n > 1:
                notes.append(
                    f"Members are dispersed all around the circle (R={cs.resultant_length:.3f}); "
                    "the mean direction carries almost no information."
                )
        else:
            mean = float(np.mean(arr))
            median = float(np.median(arr))
            minimum = float(np.min(arr))
            maximum = float(np.max(arr))
            std = float(np.std(arr, ddof=STD_DDOF)) if n > 1 else 0.0
            pct = {
                _percentile_key(p): float(np.percentile(arr, p, method="linear"))
                for p in pcts
            }
            if "50" in pct:
                pct["50"] = median
            stat = (
                f"ensemble statistics: mean, median, min, max, sample standard deviation "
                f"(ddof={STD_DDOF}), percentiles {','.join(sorted(pct, key=float))} "
                f"(numpy '{PERCENTILE_METHOD}' interpolation, Hyndman-Fan type 7)"
            )
        if n == 1:
            notes.append(
                "Only one member contributed: one member carries no spread information; "
                "standard_deviation=0.0 means 'unknown', not 'certain'."
            )
        elif n < _SMALL_ENSEMBLE:
            notes.append(
                f"Only {n} members contributed; percentiles and spread are very coarse."
            )
        if n < total:
            notes.append(
                f"member_count={n} of {total} ensemble members had a usable value at this "
                f"valid time; {total - n} were missing and are excluded, not filled."
            )

        point = forecast.point
        prov = Provenance(
            provider=forecast.provider,
            dataset="ensemble/summary",
            kind=DataKind.FORECAST,
            retrieved_at_utc=forecast.retrieved_at_utc,
            valid_time_utc=when,
            model_name=forecast.model_name,
            model_version=reference.model_version if reference else None,
            model_init_time_utc=model_init,
            point=point,
            spatial_resolution_km=reference.spatial_resolution_km if reference else None,
            temporal_resolution_s=reference.temporal_resolution_s if reference else None,
            processing=f"{stat}; member_count={n}",
            license=reference.license if reference else None,
            source_url=reference.source_url if reference else None,
            notes=" ".join(notes) if notes else None,
        )
        out.append(
            EnsembleSummary(
                variable=variable,
                valid_time_utc=when,
                point=point,
                unit=spec.unit,
                member_count=n,
                mean=mean,
                median=median,
                minimum=minimum,
                maximum=maximum,
                standard_deviation=std,
                percentiles=pct,
                provider=forecast.provider,
                model_name=forecast.model_name,
                provenance=prov,
            )
        )
    return out


def _common_init_time(forecast: EnsembleForecast) -> datetime | None:
    """The members' model init time if they all agree, else ``None`` (never a guess)."""
    inits = {m.model_init_time_utc for m in forecast.members}
    if len(inits) == 1:
        (only,) = inits
        return only
    return None
