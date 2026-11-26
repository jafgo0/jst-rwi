"""CRPS and pinball loss in rwi.verification.metrics."""

import math
from statistics import NormalDist

import pytest

from rwi.verification.metrics import crps_ensemble, crps_quantiles, pinball_loss


def test_pinball_loss_is_asymmetric() -> None:
    assert pinball_loss(10.0, 8.0, 0.9) == pytest.approx(1.8)
    assert pinball_loss(8.0, 10.0, 0.9) == pytest.approx(0.2)
    with pytest.raises(ValueError):
        pinball_loss(1.0, 1.0, 1.0)


def test_point_forecast_crps_is_absolute_error() -> None:
    assert crps_quantiles(3.0, [5.0, 5.0, 5.0], [0.1, 0.5, 0.9]) == pytest.approx(2.0)
    assert crps_ensemble(3.0, [5.0]) == pytest.approx(2.0)
    with pytest.raises(ValueError):
        crps_quantiles(3.0, [5.0], [0.1, 0.5])
    with pytest.raises(ValueError):
        crps_ensemble(3.0, [])


def test_ensemble_energy_form_matches_brute_force() -> None:
    members = [1.0, 4.0, 2.5, 7.0]
    y = 3.0
    n = len(members)
    brute = sum(abs(x - y) for x in members) / n - 0.5 * sum(
        abs(a - b) for a in members for b in members
    ) / n**2
    assert crps_ensemble(y, members) == pytest.approx(brute)


@pytest.mark.science
def test_crps_of_standard_normal_at_zero() -> None:
    # Gneiting & Raftery (2007), JASA 102: CRPS(N(0,1), y) = y(2Phi(y)-1) + 2phi(y) - 1/sqrt(pi);
    # at y = 0 that is 2/sqrt(2 pi) - 1/sqrt(pi) = 0.233695...
    exact = 2.0 / math.sqrt(2.0 * math.pi) - 1.0 / math.sqrt(math.pi)
    k = 999
    levels = [(i + 0.5) / k for i in range(k)]
    quantiles = [NormalDist().inv_cdf(t) for t in levels]
    assert crps_quantiles(0.0, quantiles, levels) == pytest.approx(exact, abs=1e-3)
    assert crps_ensemble(0.0, quantiles) == pytest.approx(exact, abs=1e-3)
