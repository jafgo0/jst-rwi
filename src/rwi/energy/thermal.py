"""Cell (junction) temperature models.

Units: irradiance W/m2, temperatures degC, wind m/s. Output is degC.

Models (select by name)
-----------------------
``faiman``
    ``T_cell = T_air + POA / (u0 + u1 * v)`` with defaults u0 = 25 W/(m2 K),
    u1 = 6.84 W s/(m3 K) (pvlib defaults). Faiman, D. (2008), "Assessing the
    outdoor operating temperature of photovoltaic modules", Prog. Photovolt.
    16, 307-315. Implemented with :func:`pvlib.temperature.faiman`.
``sapm``
    Sandia Array Performance Model module-temperature model, King, Boyson &
    Kratochvil (2004), SAND2004-3535: ``T_back = POA * exp(a + b*v) + T_air``,
    ``T_cell = T_back + POA/1000 * deltaT``. Implemented with
    :func:`pvlib.temperature.sapm_cell`. Here ``sapm`` uses the
    ``insulated_back_glass_polymer`` coefficient set (a=-2.81, b=-0.0455,
    deltaT=0), the closest published analogue of a laminate bonded to a body
    shell with no rear air gap. ``sapm_open_rack`` uses
    ``open_rack_glass_polymer`` (a=-3.56, b=-0.075, deltaT=3), the free-standing
    reference, so the two can be compared to bound the uncertainty.
``noct``
    Classic linear NOCT relation ``T_cell = T_air + (NOCT - 20) / 800 * POA``
    (NOCT defined at 800 W/m2, 20 degC, 1 m/s, open-rack mounting). Wind
    independent; ``noct_c`` defaults to 48 degC when not given.

Wind height - read this
-----------------------
Provider wind speed is the 10 m value. The models do not agree on the height
they expect:

* SAPM coefficients were fitted against a 10 m anemometer, so ``sapm`` and
  ``sapm_open_rack`` take the 10 m wind **unchanged** by default.
* Faiman's coefficients refer to wind at module height (pvlib: "measured at the
  same height for which the wind loss factor was determined"). A solar car's
  array is roughly 0.8-1.2 m above the road, where the wind is much slower than
  at 10 m (and the surface is sheltered by the body). Using the 10 m value
  directly would overstate convective cooling, i.e. underestimate cell
  temperature, i.e. overestimate power.

What is done about it: for ``faiman`` the 10 m wind is multiplied by
``wind_height_factor`` before use. The default factor is
:data:`DEFAULT_WIND_HEIGHT_FACTOR` = 0.60, the neutral logarithmic-profile
ratio ``ln(1.0/z0) / ln(10/z0)`` for an array height of 1 m and roughness length
z0 = 0.03 m (short grass / open flat terrain). This is a documented assumption,
not a fit: it ignores atmospheric stability, vehicle-induced sheltering and the
vehicle's own motion. Pass ``wind_height_factor=1.0`` to disable it. For the
SAPM variants the factor defaults to 1.0 (no adjustment) because the
coefficients were already fitted to 10 m wind; the factor may still be passed
explicitly. ``noct`` ignores wind.

Why a vehicle array runs hotter than a free-standing module
-----------------------------------------------------------
Free-standing module coefficients (open rack, both faces exposed) describe a
panel cooled on front *and* back. A vehicle array is laminated to or
bonded onto a body shell: there is no rear air gap, so the heat-transfer
area is roughly halved and the back of the cells is thermally coupled to a
body that also absorbs solar heat. That raises T_cell for the same POA, air
temperature and wind. Driving partly offsets this: the relative air speed over
the roof (tens of km/h = several m/s) is far above the ambient wind and
strongly cools the top surface; but the boundary layer on a streamlined body is
thin and the flow is not representative of a flat plate on a rack. The net
effect on a particular vehicle is **unvalidated** until telemetry (cell
temperature sensors on the array, air temperature, vehicle speed) exists; it is
the single most important thing to measure for this module. This function does
not take vehicle speed, so driving-induced forced convection is not modelled.
"""

from __future__ import annotations

import math

from pvlib import temperature as pvtemp

from rwi.core.errors import ModelConfigurationError

__all__ = [
    "CELL_TEMPERATURE_MODELS",
    "DEFAULT_WIND_HEIGHT_FACTOR",
    "FAIMAN_U0",
    "FAIMAN_U1",
    "cell_temperature_c",
    "log_wind_height_factor",
    "wind_height_factor_for",
]

FAIMAN_U0 = 25.0
"""Faiman constant heat-loss coefficient, W/(m2 K) (pvlib default)."""
FAIMAN_U1 = 6.84
"""Faiman wind-dependent heat-loss coefficient, W s/(m3 K) (pvlib default)."""

CELL_TEMPERATURE_MODELS: tuple[str, ...] = ("faiman", "sapm", "sapm_open_rack", "noct")

_SAPM_SETS = {
    "sapm": "insulated_back_glass_polymer",
    "sapm_open_rack": "open_rack_glass_polymer",
}

_REFERENCE_HEIGHT_M = 10.0


def log_wind_height_factor(
    array_height_m: float = 1.0, roughness_length_m: float = 0.03, reference_height_m: float = 10.0
) -> float:
    """Neutral log-law ratio ``v(array_height) / v(reference_height)`` (dimensionless).

    ``v(z) = (u*/kappa) * ln(z / z0)`` so the ratio is ``ln(z/z0) / ln(zref/z0)``.
    Valid for neutral stability over uniform terrain and z > z0. Does not
    include vehicle sheltering or the vehicle's own motion.
    """
    if not (roughness_length_m > 0.0 and array_height_m > roughness_length_m):
        raise ModelConfigurationError("need 0 < roughness_length_m < array_height_m")
    if reference_height_m <= array_height_m:
        raise ModelConfigurationError("reference_height_m must exceed array_height_m")
    return math.log(array_height_m / roughness_length_m) / math.log(
        reference_height_m / roughness_length_m
    )


DEFAULT_WIND_HEIGHT_FACTOR = log_wind_height_factor(1.0, 0.03, 10.0)
"""~0.60: 10 m wind -> 1 m wind over open terrain (z0 = 0.03 m). Assumption."""


def wind_height_factor_for(model: str) -> float:
    """Default 10 m -> array-height factor for ``model`` (see module docstring)."""
    return DEFAULT_WIND_HEIGHT_FACTOR if model == "faiman" else 1.0


def cell_temperature_c(
    poa_global_w_m2: float,
    air_temperature_c: float,
    wind_speed_mps: float,
    *,
    model: str = "faiman",
    noct_c: float | None = None,
    wind_height_factor: float | None = None,
) -> float:
    """Cell temperature in degC from POA irradiance, air temperature and 10 m wind.

    Parameters
    ----------
    poa_global_w_m2
        Plane-of-array irradiance, W/m2 (>= 0).
    air_temperature_c
        Ambient air temperature, degC.
    wind_speed_mps
        Wind speed **at 10 m** as delivered by the weather providers, m/s (>= 0).
    model
        One of :data:`CELL_TEMPERATURE_MODELS`.
    noct_c
        NOCT in degC, only for ``model="noct"`` (default 48).
    wind_height_factor
        Multiplier converting 10 m wind to array-height wind. ``None`` selects
        the per-model default (:func:`wind_height_factor_for`): 0.60 for
        ``faiman``, 1.0 otherwise. Pass 1.0 to use the 10 m wind unchanged.

    At night (POA = 0) every model returns the air temperature.
    """
    if model not in CELL_TEMPERATURE_MODELS:
        raise ModelConfigurationError(
            f"unknown cell temperature model {model!r}; choose from {CELL_TEMPERATURE_MODELS}"
        )
    for name, value in (
        ("poa_global_w_m2", poa_global_w_m2),
        ("air_temperature_c", air_temperature_c),
        ("wind_speed_mps", wind_speed_mps),
    ):
        if not math.isfinite(value):
            raise ModelConfigurationError(f"{name} is not finite: {value!r}")
    if poa_global_w_m2 < 0.0:
        raise ModelConfigurationError(f"poa_global_w_m2 must be >= 0, got {poa_global_w_m2}")
    if wind_speed_mps < 0.0:
        raise ModelConfigurationError(f"wind_speed_mps must be >= 0, got {wind_speed_mps}")
    factor = wind_height_factor_for(model) if wind_height_factor is None else wind_height_factor
    if not (0.0 < factor <= 1.5):
        raise ModelConfigurationError(f"wind_height_factor must be in (0, 1.5], got {factor}")
    wind = wind_speed_mps * factor

    if model == "faiman":
        return float(
            pvtemp.faiman(poa_global_w_m2, air_temperature_c, wind, u0=FAIMAN_U0, u1=FAIMAN_U1)
        )
    if model in _SAPM_SETS:
        params = pvtemp.TEMPERATURE_MODEL_PARAMETERS["sapm"][_SAPM_SETS[model]]
        return float(pvtemp.sapm_cell(poa_global_w_m2, air_temperature_c, wind, **params))
    # noct
    noct = 48.0 if noct_c is None else noct_c
    if not (30.0 <= noct <= 70.0):
        raise ModelConfigurationError(f"noct_c implausible: {noct}")
    return air_temperature_c + (noct - 20.0) / 800.0 * poa_global_w_m2
