"""rwi.ml.constraints: physical post-conditions on quantile output."""

import numpy as np
import pytest

from rwi.core.variables import spec_for
from rwi.ml.constraints import K_MAX, clip_to_spec, irradiance_from_k, sort_quantiles


def test_crossing_quantiles_are_sorted() -> None:
    q = np.array([[3.0, 1.0, 2.0], [0.0, 0.5, 0.4]])
    np.testing.assert_array_equal(sort_quantiles(q), [[1.0, 2.0, 3.0], [0.0, 0.4, 0.5]])


@pytest.mark.science
def test_direct_irradiance_is_exactly_zero_below_the_horizon() -> None:
    # Hard rule 5: sun below horizon => exactly 0.0, whatever the model said.
    k = np.array([[0.9, 1.0, 1.1], [0.9, 1.0, 1.1], [0.9, 1.0, 1.1]])
    out = irradiance_from_k(k, np.array([800.0, 5.0, 0.0]), np.array([40.0, -0.1, -20.0]))
    assert np.all(out[1:] == 0.0)
    assert np.all(out[0] > 0.0)


@pytest.mark.science
def test_k_max_caps_irradiance_at_the_cloud_enhancement_bound() -> None:
    # Gueymard (2017), Renew. Sust. Energy Rev. 74: 1-min cloud enhancement of GHI
    # rarely exceeds ~1.5x clear sky. K_MAX is that bound, not a typical value.
    assert K_MAX == 1.5
    out = irradiance_from_k(np.array([[-0.5, 2.0, 9.0]]), np.array([1000.0]), np.array([60.0]))
    np.testing.assert_allclose(out, [[0.0, 1500.0, 1500.0]])


def test_clip_to_spec_uses_the_canonical_range() -> None:
    spec = spec_for("cloud_cover")
    out = clip_to_spec("cloud_cover", np.array([[-10.0, 50.0, 400.0]]))
    assert out[0, 0] == spec.minimum and out[0, 2] == spec.maximum
