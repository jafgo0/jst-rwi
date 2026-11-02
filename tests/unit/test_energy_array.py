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
    """Neither default represents the 2027 car. DEFAULT_ARRAY is the 2025 car
    (least wrong available); MONO_SI_ARRAY is a generic comparison. Both must
    say so, and both must list the measurements that would replace them."""
    for arr in (DEFAULT_ARRAY, MONO_SI_ARRAY):
        assert arr.parameter_source == "assumed"
        assert arr.source_notes, f"{arr.name} has no source_notes"
        for word in ("area", "efficiency", "mppt"):
            assert word in arr.source_notes.lower(), (
                f"{arr.name} source_notes do not mention {word}"
            )
    # The current default comes from Tesseract 2025 and must say so.
    assert "2025" in DEFAULT_ARRAY.name
    assert "NOT the 2027" in DEFAULT_ARRAY.name or "NOT THE 2027" in DEFAULT_ARRAY.name.upper()
    assert "338 cells" in DEFAULT_ARRAY.source_notes or "shadow-analysis" in DEFAULT_ARRAY.source_notes
    # MONO_SI stays a generic placeholder for comparison.
    assert "PLACEHOLDER" in MONO_SI_ARRAY.name or "PLACEHOLDER" in (MONO_SI_ARRAY.source_notes or "")
    # Mono-Si is less efficient and derates more with temperature than the
    # perovskite-silicon tandem the team actually uses - this is the whole
    # reason tandems are chosen for solar racing.
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
    """The chain POA -> T_cell (Faiman) -> dc power -> net power is checked
    against a hand calculation. Values are read from DEFAULT_ARRAY rather than
    hard-coded so that updating the default (e.g. 2025 Tesseract vs 2027 car)
    does not break the invariant - which is that the formula is right.
    """
    import math

    solar = solar_at(60.0)
    irr = irradiance_for(solar, 800.0, 100.0)
    poa = poa_irradiance(solar, irr, surface_tilt_deg=0.0, surface_azimuth_deg=180.0)
    g = 800.0 * math.sin(math.radians(60.0)) + 100.0
    assert poa.poa_global_w_m2 == pytest.approx(g, abs=1e-6)
    est = array_power(
        poa, DEFAULT_ARRAY, air_temperature_c=25.0, wind_speed_mps=4.0, wind_height_factor=1.0
    )
    # Faiman at wind_height_factor=1 keeps the 10 m wind as-is.
    t_cell = 25.0 + g / (25.0 + 6.84 * 4.0)
    gamma = DEFAULT_ARRAY.temperature_coefficient_pmax_per_c
    p_dc = (
        DEFAULT_ARRAY.area_m2
        * DEFAULT_ARRAY.cell_efficiency_stc
        * g
        * (1.0 + gamma * (t_cell - 25.0))
    )
    p_net = p_dc * DEFAULT_ARRAY.mppt_efficiency * DEFAULT_ARRAY.wiring_efficiency
    assert est.cell_temperature_c == pytest.approx(t_cell, abs=1e-9)
    assert est.dc_power_w == pytest.approx(p_dc, abs=1e-6)
    assert est.net_power_w == pytest.approx(p_net, abs=1e-6)
