"""Verification metrics: exact arithmetic, circular handling, buckets, skill, determinism."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from rwi.contracts import ForecastObservationPair
from rwi.core.errors import UnknownVariableError
from rwi.core.geo import GeoPoint
from rwi.verification import (
    DEFAULT_LEAD_TIME_BUCKETS_S,
    circular_difference,
    circular_mean,
    is_circular_variable,
    persistence_baseline,
    score_pairs,
    skill_score,
)

T0 = datetime(2027, 10, 20, 0, 0, tzinfo=UTC)
NOW = datetime(2027, 10, 25, 0, 0, tzinfo=UTC)
STATION = GeoPoint(latitude_deg=-12.4, longitude_deg=130.9)
H = 3600.0


def make_pair(
    forecast: float,
    observed: float,
    *,
    variable: str = "temperature_2m",
    lead_h: float = 1.0,
    valid: datetime = T0,
    provider: str = "open-meteo",
    model: str | None = "ecmwf_ifs025",
    station: GeoPoint = STATION,
    offset_s: float = 0.0,
) -> ForecastObservationPair:
    return ForecastObservationPair(
        variable=variable,
        valid_time_utc=valid,
        forecast_value=forecast,
        observed_value=observed,
        lead_time_s=lead_h * H,
        provider=provider,
        model_name=model,
        forecast_point=station,
        observation_point=station,
        separation_m=0.0,
        time_offset_s=offset_s,
        forecast_recorded_at_utc=valid - timedelta(seconds=lead_h * H),
    )


def pairs_with_errors(errors: list[float], *, lead_h: float = 1.0) -> list[ForecastObservationPair]:
    return [
        make_pair(20.0 + e, 20.0, lead_h=lead_h, valid=T0 + timedelta(hours=7 * i))
        for i, e in enumerate(errors)
    ]


def alternating_series(
    n: int, error: float, *, variable: str = "temperature_2m"
) -> list[ForecastObservationPair]:
    """Hourly obs alternating 10, 12 (persistence error always 2), forecast = obs + error."""
    out = []
    for k in range(n):
        obs = 10.0 + 2.0 * (k % 2)
        out.append(
            make_pair(
                obs + error, obs, variable=variable, lead_h=1.0, valid=T0 + timedelta(hours=k)
            )
        )
    return out


# --------------------------------------------------------------------------- exact arithmetic


def test_exact_mae_rmse_bias() -> None:
    pairs = pairs_with_errors([2.0, -2.0, 4.0, -4.0])
    (score,) = score_pairs(pairs, variable="temperature_2m", unit="degC", now=NOW)
    assert score.sample_count == 4
    assert score.mean_absolute_error == 3.0
    assert score.root_mean_square_error == math.sqrt(10)
    assert score.bias == 0.0
    assert score.mean_forecast == 20.0
    assert score.mean_observed == 20.0
    assert score.unit == "degC"


def test_bias_sign_forecast_too_warm_is_positive_and_correction_negative() -> None:
    from rwi.verification import bias_correction

    pairs = [make_pair(21.5, 20.0, lead_h=8.0, valid=T0 + timedelta(hours=i)) for i in range(40)]
    (score,) = score_pairs(pairs, variable="temperature_2m", unit="degC", now=NOW)
    assert score.bias == 1.5
    assert score.mean_forecast is not None and score.mean_observed is not None
    assert score.mean_forecast - score.mean_observed == pytest.approx(1.5)
    assert (
        bias_correction(
            [score],
            variable="temperature_2m",
            provider="open-meteo",
            model_name="ecmwf_ifs025",
            lead_time_s=8 * H,
        )
        == -1.5
    )


def test_period_is_first_to_last_valid_time() -> None:
    pairs = pairs_with_errors([1.0, 2.0, 3.0])
    (score,) = score_pairs(pairs, variable="temperature_2m", unit="degC", now=NOW)
    assert score.period is not None
    assert score.period.start_utc == T0
    assert score.period.end_utc == T0 + timedelta(hours=14)


def test_provenance_marks_score_as_derived_and_uses_now() -> None:
    (score,) = score_pairs(
        pairs_with_errors([1.0, 1.0]), variable="temperature_2m", unit="degC", now=NOW
    )
    assert score.provenance.provider == "rwi"
    assert score.provenance.kind.value == "derived"
    assert score.provenance.retrieved_at_utc == NOW


def test_wrong_unit_and_unknown_variable_rejected() -> None:
    with pytest.raises(ValueError, match="degC"):
        score_pairs([], variable="temperature_2m", unit="K")
    with pytest.raises(UnknownVariableError):
        score_pairs([], variable="not_a_variable", unit="degC")


def test_pairs_of_other_variables_are_ignored() -> None:
    stray = make_pair(500.0, 400.0, variable="ghi", valid=T0 + timedelta(hours=99))
    pairs = [*pairs_with_errors([1.0, 1.0]), stray]
    (score,) = score_pairs(pairs, variable="temperature_2m", unit="degC", now=NOW)
    assert score.sample_count == 2


def test_separate_scores_per_provider_and_model() -> None:
    pairs = [
        make_pair(21.0, 20.0, model="a"),
        make_pair(23.0, 20.0, model="b", valid=T0 + timedelta(hours=1)),
        make_pair(22.0, 20.0, provider="bom", model=None, valid=T0 + timedelta(hours=2)),
    ]
    scores = score_pairs(pairs, variable="temperature_2m", unit="degC", now=NOW)
    assert [(s.provider, s.model_name, s.bias) for s in scores] == [
        ("bom", None, 2.0),
        ("open-meteo", "a", 1.0),
        ("open-meteo", "b", 3.0),
    ]


# --------------------------------------------------------------------------- circular


@pytest.mark.science
def test_wind_direction_is_circular_in_registry() -> None:
    assert is_circular_variable("wind_direction_10m") is True
    assert is_circular_variable("wind_speed_10m") is False
    assert is_circular_variable("temperature_2m") is False


@pytest.mark.science
def test_circular_355_vs_5_is_10_not_350() -> None:
    # Forecast 355 deg, observed 5 deg: shortest arc is 10 deg (forecast is 10 deg anticlockwise).
    pair = make_pair(355.0, 5.0, variable="wind_direction_10m")
    (score,) = score_pairs([pair], variable="wind_direction_10m", unit="deg", now=NOW)
    assert score.mean_absolute_error == 10.0
    assert score.root_mean_square_error == 10.0
    assert score.bias == -10.0
    # The linear (wrong) answer would have been 350.
    assert pair.error == 350.0
    assert circular_difference(355.0, 5.0) == -10.0


@pytest.mark.science
def test_circular_reverse_5_vs_355_is_10() -> None:
    pair = make_pair(5.0, 355.0, variable="wind_direction_10m")
    (score,) = score_pairs([pair], variable="wind_direction_10m", unit="deg", now=NOW)
    assert score.mean_absolute_error == 10.0
    assert score.bias == 10.0
    assert circular_difference(5.0, 355.0) == 10.0


@pytest.mark.science
def test_circular_difference_wraps_into_half_open_interval() -> None:
    assert circular_difference(180.0, 0.0) == 180.0  # +180, not -180: interval is (-180, 180]
    assert circular_difference(0.0, 180.0) == 180.0
    assert circular_difference(190.0, 0.0) == -170.0
    assert circular_difference(0.0, 360.0) == 0.0
    assert circular_difference(10.0, 350.0) == 20.0


@pytest.mark.science
def test_circular_mean_across_wrap_is_vector_mean_not_arithmetic() -> None:
    forecasts = [350.0, 10.0]
    observed = [355.0, 5.0]
    pairs = [
        make_pair(f, o, variable="wind_direction_10m", valid=T0 + timedelta(hours=i))
        for i, (f, o) in enumerate(zip(forecasts, observed, strict=True))
    ]
    (score,) = score_pairs(pairs, variable="wind_direction_10m", unit="deg", now=NOW)
    # Arithmetic means would be 180 and 180 - the opposite side of the compass.
    assert sum(forecasts) / 2 == 180.0
    assert score.mean_forecast is not None and score.mean_observed is not None
    assert abs(circular_difference(score.mean_forecast, 0.0)) < 1e-9
    assert abs(circular_difference(score.mean_observed, 0.0)) < 1e-9
    assert 0.0 <= score.mean_forecast < 360.0
    assert score.mean_absolute_error == 5.0
    assert score.bias == 0.0


@pytest.mark.science
def test_circular_mean_values() -> None:
    assert circular_mean([350.0, 10.0]) == pytest.approx(0.0, abs=1e-9)
    mean = circular_mean([340.0, 20.0, 0.0])
    assert mean is not None and abs(circular_difference(mean, 0.0)) < 1e-9
    mean = circular_mean([10.0, 20.0, 30.0])
    assert mean == pytest.approx(20.0)
    mean = circular_mean([350.0, 355.0, 5.0, 10.0])
    assert mean is not None and abs(circular_difference(mean, 0.0)) < 1e-9
    # Mean direction of opposite winds does not exist; say so rather than invent one.
    assert circular_mean([0.0, 180.0]) is None
    assert circular_mean([]) is None


@pytest.mark.science
def test_circular_mean_just_west_of_north_is_not_zero_or_180() -> None:
    mean = circular_mean([350.0, 352.0, 358.0])
    assert mean is not None
    assert 350.0 < mean < 360.0


@pytest.mark.science
def test_circular_persistence_uses_shortest_arc() -> None:
    # Observed direction jitters 355 / 5: persistence error is 10, never 350.
    pairs = []
    for k in range(10):
        obs = 355.0 if k % 2 == 0 else 5.0
        pairs.append(
            make_pair(obs, obs, variable="wind_direction_10m", valid=T0 + timedelta(hours=k))
        )
    assert persistence_baseline(pairs) == 10.0
    (score,) = score_pairs(pairs, variable="wind_direction_10m", unit="deg", now=NOW)
    assert score.persistence_mae == 10.0
    assert score.mean_absolute_error == 0.0
    assert score.skill_vs_persistence == 1.0


# --------------------------------------------------------------------------- buckets


def test_default_buckets_are_documented_set() -> None:
    assert tuple((lo / H, hi / H) for lo, hi in DEFAULT_LEAD_TIME_BUCKETS_S) == (
        (0, 3),
        (3, 6),
        (6, 12),
        (12, 24),
        (24, 48),
        (48, 120),
    )


def test_lead_time_bucketing_one_pair_each_in_three_buckets() -> None:
    pairs = [
        make_pair(21.0, 20.0, lead_h=1.0, valid=T0),
        make_pair(21.0, 20.0, lead_h=5.0, valid=T0 + timedelta(hours=1)),
        make_pair(21.0, 20.0, lead_h=5.0, valid=T0 + timedelta(hours=2)),
        make_pair(21.0, 20.0, lead_h=30.0, valid=T0 + timedelta(hours=3)),
        make_pair(21.0, 20.0, lead_h=30.0, valid=T0 + timedelta(hours=4)),
        make_pair(21.0, 20.0, lead_h=30.0, valid=T0 + timedelta(hours=5)),
    ]
    scores = score_pairs(pairs, variable="temperature_2m", unit="degC", now=NOW)
    assert [(s.lead_time_bucket_s, s.sample_count) for s in scores] == [
        ((0.0, 3 * H), 1),
        ((3 * H, 6 * H), 2),
        ((24 * H, 48 * H), 3),
    ]


def test_bucket_boundary_belongs_to_upper_bucket() -> None:
    # Half-open [lower, upper): exactly 3 h is in 3-6 h, not 0-3 h.
    pairs = [
        make_pair(21.0, 20.0, lead_h=3.0, valid=T0),
        make_pair(21.0, 20.0, lead_h=6.0, valid=T0 + timedelta(hours=1)),
        make_pair(21.0, 20.0, lead_h=0.0, valid=T0 + timedelta(hours=2)),
    ]
    scores = score_pairs(pairs, variable="temperature_2m", unit="degC", now=NOW)
    assert [(s.lead_time_bucket_s, s.sample_count) for s in scores] == [
        ((0.0, 3 * H), 1),
        ((3 * H, 6 * H), 1),
        ((6 * H, 12 * H), 1),
    ]


def test_lead_beyond_last_bucket_is_not_scored() -> None:
    pairs = [make_pair(21.0, 20.0, lead_h=120.0), make_pair(21.0, 20.0, lead_h=200.0)]
    assert score_pairs(pairs, variable="temperature_2m", unit="degC", now=NOW) == []


def test_custom_buckets_and_validation() -> None:
    pairs = [make_pair(21.0, 20.0, lead_h=10.0)]
    (score,) = score_pairs(
        pairs,
        variable="temperature_2m",
        unit="degC",
        lead_time_buckets_s=[(0.0, 100 * H)],
        now=NOW,
    )
    assert score.lead_time_bucket_s == (0.0, 100 * H)
    with pytest.raises(ValueError, match="overlap"):
        score_pairs(
            pairs,
            variable="temperature_2m",
            unit="degC",
            lead_time_buckets_s=[(0.0, 10.0), (5.0, 20.0)],
        )
    with pytest.raises(ValueError, match="invalid"):
        score_pairs(
            pairs, variable="temperature_2m", unit="degC", lead_time_buckets_s=[(10.0, 10.0)]
        )


# --------------------------------------------------------------------------- skill


@pytest.mark.parametrize(
    ("error", "expected_skill"),
    [(2.0, 0.0), (-2.0, 0.0), (1.0, 0.5), (-1.0, 0.5), (4.0, -1.0), (0.0, 1.0)],
)
def test_skill_vs_persistence(error: float, expected_skill: float) -> None:
    pairs = alternating_series(12, error)
    (score,) = score_pairs(pairs, variable="temperature_2m", unit="degC", now=NOW)
    assert score.persistence_mae == 2.0
    assert score.skill_vs_persistence == expected_skill
    assert persistence_baseline(pairs) == 2.0


def test_skill_score_function_conventions() -> None:
    assert skill_score(1.0, 2.0) == 0.5
    assert skill_score(2.0, 2.0) == 0.0
    assert skill_score(4.0, 2.0) == -1.0
    assert skill_score(1.0, 0.0) is None
    assert skill_score(None, 2.0) is None
    assert skill_score(1.0, None) is None


def test_persistence_mae_zero_gives_none_skill_not_infinity() -> None:
    pairs = [
        make_pair(21.0, 20.0, valid=T0 + timedelta(hours=k)) for k in range(10)
    ]  # observations never change
    assert persistence_baseline(pairs) == 0.0
    (score,) = score_pairs(pairs, variable="temperature_2m", unit="degC", now=NOW)
    assert score.persistence_mae == 0.0
    assert score.skill_vs_persistence is None


def test_persistence_none_without_reference_observations() -> None:
    # Pairs 7 h apart with 1 h lead: no observation exists near any init time.
    pairs = pairs_with_errors([1.0, 2.0, 3.0, 4.0])
    assert persistence_baseline(pairs) is None
    (score,) = score_pairs(pairs, variable="temperature_2m", unit="degC", now=NOW)
    assert score.persistence_mae is None
    assert score.skill_vs_persistence is None
    assert score.mean_absolute_error == 2.5  # the plain scores are still reported


def test_persistence_does_not_use_the_pairs_own_observation_at_zero_lead() -> None:
    pairs = [make_pair(21.0, 20.0 + k, lead_h=0.0, valid=T0 + timedelta(hours=k)) for k in range(6)]
    assert persistence_baseline(pairs) is None


def test_persistence_from_explicit_history() -> None:
    # Only two forecasts; the observation series (one station) supplies the references.
    pairs = [
        make_pair(15.0, 14.0, lead_h=6.0, valid=T0 + timedelta(hours=6)),
        make_pair(16.0, 17.0, lead_h=6.0, valid=T0 + timedelta(hours=7)),
    ]
    history = [(T0, 10.0), (T0 + timedelta(hours=1), 12.0)]
    # persistence forecasts 10 and 12 against obs 14 and 17 -> MAE (4 + 5) / 2
    assert persistence_baseline(pairs, observation_history=history) == 4.5
    (score,) = score_pairs(
        pairs,
        variable="temperature_2m",
        unit="degC",
        observation_history=history,
        now=NOW,
    )
    assert score.persistence_mae == 4.5
    assert score.mean_absolute_error == 1.0
    assert score.skill_vs_persistence == 1.0 - 1.0 / 4.5


def test_persistence_stations_are_not_mixed() -> None:
    other = GeoPoint(latitude_deg=-20.0, longitude_deg=134.0)
    pairs = [
        make_pair(
            10.0, 10.0, valid=T0 + timedelta(hours=k), station=STATION if k % 2 == 0 else other
        )
        for k in range(8)
    ]
    # Each station has an observation only every 2 h, but leads are 1 h: no reference.
    assert persistence_baseline(pairs) is None


def test_persistence_rejects_mixed_variables() -> None:
    with pytest.raises(ValueError, match="single variable"):
        persistence_baseline([make_pair(1.0, 1.0), make_pair(1.0, 1.0, variable="ghi")])


def test_persistence_empty_is_none() -> None:
    assert persistence_baseline([]) is None


# --------------------------------------------------------------------------- degenerate


def test_zero_pairs_gives_no_scores() -> None:
    assert score_pairs([], variable="temperature_2m", unit="degC", now=NOW) == []


def test_single_pair() -> None:
    (score,) = score_pairs([make_pair(21.0, 20.0)], variable="temperature_2m", unit="degC", now=NOW)
    assert score.sample_count == 1
    assert score.mean_absolute_error == 1.0
    assert score.persistence_mae is None
    assert score.skill_vs_persistence is None
    assert score.is_meaningful is False


def test_non_finite_pairs_are_ignored() -> None:
    pairs = [make_pair(21.0, 20.0), make_pair(float("nan"), 20.0, valid=T0 + timedelta(hours=1))]
    (score,) = score_pairs(pairs, variable="temperature_2m", unit="degC", now=NOW)
    assert score.sample_count == 1
    assert score.mean_absolute_error == 1.0


# --------------------------------------------------------------------------- meaningful


def test_is_meaningful_threshold_follows_sample_count() -> None:
    (thin,) = score_pairs(
        alternating_series(5, 1.0), variable="temperature_2m", unit="degC", now=NOW
    )
    (thick,) = score_pairs(
        alternating_series(50, 1.0), variable="temperature_2m", unit="degC", now=NOW
    )
    assert thin.sample_count == 5 and thin.is_meaningful is False
    assert thick.sample_count == 50 and thick.is_meaningful is True


# --------------------------------------------------------------------------- determinism


def test_scores_independent_of_input_order() -> None:
    rng = random.Random(1234)
    pairs = []
    for i in range(80):
        lead = rng.choice([1.0, 4.0, 9.0, 20.0, 40.0])
        model = rng.choice(["a", "b"])
        obs = 15.0 + rng.uniform(-5, 5)
        pairs.append(
            make_pair(
                obs + rng.uniform(-3, 3),
                obs,
                lead_h=lead,
                model=model,
                valid=T0 + timedelta(hours=i // 2),
            )
        )
    baseline = score_pairs(pairs, variable="temperature_2m", unit="degC", now=NOW)
    for seed in range(5):
        shuffled = pairs[:]
        random.Random(seed).shuffle(shuffled)
        again = score_pairs(shuffled, variable="temperature_2m", unit="degC", now=NOW)
        assert again == baseline
    assert baseline  # something was scored


def test_circular_scores_independent_of_input_order() -> None:
    rng = random.Random(7)
    pairs = [
        make_pair(
            rng.uniform(0, 360),
            rng.uniform(0, 360),
            variable="wind_direction_10m",
            valid=T0 + timedelta(hours=i),
        )
        for i in range(40)
    ]
    a = score_pairs(pairs, variable="wind_direction_10m", unit="deg", now=NOW)
    shuffled = pairs[:]
    random.Random(3).shuffle(shuffled)
    b = score_pairs(shuffled, variable="wind_direction_10m", unit="deg", now=NOW)
    assert a == b
