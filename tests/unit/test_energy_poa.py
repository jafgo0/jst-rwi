"""POA transposition tests. No network."""

from __future__ import annotations

import pytest
from test_energy_support import POINT, irradiance_for, make_array, solar_at

from rwi.core.errors import ModelConfigurationError
from rwi.energy.poa import (
    HORIZONTAL_AZIMUTH_CONVENTION_DEG,
    poa_irradiance,
    surface_azimuth_for_heading,
)


def test_night_all_components_zero() -> None:
    solar = solar_at(-10.0)
    irr = irradiance_for(solar, 0.0, 0.0)
    poa = poa_irradiance(solar, irr, surface_tilt_deg=20.0, surface_azimuth_deg=90.0)
    assert poa.poa_global_w_m2 == 0.0
    assert poa.poa_direct_w_m2 == 0.0
    assert poa.poa_sky_diffuse_w_m2 == 0.0
    assert poa.poa_ground_diffuse_w_m2 == 0.0


@pytest.mark.parametrize("model", ["isotropic", "haydavies", "reindl"])
def test_horizontal_equals_ghi_when_components_are_closed(model: str) -> None:
    # Horizontal plane: POA = DNI*cos(z) + DHI, which equals GHI only if the
    # supplied components satisfy GHI = DNI*cos(z) + DHI. With closed inputs the
    # difference is pure float round-off; tolerance 1e-6 W/m2.
    solar = solar_at(45.0)
    irr = irradiance_for(solar, 700.0, 120.0, closed=True)
    poa = poa_irradiance(
        solar, irr, surface_tilt_deg=0.0, surface_azimuth_deg=180.0, model=model
    )
    assert poa.poa_global_w_m2 == pytest.approx(irr.ghi_w_m2, abs=1e-6)
    assert poa.poa_ground_diffuse_w_m2 == 0.0


def test_horizontal_differs_from_ghi_when_components_are_not_closed() -> None:
    # Independent GHI (e.g. forecast variables) is generally not closed: POA on
    # a flat plane follows DNI*cos(z)+DHI, not the supplied GHI.
    solar = solar_at(45.0)
    irr = irradiance_for(solar, 700.0, 120.0, ghi=800.0)
    poa = poa_irradiance(solar, irr, surface_tilt_deg=0.0, surface_azimuth_deg=180.0)
    expected = 700.0 * 0.7071067811865476 + 120.0
    assert poa.poa_global_w_m2 == pytest.approx(expected, abs=1e-6)
    assert abs(poa.poa_global_w_m2 - 800.0) > 100.0
    assert "not closed" in (poa.provenance.notes or "")


def test_tilt_towards_sun_beats_horizontal_at_low_sun() -> None:
    solar = solar_at(20.0, azimuth_deg=0.0)  # low sun in the north (southern hemisphere)
    irr = irradiance_for(solar, 600.0, 80.0)
    flat = poa_irradiance(solar, irr, surface_tilt_deg=0.0, surface_azimuth_deg=0.0)
    tilted = poa_irradiance(solar, irr, surface_tilt_deg=70.0, surface_azimuth_deg=0.0)
    assert tilted.aoi_deg == pytest.approx(0.0, abs=1e-6)  # tilt == zenith, facing the sun
    assert tilted.poa_direct_w_m2 == pytest.approx(600.0, abs=1e-6)
    assert tilted.poa_global_w_m2 > flat.poa_global_w_m2 + 100.0


def test_aoi_above_90_gives_zero_direct() -> None:
    solar = solar_at(20.0, azimuth_deg=0.0)
    irr = irradiance_for(solar, 600.0, 80.0)
    poa = poa_irradiance(solar, irr, surface_tilt_deg=60.0, surface_azimuth_deg=180.0)
    assert poa.aoi_deg > 90.0
    assert poa.poa_direct_w_m2 == 0.0
    assert poa.poa_global_w_m2 == pytest.approx(
        poa.poa_sky_diffuse_w_m2 + poa.poa_ground_diffuse_w_m2
    )


def test_components_sum_to_global_and_metadata_recorded() -> None:
    solar = solar_at(35.0, azimuth_deg=20.0)
    irr = irradiance_for(solar, 650.0, 90.0)
    poa = poa_irradiance(
        solar, irr, surface_tilt_deg=30.0, surface_azimuth_deg=10.0, albedo=0.3, model="perez"
    )
    assert poa.poa_global_w_m2 == pytest.approx(
        poa.poa_direct_w_m2 + poa.poa_sky_diffuse_w_m2 + poa.poa_ground_diffuse_w_m2
    )
    assert poa.albedo == 0.3 and poa.transposition_model == "perez"
    assert poa.surface_tilt_deg == 30.0 and poa.surface_azimuth_deg == 10.0
    assert poa.poa_ground_diffuse_w_m2 == pytest.approx(
        irr.ghi_w_m2 * 0.3 * (1 - 0.8660254037844387) / 2, rel=1e-9
    )


def test_bad_inputs_raise() -> None:
    solar = solar_at(35.0)
    irr = irradiance_for(solar, 650.0, 90.0)
    with pytest.raises(ModelConfigurationError):
        poa_irradiance(solar, irr, surface_tilt_deg=0, surface_azimuth_deg=0, model="nope")
    with pytest.raises(ModelConfigurationError):
        poa_irradiance(solar, irr, surface_tilt_deg=95, surface_azimuth_deg=0)
    other = solar_at(35.0, when=solar.time_utc.replace(hour=5))
    with pytest.raises(ModelConfigurationError):
        poa_irradiance(other, irr, surface_tilt_deg=0, surface_azimuth_deg=0)


@pytest.mark.science
def test_isotropic_horizontal_poa_reference_value() -> None:
    # Hand-computed reference (Liu & Jordan 1960 isotropic model on a horizontal
    # plane): POA = DNI*sin(elevation) + DHI = 800*sin(30 deg) + 100 = 500 W/m2.
    # Tolerance 1e-9 W/m2 (float round-off only).
    solar = solar_at(30.0)
    irr = irradiance_for(solar, 800.0, 100.0)
    poa = poa_irradiance(
        solar, irr, surface_tilt_deg=0.0, surface_azimuth_deg=180.0, model="isotropic"
    )
    assert poa.poa_global_w_m2 == pytest.approx(500.0, abs=1e-9)
    assert irr.ghi_w_m2 == pytest.approx(500.0, abs=1e-9)


# --- surface_azimuth_for_heading -------------------------------------------------


def test_horizontal_azimuth_is_convention_and_does_not_affect_result() -> None:
    arr = make_array(mounting="horizontal")
    assert surface_azimuth_for_heading(arr, None) == HORIZONTAL_AZIMUTH_CONVENTION_DEG
    assert surface_azimuth_for_heading(arr, 77.0) == HORIZONTAL_AZIMUTH_CONVENTION_DEG
    solar = solar_at(40.0, azimuth_deg=300.0)
    irr = irradiance_for(solar, 700.0, 100.0)
    a = poa_irradiance(solar, irr, surface_tilt_deg=0.0, surface_azimuth_deg=0.0)
    b = poa_irradiance(solar, irr, surface_tilt_deg=0.0, surface_azimuth_deg=237.0)
    assert a.poa_global_w_m2 == pytest.approx(b.poa_global_w_m2, abs=1e-9)


def test_fixed_tilt_returns_configured_azimuth() -> None:
    arr = make_array(mounting="fixed_tilt", surface_tilt_deg=20.0, surface_azimuth_deg=0.0)
    assert surface_azimuth_for_heading(arr, 123.0) == 0.0
    with pytest.raises(ModelConfigurationError):
        surface_azimuth_for_heading(make_array(mounting="fixed_tilt", surface_tilt_deg=20.0), 0.0)


def test_vehicle_relative_needs_heading() -> None:
    arr = make_array(mounting="vehicle_relative", surface_tilt_deg=5.0)
    with pytest.raises(ModelConfigurationError):
        surface_azimuth_for_heading(arr, None)


@pytest.mark.parametrize(
    ("relative", "heading", "expected"),
    [
        (None, 90.0, 90.0),
        (0.0, 350.0, 350.0),
        (90.0, 90.0, 180.0),
        (270.0, 10.0, 280.0),
        (180.0, 270.0, 90.0),
    ],
)
def test_vehicle_relative_convention(
    relative: float | None, heading: float, expected: float
) -> None:
    arr = make_array(
        mounting="vehicle_relative", surface_tilt_deg=5.0, surface_azimuth_deg=relative
    )
    assert surface_azimuth_for_heading(arr, heading) == pytest.approx(expected)


def test_point_constant_is_used() -> None:
    assert POINT.latitude_deg < 0  # sanity: tests use the southern hemisphere
