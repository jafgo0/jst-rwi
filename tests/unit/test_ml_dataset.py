"""rwi.ml.dataset: leak-free temporal split and feature/target construction."""

from datetime import timedelta

import numpy as np
import pytest
from ml_synth import START, ghi_pairs, wind_pairs

from rwi.ml.constraints import K_MAX, MIN_TRAIN_ELEVATION_DEG
from rwi.ml.dataset import dataset_from_pairs, split_by_cutoff
from rwi.ml.features import FEATURE_NAMES


def test_irradiance_rows_drop_low_sun_and_target_is_clipped_clear_sky_index() -> None:
    ds = dataset_from_pairs(ghi_pairs(5), variable="ghi")
    assert len(ds) > 0
    assert ds.features.shape == (len(ds), len(FEATURE_NAMES))
    assert np.all(ds.elevation_deg >= MIN_TRAIN_ELEVATION_DEG)
    assert np.all((ds.target >= 0.0) & (ds.target <= K_MAX))
    np.testing.assert_allclose(ds.target, np.clip(ds.observed / ds.clear_sky, 0.0, K_MAX))


def test_non_irradiance_target_is_the_physical_value_and_keeps_night_rows() -> None:
    ds = dataset_from_pairs(wind_pairs(2), variable="wind_speed_10m")
    assert len(ds) == 48
    np.testing.assert_array_equal(ds.target, ds.observed)
    assert np.all(np.isnan(ds.clear_sky))


def test_other_variables_and_unknown_variable() -> None:
    assert len(dataset_from_pairs(wind_pairs(1), variable="ghi")) == 0
    with pytest.raises(ValueError):
        dataset_from_pairs([], variable="wind_direction_10m")  # direction: via u/v only


def test_split_never_trains_on_anything_unknown_at_the_cutoff() -> None:
    ds = dataset_from_pairs(ghi_pairs(10), variable="ghi")
    cutoff = START + timedelta(days=5, hours=6)
    split = split_by_cutoff(ds, cutoff)
    assert len(split.train) and len(split.holdout)
    assert all(t <= cutoff for t in split.train.issued_at_utc)
    assert all(t <= cutoff for t in split.train.valid_time_utc)
    assert all(t > cutoff for t in split.holdout.issued_at_utc)
    # The day-5 run was issued before the cutoff but verifies after it: the gap.
    assert len(split.train) + len(split.holdout) < len(ds)
    assert split.training_cutoff_utc is not None and split.training_cutoff_utc <= cutoff


def test_split_with_nothing_before_the_cutoff() -> None:
    ds = dataset_from_pairs(ghi_pairs(2), variable="ghi")
    split = split_by_cutoff(ds, START - timedelta(days=1))
    assert len(split.train) == 0 and split.training_cutoff_utc is None
    assert len(split.holdout) == len(ds)


def test_fixed_vocabulary_drops_unknown_sources() -> None:
    pairs = ghi_pairs(2) + ghi_pairs(2, provider="bom", model_name=None, seed=9)
    ds = dataset_from_pairs(pairs, variable="ghi", source_vocabulary=["bom/-"])
    assert set(ds.sources) == {"bom/-"}
    assert np.all(ds.features[:, 0] == 0)
