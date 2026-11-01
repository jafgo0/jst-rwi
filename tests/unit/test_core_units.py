"""Units and the irradiance / power / energy separation."""

from __future__ import annotations

import pytest

from rwi.core.units import (
    air_density_kg_m3,
    angular_difference_deg,
    hpa_to_pa,
    integrate_power_wh,
    kmh_to_ms,
    ms_to_kmh,
    wrap_deg_180,
    wrap_deg_360,
)


def test_speed_conversions_round_trip() -> None:
    assert kmh_to_ms(36.0) == pytest.approx(10.0)
    assert ms_to_kmh(10.0) == pytest.approx(36.0)
    assert ms_to_kmh(kmh_to_ms(87.3)) == pytest.approx(87.3)


def test_pressure_conversion() -> None:
    assert hpa_to_pa(1013.25) == pytest.approx(101325.0)


def test_constant_power_integrates_to_exact_energy() -> None:
    """500 W held for exactly two hours is 1000 Wh. Not 1000 W, not 1000 J."""
    assert integrate_power_wh([500.0, 500.0, 500.0], [0.0, 3600.0, 7200.0]) == pytest.approx(1000.0)


def test_triangular_profile_matches_the_analytic_trapezoid() -> None:
    # 0 -> 1000 W over one hour: mean 500 W, so 500 Wh.
    assert integrate_power_wh([0.0, 1000.0], [0.0, 3600.0]) == pytest.approx(500.0)


def test_a_single_sample_carries_no_energy() -> None:
    assert integrate_power_wh([800.0], [0.0]) == 0.0
    assert integrate_power_wh([], []) == 0.0


def test_mismatched_or_backwards_series_are_rejected() -> None:
    with pytest.raises(ValueError, match="equal length"):
        integrate_power_wh([1.0, 2.0], [0.0])
    with pytest.raises(ValueError, match="non-decreasing"):
        integrate_power_wh([1.0, 2.0], [3600.0, 0.0])


def test_angle_wrapping() -> None:
    assert wrap_deg_360(370.0) == pytest.approx(10.0)
    assert wrap_deg_360(-10.0) == pytest.approx(350.0)
    assert wrap_deg_180(190.0) == pytest.approx(-170.0)
    assert wrap_deg_180(180.0) == pytest.approx(180.0)
    assert angular_difference_deg(10.0, 350.0) == pytest.approx(20.0)
    assert angular_difference_deg(350.0, 10.0) == pytest.approx(-20.0)


def test_air_density_at_sea_level_and_altitude() -> None:
    """ISA sea level: 101325 Pa, 15 degC -> 1.225 kg/m3."""
    assert air_density_kg_m3(101325.0, 15.0) == pytest.approx(1.225, abs=0.001)
    # Alice Springs, ~545 m and 30 degC: thinner and hotter, so clearly lighter air.
    assert air_density_kg_m3(95000.0, 30.0) < 1.15
