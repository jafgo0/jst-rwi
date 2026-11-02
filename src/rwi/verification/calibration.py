"""Additive bias correction, applied only where the evidence supports it.

See ``docs/science/verification.md`` ("What bias correction may and may not do").
A correction exists only for a score with ``is_meaningful`` (>= 30 pairs); every
corrected value says so in its provenance ``processing`` field.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from rwi.contracts import VerificationScore, WeatherSeries, WeatherStep
from rwi.core.provenance import DataKind, Measured, QualityFlag
from rwi.core.timebase import ensure_utc
from rwi.core.variables import CANONICAL_VARIABLES, VariableClass
from rwi.verification.metrics import bucket_label, is_circular_variable

CORRECTION_MARKER = "bias_corrected("
"""Prefix of the provenance ``processing`` entry; also stops a value being corrected twice."""

_ZERO_PRESERVING = frozenset({VariableClass.RADIATION, VariableClass.PRECIPITATION})
"""Classes whose zero is physical (night, dry): an additive correction must not invent signal."""


def _select_score(
    scores: Sequence[VerificationScore],
    *,
    variable: str,
    provider: str,
    model_name: str | None,
    lead_time_s: float,
) -> VerificationScore | None:
    """The meaningful score covering this lead time (most samples wins, then lowest bucket)."""
    candidates = [
        s
        for s in scores
        if s.variable == variable
        and s.provider == provider
        and s.model_name == model_name
        and s.lead_time_bucket_s[0] <= lead_time_s < s.lead_time_bucket_s[1]
        and s.is_meaningful
        and s.bias is not None
        and math.isfinite(s.bias)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda s: (-s.sample_count, s.lead_time_bucket_s))


def bias_correction(
    scores: Sequence[VerificationScore],
    *,
    variable: str,
    provider: str,
    model_name: str | None,
    lead_time_s: float,
) -> float | None:
    """Additive correction (``-bias``) for the matching bucket, or ``None``.

    ``None`` unless a score for this exact (variable, provider, model_name) whose
    half-open lead-time bucket contains ``lead_time_s`` ``is_meaningful``.
    A model that runs 1.5 degC too warm has bias +1.5, so the correction is -1.5.
    """
    score = _select_score(
        scores,
        variable=variable,
        provider=provider,
        model_name=model_name,
        lead_time_s=lead_time_s,
    )
    if score is None or score.bias is None:
        return None
    return -score.bias


def _fmt(x: float) -> str:
    return f"{x:+.3g}"


def _correct_value(
    item: Measured,
    *,
    lead_time_s: float,
    series: WeatherSeries,
    scores: Sequence[VerificationScore],
) -> Measured:
    if item.value is None or not item.is_usable:
        return item
    if item.provenance.processing and CORRECTION_MARKER in item.provenance.processing:
        return item
    spec = CANONICAL_VARIABLES.get(item.variable)
    if spec is None or spec.variable_class is VariableClass.STATE:
        return item
    score = _select_score(
        scores,
        variable=item.variable,
        provider=series.provider,
        model_name=series.model_name,
        lead_time_s=lead_time_s,
    )
    if score is None or score.unit != item.unit or score.bias is None:
        return item
    correction = -score.bias
    raw = item.value

    clip_note = ""
    if is_circular_variable(item.variable):
        corrected = (raw + correction) % 360.0
        if corrected != raw + correction:
            clip_note = "; wrapped_to_0_360"
    else:
        if spec.variable_class in _ZERO_PRESERVING and raw <= spec.minimum:
            return item  # zero stays zero: no night irradiance or drizzle from a mean bias
        corrected = raw + correction
        clipped = min(max(corrected, spec.minimum), spec.maximum)
        if clipped != corrected:
            clip_note = (
                f"; clipped_to_registry_range({spec.minimum:g}..{spec.maximum:g} {spec.unit}: "
                f"{corrected:.6g} -> {clipped:.6g})"
            )
            corrected = clipped

    label = score.model_name or score.provider
    entry = (
        f"{CORRECTION_MARKER}{_fmt(correction)} {score.unit}, n={score.sample_count}, "
        f"{label}, {bucket_label(score.lead_time_bucket_s)}){clip_note}"
    )
    previous = item.provenance.processing
    processing = f"{previous}; {entry}" if previous else entry
    notes = item.provenance.notes
    raw_note = f"raw_model_value={raw:.6g} {item.unit}"
    provenance = item.provenance.model_copy(
        update={"processing": processing, "notes": f"{notes}; {raw_note}" if notes else raw_note}
    )
    return item.model_copy(update={"value": corrected, "provenance": provenance})


def apply_bias_correction(
    series: WeatherSeries, scores: Sequence[VerificationScore]
) -> WeatherSeries:
    """Return a NEW series with meaningful, bucket-matched bias corrections applied.

    * only ``DataKind.FORECAST`` series are corrected (observations never are);
    * the lead time of each step is ``valid_time - model_init_time`` (series
      level, else the value's own provenance); unknown lead time -> untouched;
    * values with no meaningful matching score, unusable values and state
      variables (``is_day``, ``weather_code``) pass through unchanged;
    * each corrected value records the correction (and any clip/wrap) in
      ``provenance.processing`` and the raw value in ``provenance.notes``;
    * results are clipped to the registry range, circular variables wrapped.
    """
    if series.kind is not DataKind.FORECAST:
        return series.model_copy(deep=True)
    new_steps: list[WeatherStep] = []
    for step in series.steps:
        new_values: dict[str, Measured] = {}
        for name, item in step.values.items():
            init = series.model_init_time_utc or item.provenance.model_init_time_utc
            if init is None or item.quality is QualityFlag.MISSING:
                new_values[name] = item
                continue
            lead = (ensure_utc(step.valid_time_utc) - ensure_utc(init)).total_seconds()
            if lead < 0.0 or name not in CANONICAL_VARIABLES:
                new_values[name] = item
                continue
            new_values[name] = _correct_value(item, lead_time_s=lead, series=series, scores=scores)
        new_steps.append(step.model_copy(update={"values": new_values}))
    return series.model_copy(update={"steps": new_steps})


__all__ = ["CORRECTION_MARKER", "apply_bias_correction", "bias_correction"]
