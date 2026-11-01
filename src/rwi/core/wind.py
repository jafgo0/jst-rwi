"""Wind vector conventions and vehicle-relative (apparent) wind.

Meteorological convention: ``direction_from_deg`` is the direction the wind
blows *from*, degrees clockwise from true north. A 180 deg wind blows from the
south towards the north.

Getting this wrong silently inverts head/tailwind in the energy model, so the
conversion lives in exactly one place and is covered by sign tests.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field

from rwi.core.units import wrap_deg_180, wrap_deg_360


def met_to_uv(speed_mps: float, direction_from_deg: float) -> tuple[float, float]:
    """Meteorological (speed, direction-from) -> (u_east, v_north) components in m/s."""
    rad = math.radians(direction_from_deg)
    return -speed_mps * math.sin(rad), -speed_mps * math.cos(rad)


def uv_to_met(u_east_mps: float, v_north_mps: float) -> tuple[float, float]:
    """(u_east, v_north) -> (speed, direction-from in [0,360))."""
    speed = math.hypot(u_east_mps, v_north_mps)
    if speed == 0.0:
        return 0.0, 0.0
    direction = wrap_deg_360(math.degrees(math.atan2(-u_east_mps, -v_north_mps)))
    return speed, direction


class RelativeWind(BaseModel):
    """Wind decomposed in the vehicle's frame of reference."""

    model_config = ConfigDict(frozen=True)

    headwind_mps: float = Field(
        description="component opposing travel; positive = headwind, negative = tailwind"
    )
    crosswind_mps: float = Field(
        description="component perpendicular to travel; positive = from the vehicle's left"
    )
    apparent_speed_mps: float = Field(ge=0.0, description="magnitude of the apparent wind")
    yaw_angle_deg: float = Field(
        description="angle between apparent wind and vehicle axis, (-180,180]; "
        "0 = straight on the nose"
    )


def relative_wind(
    *,
    vehicle_speed_mps: float,
    vehicle_heading_deg: float,
    wind_speed_mps: float,
    wind_from_deg: float,
) -> RelativeWind:
    """Apparent wind seen by a vehicle moving over the ground.

    The apparent wind is the vector sum of the true wind and the reversed
    ground-speed vector. ``headwind_mps`` is what the aerodynamic drag model
    must use together with the vehicle's own speed, never the raw wind speed.
    """
    if vehicle_speed_mps < 0:
        raise ValueError("vehicle_speed_mps must be non-negative")
    u_w, v_w = met_to_uv(wind_speed_mps, wind_from_deg)
    heading_rad = math.radians(vehicle_heading_deg)
    # Unit vectors in the vehicle frame.
    fwd_e, fwd_n = math.sin(heading_rad), math.cos(heading_rad)
    left_e, left_n = -math.cos(heading_rad), math.sin(heading_rad)
    # Wind component along the direction of travel (positive = pushing forward).
    wind_along = u_w * fwd_e + v_w * fwd_n
    wind_left = u_w * left_e + v_w * left_n
    headwind = vehicle_speed_mps - wind_along
    crosswind = -wind_left
    apparent = math.hypot(headwind, crosswind)
    yaw = wrap_deg_180(math.degrees(math.atan2(crosswind, headwind))) if apparent > 0 else 0.0
    return RelativeWind(
        headwind_mps=headwind,
        crosswind_mps=crosswind,
        apparent_speed_mps=apparent,
        yaw_angle_deg=yaw,
    )
