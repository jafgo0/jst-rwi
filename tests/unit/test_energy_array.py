"""Array power chain tests, including the dimensional regression guards."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from test_energy_support import flat_poa, irradiance_for, make_array, solar_at

from rwi.contracts import ArrayConfig
from rwi.energy.array import (
    ASSUMED_AIR_TEMPERATURE_C,
    DEFAULT_ARRAY,
    MONO_SI_ARRAY,
    array_power,
)
from rwi.energy.poa import poa_irradiance


def test_night_everything_zero() -> None:
    solar = solar_at(-12.0)
    irr = irradiance_for(solar, 0.0, 0.0)
    poa = poa_irradiance(solar, irr, surface_tilt_deg=0.0, surface_azimuth_deg=180.0)
    est = array_power(poa, DEFAULT_ARRAY, air_temperature_c=10.0, wind_speed_mps=2.0)
    assert est.dc_power_w == 0.0
    assert est.net_power_w == 0.0
    assert est.effective_efficiency == 0.0
    assert est.cell_temperature_c == pytest.approx(10.0)


def test_dimensions_exact_960_w_at_stc() -> None:
    # 4.0 m2 * 0.24 * 1000 W/m2 = 960 W (watts, not Wh, not W/m2). Exact.
    arr = make_array()
    est = array_power(
        flat_poa(1000.0), arr, air_temperature_c=25.0, wind_speed_mps=1.0,
        cell_temperature_override_c=25.0,
    )
    assert est.dc_power_w == 960.0
    assert est.net_power_w == 960.0 * 0.98 * 0.98
    assert est.net_power_w == pytest.approx(921.984, abs=1e-9)
    assert est.effective_efficiency == pytest.approx(921.984 / 4000.0, abs=1e-12)
    assert est.cell_temperature_model == "override"


def test_power_scales_linearly_with_irradiance_and_area() -> None:
    a1 = make_array(area_m2=2.0)
    a2 = make_array(area_m2=4.0)
    kw = {"air_temperature_c": 25.0, "wind_speed_mps": 1.0, "cell_temperature_override_c": 25.0}
    p1 = array_power(flat_poa(500.0), a1, **kw).dc_power_w
    p2 = array_power(flat_poa(1000.0), a2, **kw).dc_power_w
    assert p1 == pytest.approx(2.0 * 0.24 * 500.0, abs=1e-9)
    assert p2 == pytest.approx(4.0 * p1, rel=1e-12)


def test_temperature_derating_exactly_14_percent() -> None:
    arr = make_array(temperature_coefficient_pmax_per_c=-0.0035)
    kw = {"air_temperature_c": 25.0, "wind_speed_mps": 1.0}
    p25 = array_power(flat_poa(1000.0), arr, cell_temperature_override_c=25.0, **kw).dc_power_w
    p65 = array_power(flat_poa(1000.0), arr, cell_temperature_override_c=65.0, **kw).dc_power_w
    assert p65 / p25 == pytest.approx(1.0 + (-0.0035 * 40.0), rel=1e-12)  # 0.86
    assert p25 - p65 == pytest.approx(0.14 * p25, rel=1e-9)


def test_clamped_at_zero_for_absurd_temperature() -> None:
    arr = make_array(temperature_coefficient_pmax_per_c=-0.0035)
    est = array_power(
        flat_poa(1000.0), arr, air_temperature_c=25.0, wind_speed_mps=1.0,
        cell_temperature_override_c=400.0,
    )
    assert est.dc_power_w == 0.0 and est.net_power_w == 0.0


def test_modelled_cell_temperature_is_used_and_hot_cells_lose_power() -> None:
    arr = make_array()
    est = array_power(flat_poa(1000.0), arr, air_temperature_c=30.0, wind_speed_mps=2.0)
    assert est.cell_temperature_model == "faiman"
    assert est.cell_temperature_c > 30.0 + 20.0
    assert est.dc_power_w < 960.0
    assert est.air_temperature_c == 30.0 and est.wind_speed_mps == 2.0


def test_missing_weather_falls_back_and_is_recorded() -> None:
    est = array_power(flat_poa(800.0), DEFAULT_ARRAY, air_temperature_c=None, wind_speed_mps=None)
    notes = est.provenance.notes or ""
    assert "air temperature MISSING" in notes and "wind speed MISSING" in notes
    assert est.air_temperature_c is None and est.wind_speed_mps is None
    ref = array_power(
        flat_poa(800.0), DEFAULT_ARRAY, air_temperature_c=ASSUMED_AIR_TEMPERATURE_C,
        wind_speed_mps=3.0,
    )
    assert est.net_power_w == pytest.approx(ref.net_power_w)
    assert "MISSING" not in (ref.provenance.notes or "")


def test_assumed_array_is_flagged_in_provenance() -> None:
    est = array_power(flat_poa(800.0), DEFAULT_ARRAY, air_temperature_c=20.0, wind_speed_mps=2.0)
    assert "not measured" in (est.provenance.notes or "")


def test_default_arrays_are_labelled_assumed() -> None:
    for arr in (DEFAULT_ARRAY, MONO_SI_ARRAY):
        assert arr.parameter_source == "assumed"
        assert arr.source_notes and "PLACEHOLDER" in arr.source_notes
        assert "JU" not in arr.name or "not JST" in arr.name
        for word in ("area", "efficiency", "temperature_coefficient", "mppt", "wiring", "mounting"):
            assert word in arr.source_notes
    # Mono-Si comparison array is the less efficient, hotter-derating one.
    assert MONO_SI_ARRAY.cell_efficiency_stc < DEFAULT_ARRAY.cell_efficiency_stc
    assert (
        MONO_SI_ARRAY.temperature_coefficient_pmax_per_c
        < DEFAULT_ARRAY.temperature_coefficient_pmax_per_c
    )


def test_array_config_rejects_bad_parameters() -> None:
    with pytest.raises(ValidationError):
        make_array(area_m2=-1.0)
    with pytest.raises(ValidationError):
        make_array(area_m2=0.0)
    with pytest.raises(ValidationError):
        make_array(cell_efficiency_stc=1.2)
    with pytest.raises(ValidationError):
        make_array(temperature_coefficient_pmax_per_c=0.001)
    assert isinstance(make_array(), ArrayConfig)


@pytest.mark.science
def test_full_chain_clear_noon_against_hand_calculation() -> None:
    # Hand-computed reference: sun 90 deg overhead is not allowed to be exactly
    # singular for AOI, so use elevation 60 deg, DNI 800, DHI 100, flat array.
    #   POA = 800*sin(60) + 100 = 792.820323 W/m2 (Hay-Davies reduces to this on a flat plane)
    #   T_cell (Faiman, 10 m wind 4 m/s, factor 1) = 25 + POA/(25+6.84*4)
    #   P_dc = 4*0.24*POA*(1 + (-0.0029)*(T_cell-25)); P_net = P_dc*0.98*0.98
    # Tolerance 1e-6 W.
    import math

    solar = solar_at(60.0)
    irr = irradiance_for(solar, 800.0, 100.0)
    poa = poa_irradiance(solar, irr, surface_tilt_deg=0.0, surface_azimuth_deg=180.0)
    g = 800.0 * math.sin(math.radians(60.0)) + 100.0
    assert poa.poa_global_w_m2 == pytest.approx(g, abs=1e-6)
    est = array_power(
        poa, DEFAULT_ARRAY, air_temperature_c=25.0, wind_speed_mps=4.0, wind_height_factor=1.0
    )
    t_cell = 25.0 + g / (25.0 + 6.84 * 4.0)
    p_dc = 4.0 * 0.24 * g * (1.0 + (-0.0029) * (t_cell - 25.0))
    assert est.cell_temperature_c == pytest.approx(t_cell, abs=1e-9)
    assert est.dc_power_w == pytest.approx(p_dc, abs=1e-6)
    assert est.net_power_w == pytest.approx(p_dc * 0.98 * 0.98, abs=1e-6)
