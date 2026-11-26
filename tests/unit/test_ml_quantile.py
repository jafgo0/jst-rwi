"""rwi.ml.quantile + rwi.ml.scoring on synthetic, learnable data."""

from datetime import timedelta

import numpy as np
import pytest
from ml_synth import START, ghi_pairs, require_lightgbm, wind_pairs

from rwi.ml.dataset import dataset_from_pairs, split_by_cutoff
from rwi.ml.quantile import MIN_TRAIN_ROWS, MlUnavailableError, QuantileModel, train_quantile_model
from rwi.ml.scoring import ALL, coverage_80, score_holdout, summary_metrics

require_lightgbm()

CUTOFF = START + timedelta(days=24)


@pytest.fixture(scope="module")
def ghi_split():
    ds = dataset_from_pairs(ghi_pairs(30), variable="ghi")
    return split_by_cutoff(ds, CUTOFF)


@pytest.fixture(scope="module")
def ghi_model(ghi_split) -> QuantileModel:
    return train_quantile_model(ghi_split.train, num_boost_round=60)


def test_refuses_to_fit_on_too_few_rows() -> None:
    ds = dataset_from_pairs(ghi_pairs(1), variable="ghi")
    assert len(ds) < MIN_TRAIN_ROWS
    with pytest.raises(MlUnavailableError):
        train_quantile_model(ds)


def test_quantiles_are_ordered_non_negative_and_bounded(ghi_model, ghi_split) -> None:
    h = ghi_split.holdout
    q = ghi_model.predict(h.features, clear_sky=h.clear_sky, elevation_deg=h.elevation_deg)
    assert q.shape == (len(h), 3)
    assert np.all(np.diff(q, axis=1) >= 0.0)
    assert np.all(q >= 0.0)
    assert np.all(q <= 1.5 * h.clear_sky[:, None] + 1e-9)


def test_training_is_deterministic(ghi_split) -> None:
    a = train_quantile_model(ghi_split.train, num_boost_round=20).predict_raw(
        ghi_split.holdout.features
    )
    b = train_quantile_model(ghi_split.train, num_boost_round=20).predict_raw(
        ghi_split.holdout.features
    )
    np.testing.assert_array_equal(a, b)


def test_envelope_rejects_out_of_range_and_unknown_sources(ghi_model, ghi_split) -> None:
    x = ghi_split.train.features[:3].copy()  # training rows are in range by construction
    assert ghi_model.in_range(x).all()
    x[0, 1] = 1e6  # forecast value far outside training
    x[1, 0] = 7  # source code not in the vocabulary
    x[2, 9] = x[2, 9] + 5.0  # a longitude never seen
    assert not ghi_model.in_range(x).any()


def test_ml_beats_a_biased_raw_forecast_on_the_holdout(ghi_model, ghi_split) -> None:
    rows = score_holdout(ghi_model, ghi_split.holdout)
    ml = next(r for r in rows if r.system == "ml" and r.group_kind == ALL)
    raw = next(r for r in rows if r.system != "ml" and r.group_kind == ALL)
    assert ml.n == raw.n > 0
    assert ml.crps is not None and raw.crps is not None
    assert ml.crps < raw.crps
    # A point forecast scored as three equal quantiles: CRPS == MAE.
    assert raw.crps == pytest.approx(raw.mae)
    assert any(r.group_kind == "lead_time" for r in rows)
    m = summary_metrics(rows)
    assert m["crps"] == ml.crps and m["best_raw_crps"] == raw.crps


def test_wind_is_learned_without_clear_sky() -> None:
    ds = dataset_from_pairs(wind_pairs(20), variable="wind_speed_10m")
    split = split_by_cutoff(ds, START + timedelta(days=15))
    model = train_quantile_model(split.train, num_boost_round=60)
    h = split.holdout
    q = model.predict(h.features, clear_sky=h.clear_sky, elevation_deg=h.elevation_deg)
    assert np.all(q >= 0.0)
    assert np.mean(np.abs(q[:, 1] - h.observed)) < np.mean(np.abs(h.forecast - h.observed))


def test_coverage_80() -> None:
    q = np.array([[0.0, 1.0, 2.0]] * 4)
    assert coverage_80(np.array([0.5, 1.0, 3.0, -1.0]), q) == 0.5
    assert coverage_80(np.zeros(0), np.zeros((0, 3))) is None
