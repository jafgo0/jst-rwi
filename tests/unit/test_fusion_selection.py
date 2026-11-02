"""Evidence rule for model preference, and the (unapplied) evidence weights. No network."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest

from rwi.contracts import VerificationScore
from rwi.core.provenance import DataKind, Provenance
from rwi.fusion import (
    MEANINGFUL_SAMPLE_COUNT,
    model_weights,
    preferred_model,
)

NOW = datetime(2027, 8, 20, tzinfo=UTC)
H = 3600.0


def score(
    model: str | None = "ecmwf_ifs",
    *,
    samples: int = 200,
    skill: float | None = 0.25,
    mae: float | None = 1.2,
    variable: str = "temperature_2m",
    bucket: tuple[float, float] = (0.0, 24 * H),
    provider: str = "open-meteo",
) -> VerificationScore:
    return VerificationScore(
        variable=variable,
        provider=provider,
        model_name=model,
        lead_time_bucket_s=bucket,
        sample_count=samples,
        mean_absolute_error=mae,
        root_mean_square_error=None if mae is None else mae * 1.3,
        persistence_mae=None if skill is None or mae is None else mae / (1.0 - skill),
        skill_vs_persistence=skill,
        unit="degC",
        provenance=Provenance(
            provider="rwi",
            dataset="verification",
            kind=DataKind.DERIVED,
            retrieved_at_utc=NOW,
        ),
    )


# --- THE EVIDENCE RULE --------------------------------------------------------------------


def test_no_scores_at_all_means_no_preference() -> None:
    name, reason = preferred_model([], variable="temperature_2m", lead_time_s=6 * H)
    assert name is None
    assert "no verification scores" in reason
    assert "no model has earned preference" in reason.lower()


def test_five_samples_earn_nothing_and_the_reason_says_how_many() -> None:
    name, reason = preferred_model(
        [score(samples=5, skill=0.9, mae=0.1)], variable="temperature_2m", lead_time_s=6 * H
    )
    assert name is None
    assert "5 sample" in reason
    assert "meaningful" in reason
    assert str(MEANINGFUL_SAMPLE_COUNT) in reason
    assert "ecmwf_ifs" in reason


def test_two_hundred_samples_but_worse_than_persistence_earn_nothing() -> None:
    name, reason = preferred_model(
        [score(samples=200, skill=-0.15)], variable="temperature_2m", lead_time_s=6 * H
    )
    assert name is None
    assert "persistence" in reason
    assert "-0.150" in reason
    assert "200 samples" in reason


def test_zero_skill_and_unknown_skill_do_not_beat_persistence() -> None:
    for skill in (0.0, None):
        name, reason = preferred_model(
            [score(skill=skill)], variable="temperature_2m", lead_time_s=6 * H
        )
        assert name is None
        assert "persistence" in reason
    assert (
        "unavailable"
        in preferred_model([score(skill=None)], variable="temperature_2m", lead_time_s=6 * H)[1]
    )


def test_two_hundred_samples_and_positive_skill_earn_preference() -> None:
    name, reason = preferred_model(
        [score(samples=200, skill=0.25)], variable="temperature_2m", lead_time_s=6 * H
    )
    assert name == "ecmwf_ifs"
    assert "ecmwf_ifs is preferred" in reason
    assert "200 verified samples" in reason
    assert "+0.250" in reason
    assert "persistence" in reason
    assert "temperature_2m" in reason


def test_the_meaningful_boundary_follows_the_contract() -> None:
    assert MEANINGFUL_SAMPLE_COUNT == 30
    assert not score(samples=MEANINGFUL_SAMPLE_COUNT - 1).is_meaningful
    assert score(samples=MEANINGFUL_SAMPLE_COUNT).is_meaningful
    assert preferred_model([score(samples=29)], variable="temperature_2m", lead_time_s=H)[0] is None
    assert preferred_model([score(samples=30)], variable="temperature_2m", lead_time_s=H)[0] == (
        "ecmwf_ifs"
    )


def test_best_mae_on_few_samples_never_beats_a_verified_model() -> None:
    scores = [
        score("lucky", samples=9, skill=0.9, mae=0.01),
        score("proven", samples=300, skill=0.1, mae=1.5),
    ]
    name, reason = preferred_model(scores, variable="temperature_2m", lead_time_s=6 * H)
    assert name == "proven"
    assert "lucky (9 samples" in reason
    assert "not meaningful" in reason
    assert "only model with earned evidence" in reason


def test_highest_skill_among_earned_wins_regardless_of_input_order_or_name() -> None:
    scores = [
        score("aaa", skill=0.10, mae=0.9),
        score("zzz", skill=0.30, mae=1.4),
        score("mmm", skill=0.20, mae=1.0),
    ]
    results = {
        preferred_model(list(p), variable="temperature_2m", lead_time_s=6 * H)[0]
        for p in itertools.permutations(scores)
    }
    assert results == {"zzz"}


def test_exact_tie_is_no_preference_not_alphabetical() -> None:
    scores = [score("aaa", skill=0.2, mae=1.0), score("bbb", skill=0.2, mae=1.0)]
    name, reason = preferred_model(scores, variable="temperature_2m", lead_time_s=6 * H)
    assert name is None
    assert "tied" in reason
    assert "aaa" in reason and "bbb" in reason


def test_scores_for_other_variables_or_lead_times_do_not_count() -> None:
    other_var = score(variable="ghi")
    name, reason = preferred_model([other_var], variable="temperature_2m", lead_time_s=6 * H)
    assert name is None
    assert "temperature_2m" in reason

    day_two = score(bucket=(24 * H, 48 * H))
    name, reason = preferred_model([day_two], variable="temperature_2m", lead_time_s=6 * H)
    assert name is None
    assert "lead time" in reason
    assert "[24 h, 48 h)" in reason


def test_lead_time_bucket_is_half_open() -> None:
    s = [score(bucket=(0.0, 24 * H))]
    assert preferred_model(s, variable="temperature_2m", lead_time_s=0.0)[0] == "ecmwf_ifs"
    assert preferred_model(s, variable="temperature_2m", lead_time_s=24 * H)[0] is None


def test_score_without_a_model_name_is_identified_by_provider() -> None:
    name, _ = preferred_model(
        [score(None, provider="open-meteo")], variable="temperature_2m", lead_time_s=H
    )
    assert name == "open-meteo"


def test_negative_lead_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="lead_time_s"):
        preferred_model([], variable="temperature_2m", lead_time_s=-1.0)


# --- WEIGHTS -------------------------------------------------------------------------------


def test_no_evidence_gives_equal_weights_with_an_explicit_note() -> None:
    w = model_weights(
        [score("a", samples=5), score("b", samples=5)], variable="temperature_2m", lead_time_s=H
    )
    assert w.basis == "equal"
    assert w.weights == {"a": 0.5, "b": 0.5}
    assert "EQUAL" in w.note
    assert "no evidence" in w.note
    assert "not a recommendation to blend" in w.note


def test_no_scores_but_candidates_given_gives_equal_weights_over_candidates() -> None:
    w = model_weights(
        [], variable="temperature_2m", lead_time_s=H, candidate_models=["x", "y", "z"]
    )
    assert w.basis == "equal"
    assert sum(w.weights.values()) == 1.0
    assert set(w.weights) == {"x", "y", "z"}
    assert "no verification scores match" in w.note


def test_nothing_at_all_gives_empty_weights() -> None:
    w = model_weights([], variable="temperature_2m", lead_time_s=H)
    assert w.weights == {}
    assert w.basis == "equal"


def test_models_that_do_not_beat_persistence_get_no_inverse_mae_weight() -> None:
    w = model_weights(
        [score("a", skill=-0.1), score("b", skill=-0.3)], variable="temperature_2m", lead_time_s=H
    )
    assert w.basis == "equal"
    assert "better than persistence" in w.note


def test_inverse_mae_weights_sum_to_exactly_one() -> None:
    scores = [score("a", mae=1.0), score("b", mae=2.0), score("c", mae=4.0)]
    w = model_weights(scores, variable="temperature_2m", lead_time_s=H)
    assert w.basis == "inverse_mae"
    assert sum(w.weights.values()) == 1.0
    total = 1.0 + 0.5 + 0.25
    assert w.weights["a"] == pytest.approx(1.0 / total)
    assert w.weights["b"] == pytest.approx(0.5 / total)
    assert w.weights["c"] == pytest.approx(0.25 / total)
    assert list(w.weights) == ["a", "b", "c"]
    assert "Advisory only" in w.note and "not applied" in w.note


@pytest.mark.parametrize("maes", [(0.7, 1.3, 2.9), (0.1, 0.1, 0.1), (3.3, 0.11, 7.7, 1.9, 0.5)])
def test_inverse_mae_weights_sum_exactly_to_one_for_awkward_numbers(
    maes: tuple[float, ...],
) -> None:
    scores = [score(f"m{i}", mae=m) for i, m in enumerate(maes)]
    w = model_weights(scores, variable="temperature_2m", lead_time_s=H)
    assert sum(w.weights.values()) == 1.0


def test_unverified_candidates_get_zero_weight_when_others_have_evidence() -> None:
    scores = [score("a", mae=1.0), score("b", samples=4, mae=0.1)]
    w = model_weights(
        scores, variable="temperature_2m", lead_time_s=H, candidate_models=["a", "b", "c"]
    )
    assert w.weights == {"a": 1.0, "b": 0.0, "c": 0.0}
    assert "b, c" in w.note


def test_zero_mae_model_takes_the_weight_without_dividing_by_zero() -> None:
    w = model_weights(
        [score("a", mae=0.0), score("b", mae=1.0)], variable="temperature_2m", lead_time_s=H
    )
    assert w.weights == {"a": 1.0, "b": 0.0}


def test_weights_do_not_depend_on_score_order() -> None:
    scores = [score("a", mae=1.0), score("b", mae=2.0), score("c", mae=3.0)]
    ref = model_weights(scores, variable="temperature_2m", lead_time_s=H)
    for p in itertools.permutations(scores):
        assert model_weights(list(p), variable="temperature_2m", lead_time_s=H) == ref
