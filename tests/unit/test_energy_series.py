"""Series, energy integration, and error-metric tests."""

from __future__ import annotations

import math
from itertools import pairwise

import pytest
from test_energy_support import T0, at, flat_poa, irradiance_for, make_array, solar_at

from rwi.contracts import PowerEstimate
from rwi.core.errors import ModelConfigurationError
from rwi.energy.array import array_power
from rwi.energy.series import (
    PowerSample,
    compare_with_measured,
    energy_along_trajectory,
    integrate_energy,
    power_series,
)


def estimate_with_power(net_w: float, hours: float) -> PowerEstimate:
    """A PowerEstimate whose net power is prescribed via POA (eta chain = 0.24*0.98*0.98)."""
    arr = make_array(temperature_coefficient_pmax_per_c=0.0)
    g = net_w / (arr.area_m2 * 0.24 * 0.98 * 0.98) if net_w > 0 else 0.0
    return array_power(
        flat_poa(g, at(hours)), arr, air_temperature_c=25.0, wind_speed_mps=1.0,
        cell_temperature_override_c=25.0,
    )


def test_constant_500_w_for_2_h_is_1000_wh() -> None:
    est = [estimate_with_power(500.0, h) for h in (0.0, 1.0, 2.0)]
    assert est[0].net_power_w == pytest.approx(500.0, rel=1e-12)
    e = integrate_energy(est)
    assert e.energy_wh == pytest.approx(1000.0, rel=1e-12)
    assert e.sample_count == 3
    assert e.window.start_utc == T0 and e.window.end_utc == at(2.0)
    assert e.integration_method == "trapezoidal"


def test_triangular_profile_matches_analytic_trapezoid() -> None:
    # 0 -> 800 W over 2 h then 800 -> 0 over 2 h: area = 0.5*4 h*800 W = 1600 Wh.
    powers = [(0.0, 0.0), (1.0, 400.0), (2.0, 800.0), (3.0, 400.0), (4.0, 0.0)]
    est = [estimate_with_power(p, h) for h, p in powers]
    assert integrate_energy(est).energy_wh == pytest.approx(1600.0, rel=1e-9)
    cum = energy_along_trajectory(est)
    assert cum[0] == 0.0
    assert cum[2] == pytest.approx(0.5 * 2.0 * 800.0, rel=1e-9)  # 800 Wh after the ramp
    assert cum[-1] == pytest.approx(1600.0, rel=1e-9)
    assert all(b >= a for a, b in pairwise(cum))


def test_single_sample_is_zero_energy() -> None:
    e = integrate_energy([estimate_with_power(500.0, 0.0)])
    assert e.energy_wh == 0.0 and e.sample_count == 1
    assert e.window.start_utc == e.window.end_utc


def test_empty_and_non_monotonic_raise() -> None:
    with pytest.raises(ValueError):
        integrate_energy([])
    est = [estimate_with_power(500.0, 1.0), estimate_with_power(500.0, 0.0)]
    with pytest.raises(ValueError, match="non-decreasing"):
        integrate_energy(est)
    with pytest.raises(ValueError):
        energy_along_trajectory(est)


def test_power_series_end_to_end_day_profile() -> None:
    samples = []
    for h, elev in ((0.0, -5.0), (1.0, 20.0), (2.0, 50.0), (3.0, 20.0), (4.0, -5.0)):
        s = solar_at(elev, azimuth_deg=0.0, when=at(h))
        dni, dhi = (0.0, 0.0) if elev <= 0 else (700.0, 90.0)
        samples.append(PowerSample(
            solar=s, irradiance=irradiance_for(s, dni, dhi), air_temperature_c=28.0,
            wind_speed_mps=3.0,
        ))
    arr = make_array()
    out = power_series(samples, arr)
    assert [e.net_power_w == 0.0 for e in out] == [True, False, False, False, True]
    assert out[2].net_power_w > out[1].net_power_w
    assert integrate_energy(out).energy_wh > 0.0
    # tuple form gives identical numbers
    tup = [(p.time_utc, p.solar, p.irradiance, 28.0, 3.0) for p in samples]
    assert [e.net_power_w for e in power_series(tup, arr)] == [e.net_power_w for e in out]


def test_power_series_vehicle_relative_needs_heading() -> None:
    s = solar_at(40.0)
    sample = PowerSample(solar=s, irradiance=irradiance_for(s, 700.0, 90.0))
    arr = make_array(mounting="vehicle_relative", surface_tilt_deg=5.0)
    with pytest.raises(ModelConfigurationError):
        power_series([sample], arr)
    ok = PowerSample(solar=s, irradiance=irradiance_for(s, 700.0, 90.0), heading_deg=90.0)
    assert power_series([ok], arr)[0].net_power_w > 0.0


def test_tuple_time_mismatch_rejected() -> None:
    s = solar_at(40.0)
    with pytest.raises(ModelConfigurationError):
        power_series([(at(5.0), s, irradiance_for(s, 700.0, 90.0), 20.0, 2.0)], make_array())


def test_error_metrics_perfect_match_is_zero() -> None:
    m = compare_with_measured([100.0, 200.0, 300.0], [100.0, 200.0, 300.0])
    assert m.bias_w == 0.0 and m.mae_w == 0.0 and m.rmse_w == 0.0
    assert m.n == 3 and m.n_dropped == 0


def test_error_metrics_constant_offset_is_that_bias() -> None:
    meas = [100.0, 250.0, 400.0, 0.0]
    m = compare_with_measured([x + 25.0 for x in meas], meas)
    assert m.bias_w == pytest.approx(25.0)
    assert m.mae_w == pytest.approx(25.0)
    assert m.rmse_w == pytest.approx(25.0)
    assert m.relative_bias == pytest.approx(25.0 / 187.5)


def test_error_metrics_mixed_signs_and_dropped() -> None:
    m = compare_with_measured([110.0, 90.0, math.nan, 5.0], [100.0, 100.0, 50.0, None])
    assert m.n == 2 and m.n_dropped == 2
    assert m.bias_w == 0.0 and m.mae_w == 10.0 and m.rmse_w == pytest.approx(10.0)
    with pytest.raises(ValueError):
        compare_with_measured([1.0], [1.0, 2.0])
    with pytest.raises(ValueError):
        compare_with_measured([math.nan], [1.0])
