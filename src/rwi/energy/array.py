"""Array electrical model: POA irradiance -> cell temperature -> DC -> net power.

Dimensional chain (never mix the three):

    irradiance  G   [W/m2]      plane-of-array, from rwi.energy.poa
    power       P   [W]         this module's output
    energy      E   [Wh]        rwi.energy.series via rwi.core.units.integrate_power_wh

Derivation of the DC relation (PVWatts-style, Dobos 2014, NREL/TP-6A20-62641)
-----------------------------------------------------------------------------
Module efficiency at Standard Test Conditions (G_stc = 1000 W/m2, T = 25 degC)
is eta_stc = P_stc / (A * G_stc)  [W / (m2 * W/m2) = dimensionless]. PVWatts
models DC power as linear in irradiance with a linear temperature correction::

    P_dc = P_stc * (G / G_stc) * (1 + gamma * (T_cell - 25))
         = (eta_stc * A * G_stc) * (G / G_stc) * (1 + gamma * (T_cell - 25))
         = eta_stc * A * G * (1 + gamma * (T_cell - 25))

Units: eta [-] * A [m2] * G [W/m2] = [W]; gamma [1/degC] * dT [degC] = [-].
So ``P_dc = area * eta_stc * poa_global * (1 + gamma*(T_cell - 25))`` is in W
with no stray factors of 1000. The temperature factor is clamped so that
P_dc >= 0. Net power then is::

    P_net = P_dc * eta_mppt * eta_wiring

Effective efficiency (delivered / incident) is
``P_net / (G * A)`` when G > 0, else 0.0.

Not modelled (see docs/science/pv.md): low-irradiance efficiency roll-off
(PVWatts' linear form assumes eta constant with G), cover-glass reflection at
large angle of incidence, spectral effects, soiling, partial shading, cell
mismatch, MPPT dynamics/tracking error, and the dependence of MPPT efficiency
on power level. All loss factors are constants.
"""

from __future__ import annotations

import math

from rwi.contracts import ArrayConfig, POAIrradiance, PowerEstimate
from rwi.core.errors import ModelConfigurationError
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import utc_now
from rwi.core.units import STC_CELL_TEMPERATURE_C
from rwi.energy.thermal import cell_temperature_c

__all__ = [
    "ASSUMED_AIR_TEMPERATURE_C",
    "ASSUMED_WIND_SPEED_10M_MPS",
    "DEFAULT_ARRAY",
    "MONO_SI_ARRAY",
    "array_power",
    "dc_power_w",
]

ASSUMED_AIR_TEMPERATURE_C = 25.0
"""Fallback when no air temperature is supplied. A round mid-range value for a
BWSC (Darwin-Adelaide, late August) daytime; NOT a measurement. Cell
temperature is only weakly sensitive to this (1:1 in degC)."""

ASSUMED_WIND_SPEED_10M_MPS = 3.0
"""Fallback 10 m wind (m/s) when none is supplied: a typical light-to-moderate
inland value. Not a measurement. Missing wind biases T_cell by a few degC."""

# ---------------------------------------------------------------------------
# Placeholder array configurations. NOT the JU Solar Team vehicle.
# ---------------------------------------------------------------------------

DEFAULT_ARRAY = ArrayConfig(
    name="TESSERACT-2025 array (JUST team's 2025 panel, NOT the 2027 build)",
    area_m2=6.0,
    cell_efficiency_stc=0.27,
    temperature_coefficient_pmax_per_c=-0.0024,
    mppt_efficiency=0.98,
    wiring_efficiency=0.98,
    mounting="horizontal",
    surface_tilt_deg=0.0,
    surface_azimuth_deg=None,
    noct_c=46.0,
    albedo=0.2,
    parameter_source="assumed",
    source_notes=(
        "PARAMETERS FROM 2025 JUST SOLAR CAR, NOT THE 2027 ARRAY. "
        "Read from the team's own repository drive-system at "
        "3-software/host-software/python/cases/cars/tesseract-2025.yaml on 2026-11-02:\n"
        "  area_m2=6.0 (yaml:46, equal to the Challenger 2.4.2 ceiling)\n"
        "  cell_efficiency_stc=0.27 (yaml:47, Oxford PV perovskite-silicon tandem)\n"
        "  temperature_coefficient_pmax_per_c=-0.0024 /degC (yaml:52, a BETTER "
        "temperature coefficient than monocrystalline Si at -0.0037, which is a "
        "real advantage of the tandem cell in the Australian desert)\n"
        "  noct_c=46 (yaml:53)\n"
        "The 2027 layout is DIFFERENT: 338 cells of 182.3 x 96.3 mm in 36 modules "
        "arranged in 5 MPPT groups, with left and right MPPT tables that differ at "
        "module 16 - see 3-software/host-software/shadow-analysis/README.md:62. "
        "This flat-plane model does not represent that geometry at all. The team's "
        "shadow-analysis tool will produce an efficiency_matrix (azimuth x zenith x "
        "cell) that captures per-cell orientation; when it is available, that "
        "matrix is the right input to this model, not a scalar efficiency. "
        "mppt_efficiency=0.98 / wiring_efficiency=0.98 are kept as the team's own "
        "model also uses per-group polynomials rather than a single scalar - "
        "REPLACE with the team's measured efficiency curves. "
    ),
)
"""Primary configuration, now using the team's 2025 car as the least-wrong default.

The 2027 array will replace this once its measurements exist. Until then this
is the configuration most representative of hardware JU Solar Team actually
built and raced, with the explicit caveat that the 2027 panel is a different
layout (338 cells in 5 MPPT groups) that this flat-plane model cannot describe.
See ``source_notes`` and ``docs/science/pv.md``.
"""

# A tag of what we no longer are: a generic Si module, kept for comparison.
_LEGACY_PLACEHOLDER_NOTES = (
        "ALL VALUES ARE PLACEHOLDERS, not a JU Solar Team specification. "
        "area_m2=4.0: generic Challenger-class order of magnitude (BWSC limits the "
        "solar-cell area by class and cell type - check the current regulations); "
        "MEASURE the encapsulated cell area from the built array. "
        "cell_efficiency_stc=0.24: generic laminated high-efficiency "
        "back-contact-silicon module value (cell ~25-26 %, lamination loss); "
        "REPLACE with the cell/module datasheet or an IV-curve flash test of the "
        "finished array. "
        "temperature_coefficient_pmax_per_c=-0.0029 /degC: typical back-contact Si "
        "datasheet value; REPLACE with the datasheet value or a measured Pmax-vs-T "
        "sweep. "
        "mppt_efficiency=0.98: typical peak MPPT efficiency; REPLACE with the "
        "measured efficiency curve of the team's MPPTs (efficiency varies with "
        "power level and input/output voltage). "
        "wiring_efficiency=0.98: lump for harness, connectors, diodes and string "
        "mismatch; REPLACE by measuring array-terminal vs MPPT-input power. "
        "mounting=horizontal, tilt 0: flat array; REPLACE with the measured "
        "surface geometry (curvature of a cambered array means different facets "
        "see different POA; this model treats the array as one plane). "
        "noct_c=48: ArrayConfig default, used only by the 'noct' cell-temperature "
        "model; the default 'faiman' model ignores it - MEASURE cell temperature "
        "with sensors on the array to calibrate any thermal model. "
        "albedo=0.2: generic ground reflectance; irrelevant for a flat array."
)
"""Legacy generic-Si placeholder notes, kept for comparison with the current
TESSERACT_2025 default. Not used anywhere live."""

MONO_SI_ARRAY = ArrayConfig(
    name="PLACEHOLDER generic monocrystalline-Si (PERC) array (ASSUMED, not JST)",
    area_m2=4.0,
    cell_efficiency_stc=0.20,
    temperature_coefficient_pmax_per_c=-0.0037,
    mppt_efficiency=0.97,
    wiring_efficiency=0.98,
    mounting="horizontal",
    surface_tilt_deg=0.0,
    surface_azimuth_deg=None,
    noct_c=45.0,
    albedo=0.2,
    parameter_source="assumed",
    source_notes=(
        "ALL VALUES ARE PLACEHOLDERS for comparison with DEFAULT_ARRAY, not a JU "
        "Solar Team specification. area_m2=4.0 (same area, so differences are "
        "purely cell technology); cell_efficiency_stc=0.20: typical commercial "
        "mono-PERC module efficiency; temperature_coefficient_pmax_per_c=-0.0037: "
        "typical mono-PERC datasheet (-0.35 to -0.40 %/degC); mppt_efficiency="
        "0.97 / wiring_efficiency=0.98: generic. Measurements needed are the same "
        "as for DEFAULT_ARRAY: area, STC efficiency (flash test), Pmax "
        "temperature coefficient, MPPT efficiency curve, harness losses, "
        "mounting geometry (tilt, curvature, orientation)."
    ),
)
"""Comparison placeholder: standard monocrystalline silicon."""


def dc_power_w(
    poa_global_w_m2: float, array: ArrayConfig, cell_temperature_c_: float
) -> float:
    """DC power in W: ``area * eta_stc * G * (1 + gamma * (T_cell - 25))``, >= 0.

    Pure function of irradiance (W/m2), array and cell temperature (degC).
    """
    if poa_global_w_m2 <= 0.0:
        return 0.0
    thermal_factor = 1.0 + array.temperature_coefficient_pmax_per_c * (
        cell_temperature_c_ - STC_CELL_TEMPERATURE_C
    )
    return max(0.0, array.area_m2 * array.cell_efficiency_stc * poa_global_w_m2 * thermal_factor)


def array_power(
    poa: POAIrradiance,
    array: ArrayConfig,
    *,
    air_temperature_c: float | None,
    wind_speed_mps: float | None,
    cell_temperature_model: str = "faiman",
    cell_temperature_override_c: float | None = None,
    wind_height_factor: float | None = None,
) -> PowerEstimate:
    """Net electrical power (W) delivered by ``array`` under ``poa``.

    Chain: POA global (W/m2) -> cell temperature (degC) -> temperature-corrected
    DC power (W, see module docstring) -> x MPPT efficiency x wiring efficiency
    -> net power (W).

    Missing inputs: ``air_temperature_c is None`` falls back to
    :data:`ASSUMED_AIR_TEMPERATURE_C`; ``wind_speed_mps is None`` to
    :data:`ASSUMED_WIND_SPEED_10M_MPS` (a 10 m wind). The returned estimate keeps
    ``air_temperature_c`` / ``wind_speed_mps`` as ``None`` (they were not
    available) and the provenance notes say which fallback was used, so a
    consumer can tell. If a measured cell temperature is available (array
    sensor) pass it as ``cell_temperature_override_c``; the estimate is then
    labelled model ``"override"``.

    ``wind_speed_mps`` is the 10 m provider wind; see
    :mod:`rwi.energy.thermal` for the height adjustment (``wind_height_factor``).
    """
    notes: list[str] = []
    air = air_temperature_c
    wind = wind_speed_mps
    if air is not None and not math.isfinite(air):
        raise ModelConfigurationError(f"air_temperature_c not finite: {air!r}")
    if wind is not None and not math.isfinite(wind):
        raise ModelConfigurationError(f"wind_speed_mps not finite: {wind!r}")
    if air is None:
        air = ASSUMED_AIR_TEMPERATURE_C
        notes.append(f"air temperature MISSING -> assumed {air} degC (not measured)")
    if wind is None:
        wind = ASSUMED_WIND_SPEED_10M_MPS
        notes.append(f"wind speed MISSING -> assumed {wind} m/s at 10 m (not measured)")

    g = poa.poa_global_w_m2
    if cell_temperature_override_c is not None:
        t_cell = float(cell_temperature_override_c)
        model_name = "override"
        notes.append("cell temperature supplied externally (measured or prescribed)")
    else:
        t_cell = cell_temperature_c(
            g,
            air,
            wind,
            model=cell_temperature_model,
            noct_c=array.noct_c,
            wind_height_factor=wind_height_factor,
        )
        model_name = cell_temperature_model

    dc = dc_power_w(g, array, t_cell)
    net = dc * array.mppt_efficiency * array.wiring_efficiency
    eff = net / (g * array.area_m2) if g > 0.0 else 0.0

    if array.parameter_source != "measured":
        notes.append(f"array parameters are '{array.parameter_source}', not measured")
    notes.append(
        "vehicle-array thermal behaviour unvalidated; "
        "no shading/AOI-reflection/MPPT dynamics"
    )
    prov = Provenance(
        provider="rwi",
        dataset="energy.array_power",
        kind=DataKind.PHYSICAL_MODEL,
        retrieved_at_utc=utc_now(),
        valid_time_utc=poa.time_utc,
        model_name=f"pvwatts-style+{model_name}",
        point=poa.provenance.point,
        processing="POA -> T_cell -> P_dc -> x MPPT x wiring",
        notes="; ".join(notes) + f"; POA: {poa.provenance.notes or ''}",
    )
    return PowerEstimate(
        time_utc=poa.time_utc,
        poa=poa,
        cell_temperature_c=t_cell,
        air_temperature_c=air_temperature_c,
        wind_speed_mps=wind_speed_mps,
        dc_power_w=dc,
        net_power_w=net,
        effective_efficiency=eff,
        array=array,
        cell_temperature_model=model_name,
        provenance=prov,
    )
