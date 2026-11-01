"""Wind conventions. An inverted sign here would corrupt every energy estimate."""

from __future__ import annotations

import pytest

from rwi.core.wind import met_to_uv, relative_wind, uv_to_met


def test_meteorological_direction_is_where_the_wind_comes_from() -> None:
    # A northerly (from 0 deg) blows towards the south: v is negative, u is zero.
    u, v = met_to_uv(10.0, 0.0)
    assert u == pytest.approx(0.0, abs=1e-9)
    assert v == pytest.approx(-10.0)
    # An easterly (from 90 deg) blows towards the west.
    u, v = met_to_uv(10.0, 90.0)
    assert u == pytest.approx(-10.0)
    assert v == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize("direction", [0.0, 37.0, 90.0, 180.0, 271.5, 359.0])
def test_uv_round_trip(direction: float) -> None:
    speed, back = uv_to_met(*met_to_uv(7.5, direction))
    assert speed == pytest.approx(7.5)
    assert back == pytest.approx(direction, abs=1e-6)


def test_calm_wind_has_no_direction() -> None:
    assert uv_to_met(0.0, 0.0) == (0.0, 0.0)


def test_headwind_adds_to_airspeed() -> None:
    """Driving south (heading 180) into a southerly (from 180) is a headwind."""
    rw = relative_wind(
        vehicle_speed_mps=25.0, vehicle_heading_deg=180.0, wind_speed_mps=10.0, wind_from_deg=180.0
    )
    assert rw.headwind_mps == pytest.approx(35.0)
    assert rw.crosswind_mps == pytest.approx(0.0, abs=1e-9)
    assert rw.apparent_speed_mps == pytest.approx(35.0)
    assert rw.yaw_angle_deg == pytest.approx(0.0, abs=1e-9)


def test_tailwind_subtracts_from_airspeed() -> None:
    rw = relative_wind(
        vehicle_speed_mps=25.0, vehicle_heading_deg=180.0, wind_speed_mps=10.0, wind_from_deg=0.0
    )
    assert rw.headwind_mps == pytest.approx(15.0)
    assert rw.crosswind_mps == pytest.approx(0.0, abs=1e-9)


def test_tailwind_stronger_than_the_car_gives_a_negative_headwind() -> None:
    rw = relative_wind(
        vehicle_speed_mps=5.0, vehicle_heading_deg=180.0, wind_speed_mps=12.0, wind_from_deg=0.0
    )
    assert rw.headwind_mps == pytest.approx(-7.0)
    assert rw.apparent_speed_mps == pytest.approx(7.0)
    assert abs(rw.yaw_angle_deg) == pytest.approx(180.0)


def test_pure_crosswind_still_increases_the_apparent_speed() -> None:
    """Driving south with a westerly (from 270): the wind blows towards the east,
    which is the vehicle's left-hand side, so it arrives from the right."""
    rw = relative_wind(
        vehicle_speed_mps=25.0, vehicle_heading_deg=180.0, wind_speed_mps=10.0, wind_from_deg=270.0
    )
    assert rw.headwind_mps == pytest.approx(25.0)
    assert rw.crosswind_mps == pytest.approx(-10.0)
    assert rw.apparent_speed_mps == pytest.approx((25.0**2 + 10.0**2) ** 0.5)
    assert rw.yaw_angle_deg == pytest.approx(-21.80140948635181, abs=1e-6)


def test_the_same_wind_is_head_or_tail_depending_on_heading() -> None:
    """Wind direction alone says nothing: the vehicle's heading decides the effect.
    This is the error the master specification calls out explicitly."""
    northbound = relative_wind(
        vehicle_speed_mps=25.0, vehicle_heading_deg=0.0, wind_speed_mps=10.0, wind_from_deg=0.0
    )
    southbound = relative_wind(
        vehicle_speed_mps=25.0, vehicle_heading_deg=180.0, wind_speed_mps=10.0, wind_from_deg=0.0
    )
    assert northbound.headwind_mps == pytest.approx(35.0)
    assert southbound.headwind_mps == pytest.approx(15.0)


def test_stationary_vehicle_sees_the_true_wind() -> None:
    rw = relative_wind(
        vehicle_speed_mps=0.0, vehicle_heading_deg=45.0, wind_speed_mps=8.0, wind_from_deg=45.0
    )
    assert rw.apparent_speed_mps == pytest.approx(8.0)
    assert rw.headwind_mps == pytest.approx(8.0)


def test_negative_vehicle_speed_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        relative_wind(
            vehicle_speed_mps=-1.0,
            vehicle_heading_deg=0.0,
            wind_speed_mps=1.0,
            wind_from_deg=0.0,
        )
