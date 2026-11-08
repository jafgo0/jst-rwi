"""Canonical meteorological variable registry.

Every variable the platform transports has exactly one name, one unit and one
physically plausible range. Provider adapters convert into these units at the
boundary; quality control uses the ranges; the UI reads the unit from here
instead of hardcoding a label.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from rwi.core.errors import UnknownVariableError


class VariableClass(StrEnum):
    THERMODYNAMIC = "thermodynamic"
    WIND = "wind"
    MOISTURE = "moisture"
    CLOUD = "cloud"
    PRECIPITATION = "precipitation"
    RADIATION = "radiation"
    VISIBILITY = "visibility"
    STATE = "state"


class VariableSpec(BaseModel):
    """Definition of one canonical variable."""

    model_config = ConfigDict(frozen=True)

    name: str
    unit: str
    minimum: float
    maximum: float
    variable_class: VariableClass
    description: str
    max_plausible_rate_per_hour: float | None = None
    """Step-change limit used by the rate-of-change quality check, in unit/hour."""


def _spec(
    name: str,
    unit: str,
    minimum: float,
    maximum: float,
    cls: VariableClass,
    description: str,
    rate: float | None = None,
) -> VariableSpec:
    return VariableSpec(
        name=name,
        unit=unit,
        minimum=minimum,
        maximum=maximum,
        variable_class=cls,
        description=description,
        max_plausible_rate_per_hour=rate,
    )


V = VariableClass

CANONICAL_VARIABLES: dict[str, VariableSpec] = {
    s.name: s
    for s in (
        _spec("temperature_2m", "degC", -90.0, 60.0, V.THERMODYNAMIC,
              "Air temperature at 2 m above ground", 15.0),
        _spec("apparent_temperature", "degC", -90.0, 80.0, V.THERMODYNAMIC,
              "Perceived temperature including wind and humidity"),
        _spec("dew_point_2m", "degC", -90.0, 40.0, V.MOISTURE,
              "Dew point temperature at 2 m", 15.0),
        _spec("relative_humidity_2m", "percent", 0.0, 100.0, V.MOISTURE,
              "Relative humidity at 2 m", 80.0),
        _spec("pressure_msl", "Pa", 85000.0, 110000.0, V.THERMODYNAMIC,
              "Air pressure reduced to mean sea level", 1500.0),
        _spec("surface_pressure", "Pa", 50000.0, 110000.0, V.THERMODYNAMIC,
              "Air pressure at the surface", 1500.0),
        _spec("wind_speed_10m", "m/s", 0.0, 120.0, V.WIND,
              "Mean wind speed at 10 m above ground", 25.0),
        _spec("wind_direction_10m", "deg", 0.0, 360.0, V.WIND,
              "Direction the 10 m wind blows FROM, clockwise from true north"),
        _spec("wind_gusts_10m", "m/s", 0.0, 150.0, V.WIND,
              "Maximum gust at 10 m over the reporting interval", 40.0),
        _spec("cloud_cover", "percent", 0.0, 100.0, V.CLOUD,
              "Total cloud cover fraction of the sky", 100.0),
        _spec("cloud_cover_low", "percent", 0.0, 100.0, V.CLOUD, "Low cloud cover", 100.0),
        _spec("cloud_cover_mid", "percent", 0.0, 100.0, V.CLOUD, "Mid-level cloud cover", 100.0),
        _spec("cloud_cover_high", "percent", 0.0, 100.0, V.CLOUD, "High cloud cover", 100.0),
        _spec("precipitation", "mm", 0.0, 500.0, V.PRECIPITATION,
              "Liquid-equivalent precipitation accumulated over the step"),
        _spec("precipitation_probability", "percent", 0.0, 100.0, V.PRECIPITATION,
              "Probability of measurable precipitation in the step"),
        _spec("rain", "mm", 0.0, 500.0, V.PRECIPITATION, "Rain accumulated over the step"),
        _spec("ghi", "W/m2", 0.0, 1600.0, V.RADIATION,
              "Global horizontal irradiance", 1400.0),
        _spec("dni", "W/m2", 0.0, 1400.0, V.RADIATION,
              "Direct normal irradiance", 1300.0),
        _spec("dhi", "W/m2", 0.0, 900.0, V.RADIATION,
              "Diffuse horizontal irradiance", 800.0),
        _spec("terrestrial_radiation", "W/m2", 0.0, 1500.0, V.RADIATION,
              "Extraterrestrial irradiance on a horizontal plane"),
        _spec("uv_index", "index", 0.0, 20.0, V.RADIATION, "UV index"),
        _spec("visibility", "m", 0.0, 100000.0, V.VISIBILITY, "Horizontal visibility"),
        _spec("is_day", "bool", 0.0, 1.0, V.STATE,
              "Provider's own day/night flag; not a substitute for solar geometry"),
        _spec("weather_code", "wmo", 0.0, 99.0, V.STATE, "WMO 4677-style present weather code"),
        _spec("soil_temperature_0cm", "degC", -60.0, 90.0, V.THERMODYNAMIC,
              "Surface/skin temperature, proxy for road surface temperature", 25.0),
        # Instability indices: integrated thermodynamic quantities derived from
        # the model's vertical profile. Grouped under THERMODYNAMIC rather than
        # given a new variable class because they ARE thermodynamic and the
        # consumers (convection alerts, meteorologist layer) read them as
        # scalars, not as a new coordinate axis. Range floors/ceilings are the
        # physically plausible outer envelope; typical values are well inside.
        # No rate-of-change limit: these are prognostic indices that can swing
        # legitimately between forecast steps as the model updates its
        # boundary-layer scheme, so a step-change gate would drop valid data.
        _spec("cape", "J/kg", 0.0, 8000.0, V.THERMODYNAMIC,
              "Convective Available Potential Energy, surface-based unless the "
              "provider states otherwise"),
        _spec("lifted_index", "K", -20.0, 20.0, V.THERMODYNAMIC,
              "Lifted index: 500 hPa environment temperature minus lifted-parcel "
              "temperature; negative values indicate instability"),
    )
}

RADIATION_VARIABLES = tuple(
    name for name, s in CANONICAL_VARIABLES.items() if s.variable_class is V.RADIATION
)


def spec_for(name: str) -> VariableSpec:
    """Look up a variable spec, raising :class:`UnknownVariableError` if absent."""
    try:
        return CANONICAL_VARIABLES[name]
    except KeyError as exc:
        raise UnknownVariableError(
            f"{name!r} is not a canonical variable; add it to rwi.core.variables"
        ) from exc


def unit_for(name: str) -> str:
    return spec_for(name).unit
