"""Self-knowledge: what the platform knows about its own forecast skill.

The honest briefing says not just "here is the forecast" but "and here
is how well forecasts of this kind have actually performed on this
platform." That answer lives in the verification archive. This module
reads it; it does not score anything itself - all metrics come straight
from :func:`rwi.verification.verify` run over the canonical lead-time
buckets (:data:`rwi.verification.metrics.DEFAULT_LEAD_TIME_BUCKETS_S`).

Why reuse the canonical buckets: scoring with a parallel set of buckets
would make the briefing's skill numbers diverge from the ones the
platform actually computes. The race dashboard, the API's
/forecast-quality endpoints, and this module must all speak about the
same buckets.

A per-variable aggregate across all six buckets is produced for
operator readability, and the aggregate is explicitly labelled as such
so nothing hides what was done.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rwi.archive.errors import ArchiveError, ArchiveUnavailableError
from rwi.archive.store import SqliteArchive
from rwi.contracts import VerificationScore
from rwi.verification.metrics import (
    DEFAULT_LEAD_TIME_BUCKETS_S,
    MIN_PERSISTENCE_COVERAGE,
    MIN_PERSISTENCE_MATCHES,
)
from rwi.verification.report import verify

Verdict = Literal["meningsfull", "sämre_än_persistens", "oprövad", "saknas"]


class SkillStatus(BaseModel):
    """A per-variable rollup across the canonical lead-time buckets.

    Numbers come from one :class:`VerificationScore` per bucket - the
    sample_count is summed, the MAE is the sample-weighted mean, and
    the skill_vs_persistence is the sample-weighted mean of the buckets
    whose skill the engine could compute. The verdict is derived from
    :func:`rwi.verification.metrics.skill_score` and the engine's own
    :data:`MIN_PERSISTENCE_MATCHES` / :data:`MIN_PERSISTENCE_COVERAGE`
    gates - no new threshold is invented here.
    """

    model_config = ConfigDict(frozen=True)

    variable: str
    sample_count: int
    mean_absolute_error: float | None
    skill_vs_persistence: float | None
    bucket_count: int
    verdict: Verdict
    detail: str


class SelfKnowledgeSummary(BaseModel):
    """A compact answer to 'what does this platform know about its own skill?'."""

    model_config = ConfigDict(frozen=True)

    archive_available: bool
    total_pairs: int
    coverage_by_variable: dict[str, SkillStatus]
    oldest_pair_utc: datetime | None
    newest_pair_utc: datetime | None
    verdict: str
    detail: str


def _verdict_for(
    *, sample_count: int, skill: float | None
) -> tuple[Verdict, str]:
    """Derive the verdict from the engine's own gates and skill_score.

    ``meningsfull`` requires the engine to have produced a skill number
    AND that number to be strictly positive (model beats persistence).
    A negative skill is reported honestly as ``sämre_än_persistens`` -
    the most self-critical outcome the platform can print about itself.
    """
    if sample_count == 0:
        return "saknas", "inga lagrade par"
    if skill is None:
        return (
            "oprövad",
            f"persistens-baseline saknas (kräver ≥{MIN_PERSISTENCE_MATCHES} matchade "
            f"par och ≥{MIN_PERSISTENCE_COVERAGE:.0%} täckning i någon bucket)",
        )
    if skill > 0.0:
        return "meningsfull", f"skicklighet mot persistens {skill:+.2f}"
    return (
        "sämre_än_persistens",
        f"skicklighet mot persistens {skill:+.2f} — modellen slår inte en "
        "persistens-baseline lokalt",
    )


def _aggregate(
    variable: str, bucket_scores: list[VerificationScore]
) -> SkillStatus:
    """Sample-weighted aggregate across the canonical lead-time buckets.

    The aggregate is labelled as such in ``detail`` so nothing hides
    what was done.
    """
    total = sum(s.sample_count for s in bucket_scores)
    if total == 0:
        verdict, reason = _verdict_for(sample_count=0, skill=None)
        return SkillStatus(
            variable=variable,
            sample_count=0,
            mean_absolute_error=None,
            skill_vs_persistence=None,
            bucket_count=len(bucket_scores),
            verdict=verdict,
            detail=f"aggregerat över {len(bucket_scores)} canonical-buckets: {reason}",
        )
    mae_num = 0.0
    mae_den = 0
    skill_num = 0.0
    skill_den = 0
    for s in bucket_scores:
        if s.mean_absolute_error is not None and s.sample_count > 0:
            mae_num += s.mean_absolute_error * s.sample_count
            mae_den += s.sample_count
        if s.skill_vs_persistence is not None and s.sample_count > 0:
            skill_num += s.skill_vs_persistence * s.sample_count
            skill_den += s.sample_count
    mae = mae_num / mae_den if mae_den > 0 else None
    skill = skill_num / skill_den if skill_den > 0 else None
    verdict, reason = _verdict_for(sample_count=total, skill=skill)
    detail = (
        f"aggregerat över {len(bucket_scores)} canonical-buckets, "
        f"{total} par totalt: {reason}"
    )
    return SkillStatus(
        variable=variable,
        sample_count=total,
        mean_absolute_error=mae,
        skill_vs_persistence=skill,
        bucket_count=len(bucket_scores),
        verdict=verdict,
        detail=detail,
    )


def _overall_verdict(
    archive_available: bool,
    total_pairs: int,
    coverage: dict[str, SkillStatus],
) -> tuple[str, str]:
    if not archive_available:
        return (
            "arkivet kunde inte öppnas",
            "ingen lagrad verifiering — alla skicklighetsutsagor nedan är "
            "obevisade lokalt",
        )
    if total_pairs == 0:
        return (
            "inga lagrade par",
            "arkivet är tomt — plattformen har ingen egen track record ännu",
        )
    meaningful = sum(1 for s in coverage.values() if s.verdict == "meningsfull")
    worse = sum(1 for s in coverage.values() if s.verdict == "sämre_än_persistens")
    tested = sum(1 for s in coverage.values() if s.sample_count > 0)
    verdict = f"{meaningful}/{tested} variabler slår persistens lokalt"
    detail_parts = [
        f"totalt {total_pairs} lagrade par",
        f"{tested} variabler har par",
        f"{meaningful} slår persistens",
    ]
    if worse > 0:
        detail_parts.append(f"{worse} sämre än persistens")
    return verdict, "; ".join(detail_parts)


def platform_confidence(
    archive: SqliteArchive,
    *,
    variables: tuple[str, ...],
    now: datetime,
) -> SelfKnowledgeSummary:
    """Summarise what the archive knows about forecast skill for ``variables``.

    Catches :class:`ArchiveUnavailableError` at the first read (not at
    open time, because :func:`rwi.archive.store.open_archive` is lazy)
    and reports ``archive_available=False`` with an empty coverage. A
    known variable that has zero pairs becomes a ``SkillStatus`` with
    verdict ``saknas`` so the briefing says so plainly.
    """
    coverage: dict[str, SkillStatus] = {}
    # Probe .pairs() directly: open_archive is lazy, so the first real read
    # is where ArchiveUnavailableError surfaces. If we cannot even read an
    # empty slice, the archive is unavailable and every skill claim below
    # would be obevisad lokalt.
    try:
        _ = list(archive.pairs(variable=variables[0]))
    except ArchiveUnavailableError as exc:
        verdict, detail = _overall_verdict(False, 0, coverage)
        return SelfKnowledgeSummary(
            archive_available=False,
            total_pairs=0,
            coverage_by_variable={},
            oldest_pair_utc=None,
            newest_pair_utc=None,
            verdict=verdict,
            detail=f"{detail}: {exc}",
        )
    except ArchiveError as exc:
        verdict, detail = _overall_verdict(False, 0, coverage)
        return SelfKnowledgeSummary(
            archive_available=False,
            total_pairs=0,
            coverage_by_variable={},
            oldest_pair_utc=None,
            newest_pair_utc=None,
            verdict=verdict,
            detail=f"{detail} (archive error): {exc}",
        )
    report = verify(
        archive,
        variables=variables,
        lead_time_buckets_s=DEFAULT_LEAD_TIME_BUCKETS_S,
        now=now,
    )
    by_variable: dict[str, list[VerificationScore]] = {v: [] for v in variables}
    for score in report.scores:
        if score.variable in by_variable:
            by_variable[score.variable].append(score)
    for v, scores in by_variable.items():
        coverage[v] = _aggregate(v, scores)
    total_pairs = report.pair_count
    oldest = report.period.start_utc if report.period is not None else None
    newest = report.period.end_utc if report.period is not None else None
    verdict, detail = _overall_verdict(True, total_pairs, coverage)
    return SelfKnowledgeSummary(
        archive_available=True,
        total_pairs=total_pairs,
        coverage_by_variable=coverage,
        oldest_pair_utc=oldest,
        newest_pair_utc=newest,
        verdict=verdict,
        detail=detail,
    )


__all__ = [
    "SelfKnowledgeSummary",
    "SkillStatus",
    "Verdict",
    "platform_confidence",
]
