"""Run verification against anything satisfying the ``ForecastArchive`` protocol."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from rwi.contracts import (
    ForecastArchive,
    ForecastObservationPair,
    VerificationReport,
    VerificationScore,
)
from rwi.core.errors import UnknownVariableError
from rwi.core.timebase import TimeWindow, ensure_utc, utc_now
from rwi.core.variables import spec_for
from rwi.verification.metrics import (
    bucket_for,
    bucket_label,
    finite_pairs,
    score_pairs,
    validate_buckets,
)


def verify(
    archive: ForecastArchive,
    *,
    variables: Sequence[str],
    period: TimeWindow | None = None,
    providers: Sequence[str] | None = None,
    lead_time_buckets_s: Sequence[tuple[float, float]] | None = None,
    now: datetime | None = None,
) -> VerificationReport:
    """Score every requested variable using pairs from ``archive``.

    A variable with no pairs, an unknown variable, an archive error, ignored
    pairs and thin buckets are all stated in ``notes``; nothing is dropped
    silently. ``period`` is the requested window when given, otherwise the span
    of valid times actually scored (first to last valid time).
    """
    if not variables:
        raise ValueError("verify needs at least one variable")
    if providers is not None and len(providers) == 0:
        raise ValueError("providers must be None (all) or a non-empty sequence")
    buckets = validate_buckets(lead_time_buckets_s)
    computed_at = ensure_utc(now) if now is not None else utc_now()
    unique_variables = tuple(dict.fromkeys(variables))
    provider_list: Sequence[str | None] = (
        [None] if providers is None else list(dict.fromkeys(providers))
    )

    notes: list[str] = []
    scores: list[VerificationScore] = []
    pair_count = 0
    valid_times: list[datetime] = []

    for variable in unique_variables:
        try:
            unit = spec_for(variable).unit
        except UnknownVariableError:
            notes.append(f"{variable}: not a canonical variable; not scored")
            continue

        pairs: list[ForecastObservationPair] = []
        failed = False
        for provider in provider_list:
            try:
                pairs.extend(archive.pairs(variable=variable, period=period, provider=provider))
            except Exception as exc:  # archive trouble must degrade to a stated gap
                failed = True
                where = f" (provider {provider})" if provider else ""
                notes.append(
                    f"{variable}: archive error{where}: {type(exc).__name__}: {exc}; "
                    "verification unavailable for this query"
                )
        if not pairs:
            if not failed:
                scope = f" from providers {', '.join(providers)}" if providers else ""
                window = " in the requested period" if period is not None else ""
                notes.append(
                    f"{variable}: no forecast/observation pairs{scope}{window}; not scored"
                )
            continue

        wrong = [p for p in pairs if p.variable != variable]
        if wrong:
            notes.append(f"{variable}: {len(wrong)} pair(s) of another variable ignored")
        pairs = [p for p in pairs if p.variable == variable]
        pair_count += len(pairs)

        usable = finite_pairs(pairs)
        if len(usable) != len(pairs):
            dropped = len(pairs) - len(usable)
            notes.append(f"{variable}: {dropped} pair(s) with non-finite values ignored")
        outside = [p for p in usable if bucket_for(p.lead_time_s, buckets) is None]
        if outside:
            notes.append(
                f"{variable}: {len(outside)} pair(s) with lead time outside all buckets ignored"
            )
        valid_times.extend(ensure_utc(p.valid_time_utc) for p in usable)

        var_scores = score_pairs(
            pairs, variable=variable, unit=unit, lead_time_buckets_s=buckets, now=computed_at
        )
        scores.extend(var_scores)
        if not var_scores:
            notes.append(f"{variable}: pairs retrieved but none could be scored")
        for s in var_scores:
            tag = (
                f"{variable} {s.provider}/{s.model_name or '-'} "
                f"{bucket_label(s.lead_time_bucket_s)}"
            )
            if not s.is_meaningful:
                notes.append(
                    f"{tag}: only {s.sample_count} pair(s) (< 30); provisional, "
                    "not usable as evidence or for bias correction"
                )
            if s.skill_vs_persistence is None:
                notes.append(f"{tag}: skill vs persistence unavailable")
            elif s.skill_vs_persistence <= 0.0:
                notes.append(
                    f"{tag}: does not beat persistence (skill {s.skill_vs_persistence:+.3f})"
                )

    if scores and not any(s.is_meaningful for s in scores):
        notes.append("no score reaches 30 pairs: this report is not evidence for any model")

    covered = period
    if covered is None and valid_times:
        covered = TimeWindow(start_utc=min(valid_times), end_utc=max(valid_times))

    return VerificationReport(
        scores=scores,
        computed_at_utc=computed_at,
        period=covered,
        pair_count=pair_count,
        variables=unique_variables,
        notes=notes,
    )
