"""Evidence-based model preference and (unused-for-now) evidence weights.

THE HARD RULE
-------------
A model is *preferred* only when there is a verification score for exactly the
variable and lead time in question that

  1. is meaningful (``VerificationScore.is_meaningful``: enough samples), AND
  2. beats persistence (``skill_vs_persistence > 0``), the trivial "nothing
     changes" baseline.

Otherwise the answer is ``(None, reason)``. There is no fallback to the first
model, to alphabetical order, to a "default" model, to the lowest MAE on a
handful of samples, or to reputation. With no verification history, "no model
has earned preference yet" is the correct and expected answer.

The model identity used here is ``VerificationScore.model_name``, falling back
to ``provider`` when the score carries no model name. When several scores match
one model, variable and lead time (different periods, say) the one with the
most samples is used.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rwi.contracts import VerificationScore

__all__ = [
    "MEANINGFUL_SAMPLE_COUNT",
    "ModelWeights",
    "model_weights",
    "preferred_model",
]

MEANINGFUL_SAMPLE_COUNT = 30
"""Mirror of the threshold inside ``VerificationScore.is_meaningful`` - used only
to word messages. The decision itself always calls ``is_meaningful``; a test
keeps this constant honest."""


def _label(score: VerificationScore) -> str:
    return score.model_name if score.model_name else score.provider


def _hours(seconds: float) -> str:
    return f"{seconds / 3600.0:g} h"


def _check_lead(lead_time_s: float) -> None:
    if not math.isfinite(lead_time_s) or lead_time_s < 0.0:
        raise ValueError("lead_time_s must be finite and >= 0")


def _matching(
    scores: Sequence[VerificationScore], variable: str, lead_time_s: float
) -> dict[str, VerificationScore]:
    """Best-sampled score per model for this variable and lead time (half-open bucket)."""
    out: dict[str, VerificationScore] = {}
    for s in scores:
        lo, hi = s.lead_time_bucket_s
        if s.variable != variable or not (lo <= lead_time_s < hi):
            continue
        key = _label(s)
        held = out.get(key)
        if held is None or s.sample_count > held.sample_count:
            out[key] = s
    return dict(sorted(out.items()))


def _beats_persistence(score: VerificationScore) -> bool:
    return score.skill_vs_persistence is not None and score.skill_vs_persistence > 0.0


def _skill_text(score: VerificationScore) -> str:
    if score.skill_vs_persistence is None:
        return "skill versus persistence unavailable (no persistence baseline)"
    return f"skill versus persistence {score.skill_vs_persistence:+.3f}"


def preferred_model(
    scores: Sequence[VerificationScore], *, variable: str, lead_time_s: float
) -> tuple[str | None, str]:
    """(model name, reason) - the name is ``None`` unless evidence has earned it.

    See the module docstring for the rule. When more than one model has earned
    preference, the one with the highest ``skill_vs_persistence`` wins (that
    measure is relative to each model's own sample, so it is comparable across
    models verified over different periods where raw MAE is not); an exact tie
    on skill and MAE yields ``None``. No significance test between models is
    available from the scores, and the reason says so.
    """
    _check_lead(lead_time_s)
    where = f"{variable} at lead time {_hours(lead_time_s)}"
    for_variable = [s for s in scores if s.variable == variable]
    if not for_variable:
        return None, (
            f"No model has earned preference for {where}: there are no verification scores "
            f"for {variable} at all ({len(scores)} score(s) supplied for other variables)."
            if scores
            else f"No model has earned preference for {where}: no verification scores exist yet."
        )
    matches = _matching(scores, variable, lead_time_s)
    if not matches:
        buckets = sorted({s.lead_time_bucket_s for s in for_variable})
        listed = ", ".join(f"[{_hours(lo)}, {_hours(hi)})" for lo, hi in buckets)
        return None, (
            f"No model has earned preference for {where}: no score covers that lead time "
            f"(scored lead-time buckets: {listed})."
        )

    meaningful = {k: s for k, s in matches.items() if s.is_meaningful}
    if not meaningful:
        counts = ", ".join(f"{k}: {s.sample_count} sample(s)" for k, s in matches.items())
        return None, (
            f"No model has earned preference for {where}: no score is meaningful "
            f"(needs at least {MEANINGFUL_SAMPLE_COUNT} verified samples; have {counts})."
        )

    earned = {k: s for k, s in meaningful.items() if _beats_persistence(s)}
    if not earned:
        detail = "; ".join(
            f"{k} over {s.sample_count} samples: {_skill_text(s)}" for k, s in meaningful.items()
        )
        return None, (
            f"No model has earned preference for {where}: no meaningful score beats the "
            f"persistence baseline ({detail}). A model that cannot beat persistence has not "
            "earned its place."
        )

    def rank(item: tuple[str, VerificationScore]) -> tuple[float, float]:
        s = item[1]
        mae = s.mean_absolute_error if s.mean_absolute_error is not None else math.inf
        return (-(s.skill_vs_persistence or 0.0), mae)

    ranked = sorted(earned.items(), key=rank)
    best_name, best = ranked[0]
    if len(ranked) > 1 and rank(ranked[1]) == rank(ranked[0]):
        names = ", ".join(k for k, s in ranked if rank((k, s)) == rank(ranked[0]))
        return None, (
            f"No model has earned preference for {where}: {names} are tied on skill versus "
            "persistence and MAE, and the order of names is not evidence."
        )

    others = [
        f"{k} ({s.sample_count} samples, "
        + (
            _skill_text(s)
            if s.is_meaningful
            else f"not meaningful, needs {MEANINGFUL_SAMPLE_COUNT}"
        )
        + ")"
        for k, s in matches.items()
        if k != best_name
    ]
    mae = (
        f", MAE {best.mean_absolute_error:.4g} {best.unit}"
        if best.mean_absolute_error is not None
        else ""
    )
    reason = (
        f"{best_name} is preferred for {where}: {best.sample_count} verified samples, "
        f"{_skill_text(best)}{mae}."
    )
    if others:
        reason += " Compared with " + "; ".join(others) + "."
        if len(ranked) == 1:
            reason += (
                " It is the only model with earned evidence - that is not proof it is better "
                "than the others, only that the others have not shown anything yet."
            )
    reason += (
        " Ranking uses skill versus persistence without a significance test between models; "
        "the preference applies to this variable and lead-time bucket only."
    )
    return best_name, reason


class ModelWeights(BaseModel):
    """Evidence-based weights per model. For later use; NOT applied to any forecast."""

    model_config = ConfigDict(frozen=True)

    variable: str
    lead_time_s: float
    weights: dict[str, float] = Field(description="model name -> weight; sums to 1.0 (or empty)")
    basis: Literal["inverse_mae", "equal"]
    note: str


def _normalise(raw: dict[str, float]) -> dict[str, float]:
    """Normalise to a sum of exactly 1.0 (the rounding residue goes on the largest weight)."""
    total = math.fsum(raw.values())
    weights = {k: v / total for k, v in raw.items()}
    keys = list(weights)
    largest = max(keys, key=lambda k: weights[k])
    rest = math.fsum(weights[k] for k in keys if k != largest)
    weights[largest] = 1.0 - rest
    return weights


def model_weights(
    scores: Sequence[VerificationScore],
    *,
    variable: str,
    lead_time_s: float,
    candidate_models: Sequence[str] | None = None,
) -> ModelWeights:
    """Weights proportional to 1/MAE over models with earned evidence.

    Scheme: ``w_i = (1/MAE_i) / sum_j (1/MAE_j)`` over the models whose matching
    score is meaningful AND beats persistence. Inverse MAE is deliberately the
    simplest defensible scheme: monotone in error, no tuning parameters, and
    easy to explain. It ignores correlation between models' errors (two models
    sharing a parent system are less independent than their weights imply) and
    compares MAEs that may come from different verification periods; both are
    reasons these weights are advisory input for a future, separately validated
    blend. A model whose MAE is exactly zero takes all the weight, shared
    equally among such models.

    ``candidate_models`` restricts and names the models of interest; candidates
    with no earned evidence get weight 0.0 when others have evidence. With no
    earned evidence at all the weights are EQUAL over the candidates (or over
    the models in the matching scores) and ``basis == "equal"``: equal weights
    state the absence of a preference and are not a recommendation to blend.

    These weights are not applied to anything in this phase. Blending
    unverified models would be an unjustified sophistication; the weights exist
    so that a later phase (and the UI) can show how the evidence is
    distributed once there is any.
    """
    _check_lead(lead_time_s)
    matches = _matching(scores, variable, lead_time_s)
    universe = sorted(set(candidate_models)) if candidate_models is not None else list(matches)
    earned = {
        k: s
        for k, s in matches.items()
        if k in universe
        and s.is_meaningful
        and _beats_persistence(s)
        and s.mean_absolute_error is not None
        and s.mean_absolute_error >= 0.0
    }

    if not earned:
        if not universe:
            return ModelWeights(
                variable=variable,
                lead_time_s=lead_time_s,
                weights={},
                basis="equal",
                note=f"No models and no evidence for {variable}: no weights to give.",
            )
        equal = _normalise(dict.fromkeys(universe, 1.0))
        why = (
            "no verification scores match"
            if not matches
            else "no matching score is both meaningful and better than persistence"
        )
        return ModelWeights(
            variable=variable,
            lead_time_s=lead_time_s,
            weights=equal,
            basis="equal",
            note=(
                f"EQUAL weights over {len(universe)} model(s): {why} for {variable} at lead "
                f"time {_hours(lead_time_s)}. Equal weights mean 'no evidence', not that the "
                "models are equally good, and are not a recommendation to blend."
            ),
        )

    zero = [k for k, s in earned.items() if s.mean_absolute_error == 0.0]
    raw = (
        dict.fromkeys(zero, 1.0)
        if zero
        else {k: 1.0 / float(s.mean_absolute_error or 0.0) for k, s in earned.items()}
    )
    normalised = _normalise(raw)
    full = {k: normalised.get(k, 0.0) for k in universe}
    unweighted = [k for k in universe if k not in normalised]
    note = (
        f"Inverse-MAE weights over {len(normalised)} model(s) with meaningful, "
        f"persistence-beating scores for {variable} at lead time {_hours(lead_time_s)}."
    )
    if unweighted:
        note += " Weight 0.0 (no earned evidence yet): " + ", ".join(unweighted) + "."
    note += " Advisory only: not applied to any forecast in this phase."
    return ModelWeights(
        variable=variable,
        lead_time_s=lead_time_s,
        weights=full,
        basis="inverse_mae",
        note=note,
    )
