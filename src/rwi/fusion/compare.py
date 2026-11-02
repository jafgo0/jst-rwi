"""Side-by-side comparison of several models for one point and one variable.

Disagreement between numerical weather models is information: it is the
cheapest available signal that a forecast should not be acted on without a
margin. This module measures it. It does not decide which model is right
(that is :mod:`rwi.fusion.selection`, and only with verification evidence) and
it does not blend anything.

ALIGNMENT RULE (the one place models with different time steps meet)
--------------------------------------------------------------------
1. Only usable values are considered (`WeatherSeries.series`: a missing,
   stale or otherwise unusable value is not a value).
2. The candidate comparison times are the distinct valid times of all models.
   Walking them in order, the earliest unassigned time becomes a *reference
   time*; every model contributes its value closest to that reference time
   provided it lies within ``tolerance_s`` AFTER it (default
   :data:`DEFAULT_ALIGNMENT_TOLERANCE_S`). The comparison point is stamped
   with the reference time.
3. A model with no value inside that window is ABSENT from the point. It is
   never interpolated, resampled, forward-filled or substituted.
4. A disagreement is produced only where at least two models are present.
   One model alone has nothing to disagree with, and that is reported as
   "not comparable", never as a spread of zero (which would read as agreement).
5. Any non-zero time offset used, any window holding more than one value for
   a model, and every model that is absent at some points is recorded in
   ``ModelComparison.notes``.

So hourly and 3-hourly models are compared at the 3-hourly instants they
share, and at the hours between only one of them exists. Note that
Open-Meteo itself interpolates native 3-hourly or 6-hourly models to hourly
output; the notes say so for the models whose native step is documented
(:data:`KNOWN_NATIVE_STEP_S`).
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise

import numpy as np

from rwi.contracts import ModelComparison, ModelDisagreement, VerificationScore, WeatherSeries
from rwi.core.geo import GeoPoint, geodesic_distance_m
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import ensure_utc, utc_now
from rwi.core.variables import spec_for
from rwi.fusion.selection import preferred_model
from rwi.fusion.thresholds import THRESHOLD_BASIS, resolve_threshold

__all__ = [
    "DEFAULT_ALIGNMENT_TOLERANCE_S",
    "KNOWN_NATIVE_STEP_S",
    "MAX_POINT_SEPARATION_M",
    "compare_models",
    "disagreements",
]

DEFAULT_ALIGNMENT_TOLERANCE_S = 300.0
"""Models are matched when their valid times differ by at most this. Open-Meteo
stamps everything on the hour, so five minutes only absorbs sub-hourly feeds and
clock noise and is far below any model step; it is a convention, not a theorem."""

MAX_POINT_SEPARATION_M = 50_000.0
"""Series whose points are further apart than this are not 'the same point'.
Two 25 km grid cells can legitimately be ~35 km apart, so this is generous."""

KNOWN_NATIVE_STEP_S: Mapping[str, float] = {
    "ecmwf_ifs": 3600.0,
    "ecmwf_ifs025": 10800.0,
    "ecmwf_aifs025_single": 21600.0,
}
"""Native output step of the models whose cadence is verified in
docs/science/providers.md (sec. 1, single-model row). Open-Meteo interpolates
these to hourly output. Models not listed are simply not annotated - no claim."""

_ALLOWED_KINDS = frozenset({DataKind.FORECAST, DataKind.ANALYSIS})
_CIRCULAR_VARIABLES = frozenset({"wind_direction_10m"})


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------


@dataclass
class _Point:
    time: datetime
    values: dict[str, float]
    max_offset_s: float = 0.0


@dataclass
class _Alignment:
    points: list[_Point]
    notes: list[str] = field(default_factory=list)
    used_models: list[str] = field(default_factory=list)


def _validated(
    series_by_model: Mapping[str, WeatherSeries], variable: str
) -> tuple[list[str], GeoPoint]:
    """Check the inputs and return (model names sorted, the common point)."""
    spec_for(variable)  # raises UnknownVariableError for a non-canonical name
    if not series_by_model:
        raise ValueError("series_by_model is empty: nothing to compare")
    names = sorted(series_by_model)
    kinds = {series_by_model[n].kind for n in names}
    if len(kinds) != 1:
        raise ValueError(
            "models must share one data kind, got "
            + ", ".join(sorted(k.value for k in kinds))
            + "; forecasts and analyses must not be compared as if they were the same thing"
        )
    kind = next(iter(kinds))
    if kind not in _ALLOWED_KINDS:
        raise ValueError(
            f"cannot compare series of kind {kind.value!r}: only model output "
            "(forecast or analysis) can disagree"
        )
    first = series_by_model[names[0]]
    anchor = first.requested_point or first.point
    for name in names[1:]:
        other = series_by_model[name]
        for candidate in (other.requested_point or other.point, other.point):
            if geodesic_distance_m(anchor, candidate) > MAX_POINT_SEPARATION_M:
                raise ValueError(
                    f"{name!r} is for {candidate}, more than "
                    f"{MAX_POINT_SEPARATION_M / 1000:.0f} km from {names[0]!r} at {anchor}: "
                    "not the same point"
                )
    return names, anchor


def _align(
    series_by_model: Mapping[str, WeatherSeries],
    names: Sequence[str],
    variable: str,
    tolerance_s: float,
) -> _Alignment:
    if not math.isfinite(tolerance_s) or tolerance_s < 0.0:
        raise ValueError("tolerance_s must be finite and >= 0")
    data: dict[str, list[tuple[datetime, float]]] = {
        n: [(ensure_utc(t), v) for t, v in series_by_model[n].series(variable)] for n in names
    }
    times_of = {n: [t for t, _ in data[n]] for n in names}
    time_sets = {n: set(ts) for n, ts in times_of.items()}
    all_times = sorted({t for n in names for t in times_of[n]})
    window = timedelta(seconds=tolerance_s)

    points: list[_Point] = []
    multi: dict[str, int] = {}
    consumed: set[tuple[str, datetime]] = set()
    for ref in all_times:
        if all((n, ref) in consumed for n in names if ref in time_sets[n]):
            continue  # every value stamped at this time already joined an earlier point
        values: dict[str, float] = {}
        max_offset = 0.0
        for n in names:
            ts = times_of[n]
            lo = bisect.bisect_left(ts, ref)
            hi = bisect.bisect_right(ts, ref + window)
            in_window = [i for i in range(lo, hi) if (n, ts[i]) not in consumed]
            if not in_window:
                continue
            chosen = in_window[0]  # the earliest, i.e. closest to the reference time
            if len(in_window) > 1:
                multi[n] = multi.get(n, 0) + 1
            for i in in_window:
                consumed.add((n, ts[i]))
            values[n] = data[n][chosen][1]
            max_offset = max(max_offset, (ts[chosen] - ref).total_seconds())
        if values:
            points.append(_Point(time=ref, values=values, max_offset_s=max_offset))

    alignment = _Alignment(points=points)
    alignment.used_models = [n for n in names if data[n]]
    alignment.notes = _alignment_notes(series_by_model, names, data, points, multi, tolerance_s)
    return alignment


def _alignment_notes(
    series_by_model: Mapping[str, WeatherSeries],
    names: Sequence[str],
    data: Mapping[str, list[tuple[datetime, float]]],
    points: Sequence[_Point],
    multi: Mapping[str, int],
    tolerance_s: float,
) -> list[str]:
    notes = [
        "Alignment: a comparison point is built at a reference time (the earliest unassigned "
        f"valid time of any model); each model contributes its value within +{tolerance_s:g} s "
        "of it. Nothing is interpolated, resampled or filled: a model with no value in that "
        "window is absent from the point, and a point needs at least two models to count as a "
        "comparison."
    ]
    empty = [n for n in names if not data[n]]
    if empty:
        notes.append(
            "No usable values for this variable from: "
            + ", ".join(empty)
            + ". They are listed with an empty series and take no part in any comparison."
        )
    comparable = [p for p in points if len(p.values) >= 2]
    full = [p for p in comparable if len(p.values) == len(names)]
    notes.append(
        f"{len(comparable)} comparison point(s) with two or more models, {len(full)} with all "
        f"{len(names)} model(s); {len(points) - len(comparable)} time(s) had a value from only "
        "one model and produced no comparison."
    )
    absent = {n: sum(1 for p in comparable if n not in p.values) for n in names if data[n]}
    absent = {n: c for n, c in absent.items() if c}
    if absent:
        notes.append(
            "Models absent (not interpolated) at comparison points: "
            + ", ".join(f"{n} at {c}" for n, c in sorted(absent.items()))
            + "."
        )
    offsets = [p.max_offset_s for p in comparable if p.max_offset_s > 0.0]
    if offsets:
        notes.append(
            f"{len(offsets)} comparison point(s) matched values whose time stamps differ "
            f"(largest offset {max(offsets):g} s, within the {tolerance_s:g} s tolerance); the "
            "values were used as given, not shifted or interpolated."
        )
    if multi:
        notes.append(
            "More than one value fell in a single alignment window for "
            + ", ".join(f"{n} ({c}x)" for n, c in sorted(multi.items()))
            + "; the earliest was used and the others were dropped from the comparison."
        )
    for n in names:
        series = series_by_model[n]
        native = KNOWN_NATIVE_STEP_S.get(series.model_name or n)
        step = _median_step_s(data[n])
        if native is not None and step is not None and native > step + 1.0:
            notes.append(
                f"{n}: native step is {native / 3600:g} h but the series step is "
                f"{step / 3600:g} h, so the provider interpolated; values between native steps "
                "are not independent model output, and disagreement there is partly "
                "interpolation."
            )
    return notes


def _median_step_s(series: Sequence[tuple[datetime, float]]) -> float | None:
    if len(series) < 2:
        return None
    diffs = [(b[0] - a[0]).total_seconds() for a, b in pairwise(series)]
    positive = [d for d in diffs if d > 0.0]
    return float(statistics.median(positive)) if positive else None


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def _linear_stats(values: Sequence[float]) -> tuple[float, float, float, float, str | None]:
    arr = np.asarray(values, dtype=float)
    return (
        float(arr.max() - arr.min()),
        float(np.std(arr)),
        float(np.mean(arr)),
        float(np.median(arr)),
        None,
    )


def _circular_stats(values: Sequence[float]) -> tuple[float, float, float, float, str | None]:
    """Spread, standard deviation, mean and median of directions in degrees.

    Spread is the smallest arc that contains every direction (360 minus the
    largest gap between neighbours), so 350 and 10 are 20 apart, not 340. The
    mean is the vector mean; the standard deviation is the root mean square of
    the signed angular deviations from it; the median is the circular mean
    plus the median deviation.
    """
    angles = np.mod(np.asarray(values, dtype=float), 360.0)
    ordered = np.sort(angles)
    gaps = np.diff(np.concatenate([ordered, [ordered[0] + 360.0]]))
    spread = float(360.0 - gaps.max()) if len(ordered) > 1 else 0.0
    rad = np.deg2rad(angles)
    s, c = float(np.sin(rad).sum()), float(np.cos(rad).sum())
    if math.hypot(s, c) < 1e-9 * len(angles):
        # Directions cancel (e.g. exactly opposite): the mean is undefined.
        reference = float(angles[0])
        note = (
            "circular mean undefined (directions cancel); mean and median are the first "
            "model's direction (by model name), not a statistic"
        )
        mean = reference
    else:
        mean = float(math.degrees(math.atan2(s, c)) % 360.0)
        note = None
    dev = (angles - mean + 180.0) % 360.0 - 180.0
    median = float((mean + np.median(dev)) % 360.0)
    return spread, float(np.sqrt(np.mean(dev**2))), mean, median, note


def _stats(variable: str, values: Sequence[float]) -> tuple[float, float, float, float, str | None]:
    return _circular_stats(values) if variable in _CIRCULAR_VARIABLES else _linear_stats(values)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _build_disagreements(
    series_by_model: Mapping[str, WeatherSeries],
    names: Sequence[str],
    point: GeoPoint,
    variable: str,
    threshold: float,
    alignment: _Alignment,
    basis_note: str | None,
) -> list[ModelDisagreement]:
    unit = spec_for(variable).unit
    retrieved = max(ensure_utc(series_by_model[n].retrieved_at_utc) for n in names)
    out: list[ModelDisagreement] = []
    for p in alignment.points:
        if len(p.values) < 2:
            continue
        ordered = {n: p.values[n] for n in names if n in p.values}
        spread, std, mean, median, stat_note = _stats(variable, list(ordered.values()))
        notes = [
            "Spread across " + ", ".join(ordered) + "; computed by rwi.fusion from the "
            "models' own values, nothing interpolated.",
            f"Significance threshold {threshold:g} {unit}"
            + (f" ({basis_note})" if basis_note else " (caller supplied)")
            + ".",
        ]
        if p.max_offset_s > 0.0:
            notes.append(f"Source time stamps differ by up to {p.max_offset_s:g} s.")
        if stat_note:
            notes.append(stat_note)
        out.append(
            ModelDisagreement(
                variable=variable,
                valid_time_utc=p.time,
                point=point,
                unit=unit,
                values_by_model=ordered,
                spread=spread,
                standard_deviation=std,
                mean=mean,
                median=median,
                is_significant=spread > threshold,
                threshold=threshold,
                provenance=Provenance(
                    provider="rwi",
                    dataset="fusion/model_disagreement",
                    kind=DataKind.DERIVED,
                    retrieved_at_utc=retrieved,
                    valid_time_utc=p.time,
                    point=point,
                    processing="spread_across_models",
                    notes=" ".join(notes),
                ),
            )
        )
    return out


def disagreements(
    series_by_model: Mapping[str, WeatherSeries],
    *,
    variable: str,
    threshold: float,
    tolerance_s: float = DEFAULT_ALIGNMENT_TOLERANCE_S,
) -> list[ModelDisagreement]:
    """Disagreement at every time at which at least two models have a value.

    ``spread`` is max minus min (the smallest containing arc for wind
    direction), ``standard_deviation`` is the population standard deviation of
    the models' values (ddof=0: the models are the whole population, not a
    sample), and ``is_significant`` is ``spread > threshold``. Ordered by time;
    ``values_by_model`` is ordered by model name. A single model yields an
    empty list: nothing to disagree with.
    """
    if not math.isfinite(threshold) or threshold < 0.0:
        raise ValueError("threshold must be finite and >= 0")
    names, point = _validated(series_by_model, variable)
    alignment = _align(series_by_model, names, variable, tolerance_s)
    return _build_disagreements(series_by_model, names, point, variable, threshold, alignment, None)


def compare_models(
    series_by_model: Mapping[str, WeatherSeries],
    *,
    variable: str,
    significance_thresholds: Mapping[str, float] | None = None,
    tolerance_s: float = DEFAULT_ALIGNMENT_TOLERANCE_S,
    verification_scores: Sequence[VerificationScore] | None = None,
    lead_time_s: float | None = None,
    now: datetime | None = None,
) -> ModelComparison:
    """Align several models for one point and variable and measure their disagreement.

    ``series_by_model`` keys are the model names used throughout the result.
    ``significance_thresholds`` overrides :data:`DEFAULT_SIGNIFICANCE_THRESHOLDS`
    per variable (engineering judgements, see :mod:`rwi.fusion.thresholds`); a
    variable with neither raises ``ValueError``.

    ``preferred_model`` is ``None`` unless ``verification_scores`` AND a
    ``lead_time_s`` are given and :func:`rwi.fusion.selection.preferred_model`
    finds earned evidence for one of the compared models. ``preference_basis``
    always says why.
    """
    names, point = _validated(series_by_model, variable)
    threshold = resolve_threshold(variable, significance_thresholds)
    overridden = significance_thresholds is not None and variable in significance_thresholds
    basis = THRESHOLD_BASIS.get(variable)
    basis_note = "caller supplied" if overridden else (basis.status if basis is not None else None)
    alignment = _align(series_by_model, names, variable, tolerance_s)
    found = _build_disagreements(
        series_by_model, names, point, variable, threshold, alignment, basis_note
    )

    notes = list(alignment.notes)
    if len(alignment.used_models) <= 1:
        notes.append(
            f"Only {len(alignment.used_models)} model(s) supplied usable data: models cannot "
            "be compared. No disagreement was computed - this is NOT evidence that the "
            "forecast is certain, and an absent spread must not be read as agreement."
        )
    elif not found:
        notes.append(
            "Two or more models have data but none at a shared time: not comparable, which is "
            "different from agreeing."
        )
    significant = sum(1 for d in found if d.is_significant)
    notes.append(
        f"Threshold {threshold:g} {spec_for(variable).unit} for {variable} ("
        + ("caller supplied" if overridden else "default; engineering judgement, not physics")
        + f"); {significant} of {len(found)} comparison point(s) exceed it."
    )
    notes.extend(_context_notes(series_by_model, names))

    preferred, preference_basis = _preference(
        names, variable, verification_scores, lead_time_s, found
    )
    return ModelComparison(
        point=point,
        variable=variable,
        generated_at_utc=ensure_utc(now) if now is not None else utc_now(),
        series_by_model={
            n: [(ensure_utc(t), v) for t, v in series_by_model[n].series(variable)] for n in names
        },
        disagreements=found,
        preferred_model=preferred,
        preference_basis=preference_basis,
        notes=notes,
    )


def _preference(
    names: Sequence[str],
    variable: str,
    scores: Sequence[VerificationScore] | None,
    lead_time_s: float | None,
    found: Sequence[ModelDisagreement],
) -> tuple[str | None, str]:
    if scores is None or lead_time_s is None:
        return None, (
            "No verification scores (and lead time) were supplied to compare_models, so no "
            "model has earned preference; models are shown side by side without ranking."
        )
    name, reason = preferred_model(scores, variable=variable, lead_time_s=lead_time_s)
    if name is not None and name not in names:
        return None, (
            f"Verification favours {name!r}, which is not among the compared models "
            f"({', '.join(names)}); no preference is stated. {reason}"
        )
    return name, reason


def _context_notes(series_by_model: Mapping[str, WeatherSeries], names: Sequence[str]) -> list[str]:
    notes: list[str] = []
    cells = {n: series_by_model[n].point for n in names}
    distinct = {(p.latitude_deg, p.longitude_deg) for p in cells.values()}
    if len(distinct) > 1:
        far = max(geodesic_distance_m(cells[a], cells[b]) for a in names for b in names if a < b)
        notes.append(
            f"Models are sampled at different grid points (up to {far / 1000:.1f} km apart): "
            "part of any disagreement is spatial representativeness, not model physics."
        )
    inits = {
        n: ensure_utc(s.model_init_time_utc)
        for n in names
        if (s := series_by_model[n]).model_init_time_utc is not None
    }
    unknown = [n for n in names if n not in inits]
    if unknown:
        notes.append(
            "Model run (initialisation) time unknown for: " + ", ".join(unknown) + "; run "
            "timing can account for part of a disagreement."
        )
    if len(set(inits.values())) > 1:
        notes.append(
            "Models come from different runs ("
            + ", ".join(f"{n} {t:%Y-%m-%d %H:%MZ}" for n, t in sorted(inits.items()))
            + "); a fresher run is not the same as a better model."
        )
    return notes
