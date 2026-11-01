"""Cell-temperature model tests."""

from __future__ import annotations

import pytest

from rwi.core.errors import ModelConfigurationError
from rwi.energy.thermal import (
    DEFAULT_WIND_HEIGHT_FACTOR,
    cell_temperature_c,
    log_wind_height_factor,
)


@pytest.mark.science
def test_faiman_reference_triple() -> None:
    # Faiman (2008) Prog. Photovolt. 16:307 with the pvlib default coefficients
    # u0=25 W/(m2 K), u1=6.84 W s/(m3 K): T = 20 + 1000/(25 + 6.84*1) = 51.40703517...
    # Hand-computed; tolerance 1e-9 degC. wind_height_factor=1.0 disables the
    # 10 m -> array-height adjustment so the published form is tested.
    t = cell_temperature_c(1000.0, 20.0, 1.0, model="faiman", wind_height_factor=1.0)
    assert t == pytest.approx(20.0 + 1000.0 / (25.0 + 6.84), abs=1e-9)
    assert t == pytest.approx(51.4070351758794, abs=1e-9)


def test_noct_definition_reproduces_noct() -> None:
    # NOCT is defined at 800 W/m2, 20 degC ambient: T_cell must equal NOCT there.
    assert cell_temperature_c(800.0, 20.0, 1.0, model="noct", noct_c=45.0) == pytest.approx(45.0)
    assert cell_temperature_c(800.0, 20.0, 9.0, model="noct") == pytest.approx(48.0)  # default


def test_night_returns_air_temperature_for_all_models() -> None:
    for m in ("faiman", "sapm", "sapm_open_rack", "noct"):
        assert cell_temperature_c(0.0, 12.5, 4.0, model=m) == pytest.approx(12.5)


def test_sapm_known_value_and_open_rack_is_cooler() -> None:
    # SAPM insulated-back glass/polymer: a=-2.81, b=-0.0455, deltaT=0 (King et al. 2004
    # / pvlib TEMPERATURE_MODEL_PARAMETERS). T = Tair + POA*exp(a + b*v).
    import math

    t = cell_temperature_c(1000.0, 20.0, 2.0, model="sapm")
    assert t == pytest.approx(20.0 + 1000.0 * math.exp(-2.81 - 0.0455 * 2.0), abs=1e-9)
    assert cell_temperature_c(1000.0, 20.0, 2.0, model="sapm_open_rack") < t


def test_wind_height_adjustment_makes_faiman_hotter() -> None:
    adj = cell_temperature_c(1000.0, 25.0, 6.0, model="faiman")
    raw = cell_temperature_c(1000.0, 25.0, 6.0, model="faiman", wind_height_factor=1.0)
    assert adj > raw
    assert pytest.approx(0.6045, abs=1e-3) == DEFAULT_WIND_HEIGHT_FACTOR
    expected = 25.0 + 1000.0 / (25.0 + 6.84 * 6.0 * DEFAULT_WIND_HEIGHT_FACTOR)
    assert adj == pytest.approx(expected, abs=1e-9)


def test_log_law_factor() -> None:
    assert log_wind_height_factor(10.0 * 0.5, 0.03, 10.0) < 1.0
    with pytest.raises(ModelConfigurationError):
        log_wind_height_factor(0.01, 0.03, 10.0)


def test_more_wind_means_cooler_and_more_poa_means_hotter() -> None:
    assert cell_temperature_c(900, 25, 5) < cell_temperature_c(900, 25, 1)
    assert cell_temperature_c(900, 25, 3) > cell_temperature_c(500, 25, 3)


def test_invalid_inputs() -> None:
    with pytest.raises(ModelConfigurationError):
        cell_temperature_c(500, 20, 2, model="mystery")
    with pytest.raises(ModelConfigurationError):
        cell_temperature_c(-1, 20, 2)
    with pytest.raises(ModelConfigurationError):
        cell_temperature_c(500, 20, -2)
    with pytest.raises(ModelConfigurationError):
        cell_temperature_c(500, float("nan"), 2)
