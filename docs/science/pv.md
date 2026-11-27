# PV energy model (`rwi.energy`)

Status of the module as a whole: **implemented, not validated**. No telemetry
from a real array exists yet; every number below that describes the vehicle is
a placeholder.

Dimensional discipline (enforced by exact-value tests):

| Quantity | Symbol | Unit | Where |
|---|---|---|---|
| irradiance | G | W/m2 | `poa.py` |
| temperature | T | degC | `thermal.py` |
| power | P | W | `array.py` |
| energy | E | Wh | `series.py`, via `rwi.core.units.integrate_power_wh` only |

## 1. Model chain

```
GHI, DNI, DHI [W/m2] + SolarPosition
   | (1) transposition (pvlib get_total_irradiance)
POA_global = POA_direct + POA_sky + POA_ground [W/m2]
   | (2) cell temperature (Faiman | SAPM | NOCT)  [degC]
T_cell = f(POA, T_air, v_10m)
   | (3) PVWatts-style DC power
P_dc = A * eta_stc * POA * (1 + gamma*(T_cell - 25))        [W]
   | (4) electrical losses
P_net = P_dc * eta_mppt * eta_wiring                         [W]
   | (5) trapezoidal integration over sample times
E = integral P_net dt                                        [Wh]
```

### 1.1 Transposition (`poa_irradiance`)

* `POA_direct = DNI * max(cos AOI, 0)`; `cos AOI = cos z cos b + sin z sin b cos(az_sun - az_surf)`
  (z apparent zenith, b tilt). AOI > 90 deg gives exactly 0.
* `POA_ground = GHI * albedo * (1 - cos b) / 2` (isotropic ground; zero when flat).
* `POA_sky` by model name: `isotropic`, `klucher`, `haydavies` (default), `reindl`,
  `king`, `perez` (`allsitescomposite1990`). Sun at or below the horizon
  (`apparent_elevation_deg <= 0`) gives all components exactly 0.0.
* Extraterrestrial DNI (needed by Hay-Davies/Reindl/Perez) from
  `pvlib.irradiance.get_extra_radiation` with `SOLAR_CONSTANT_W_M2` = 1361.
* For a flat plane `POA = DNI cos z + DHI`, which equals GHI only when the
  input components are closed. Independent GHI/DNI/DHI (forecast variables,
  decomposition output) rarely are; a >5 % residual is written to the provenance
  notes. This is a property of the input, not an error of the transposition.

References: Liu & Jordan (1960) Solar Energy 4(3); Klucher (1979) Solar Energy
23(2); Hay & Davies (1980) Proc. 1st Canadian Solar Radiation Data Workshop;
Reindl, Beckman & Duffie (1990) Solar Energy 45(1); Perez et al. (1990) Solar
Energy 44(5); Loutzenhiser et al. (2007) Solar Energy 81, for the model
comparison. Implementation: Holmgren, Hansen & Mikofski (2018) *pvlib python*,
JOSS 3(29), 884.

### 1.2 Vehicle heading to panel azimuth (`surface_azimuth_for_heading`)

* `horizontal`: returns 180.0. Convention only; with tilt 0 the normal is the zenith
  for every azimuth and the ground-view factor is 0, so the result is independent of it
  (tested).
* `fixed_tilt`: returns `array.surface_azimuth_deg` (error if None).
* `vehicle_relative`: **convention implemented**: `array.surface_azimuth_deg` is read as the
  bearing of the panel's facing direction *relative to the vehicle's forward
  direction*, clockwise from above. `None`/0 = tilted about the transverse axis and
  facing the heading; 90 = tilted about the longitudinal axis, right side low,
  facing heading + 90; 270 = left side low; 180 = nose-up, facing the rear. World
  azimuth = (heading + relative) mod 360. Heading `None` raises
  `ModelConfigurationError`. This reuses an existing contract field with a
  different meaning for this one mounting; see "Contract notes" below.

### 1.3 Cell temperature (`cell_temperature_c`)

| Name | Formula | Reference |
|---|---|---|
| `faiman` | `T = T_air + POA / (u0 + u1 v)`, u0 = 25 W/(m2 K), u1 = 6.84 W s/(m3 K) | Faiman (2008), Prog. Photovolt. 16:307-315 |
| `sapm` | `T_back = T_air + POA exp(a + b v)`, `T = T_back + POA/1000 * dT`; a=-2.81, b=-0.0455, dT=0 (insulated back, glass/polymer) | King, Boyson, Kratochvil (2004), SAND2004-3535 |
| `sapm_open_rack` | same, a=-3.56, b=-0.075, dT=3 (open rack, glass/polymer): the free-standing reference | same |
| `noct` | `T = T_air + (NOCT-20)/800 * POA`; NOCT 48 degC unless given | standard IEC 61215 NOCT definition |

**Wind height.** Providers deliver 10 m wind. SAPM coefficients were fitted to
10 m wind, so SAPM variants use it unchanged (factor 1.0). Faiman's coefficients
refer to module-height wind; using 10 m wind directly overstates cooling and
hence overestimates power. Implemented mitigation: for `faiman` the wind is
multiplied by `wind_height_factor`, default `DEFAULT_WIND_HEIGHT_FACTOR` =
ln(1.0/0.03)/ln(10/0.03) = 0.604 (neutral log law, array height 1 m, z0 = 0.03 m). It
is an explicit, overridable assumption (pass 1.0 to disable) that ignores stability, vehicle sheltering
and vehicle speed.

**Why a vehicle array is hotter than a free-standing module.** Coefficients above
describe panels cooled on both faces (open rack) or at least with a rear air gap. A
vehicle array is bonded to a body shell: no rear ventilation, so less heat-loss
area and extra heat from the body. Driving adds forced convection on the top
surface (relative air speed of several m/s to tens of m/s versus an ambient wind of a few m/s), which cools
it, partly offsetting the first effect. The net effect is **unvalidated** until telemetry
(cell temperature sensors, air temperature, vehicle speed) exists. Vehicle speed
is not an input to the thermal model.

### 1.4 DC power (`dc_power_w`, `array_power`)

With `eta_stc = P_stc / (A G_stc)`, `G_stc` = 1000 W/m2 (`STC_IRRADIANCE_W_M2`):

```
P_dc = P_stc (G/G_stc) (1 + gamma (T_cell - 25))
     = eta_stc A G_stc (G/G_stc) (1 + gamma (T_cell - 25))
     = A eta_stc G (1 + gamma (T_cell - 25))
```

Units: m2 * (-) * W/m2 * (-) = W. Clamped to >= 0. Reference: Dobos (2014),
*PVWatts Version 5 Manual*, NREL/TP-6A20-62641. Net: `P_net = P_dc * eta_mppt * eta_wiring`.
`effective_efficiency = P_net / (G A)` for G > 0 else 0.0.

Missing weather: `air_temperature_c=None` falls back to 25 degC, `wind_speed_mps=None`
to 3 m/s (10 m). The returned `PowerEstimate` keeps `None` for the missing fields
and the provenance notes read "MISSING -> assumed ...". A measured cell temperature
may be injected with `cell_temperature_override_c` (model name "override").

### 1.5 Energy (`power_series`, `integrate_energy`, `energy_along_trajectory`)

Trapezoidal rule on net power versus seconds since the first sample, converted
J to Wh by `integrate_power_wh`. Window = first to last sample, one sample gives
0.0 Wh, decreasing times raise `ValueError`. Cumulative energy per sample is
`energy_along_trajectory`. Sparse sampling (e.g. 25 km trajectory spacing) smooths
sunrise/sunset and cloud transitions; the largest gap is in the provenance notes.
No uncertainty is attached (`uncertainty_wh=None`) because none has been measured.

### 1.6 Validation helper (`compare_with_measured`)

Bias (mean model - measured), MAE, RMSE, relative bias on supplied pairs (None/NaN dropped
and counted). Works on any pairs; no telemetry exists yet, so it has only been exercised on synthetic
numbers.

### 1.7 Camp-site charging forecast (`rwi.energy.camp`, ADR 0015)

This section covers one candidate overnight stop. Choosing the stop is the strategy
tool's job.

**Windows** (`charge_windows`). Times come from `rwi.regulations.SCHEDULE`.

* Evening: from `daily_stop` (17:00) to the earlier of SPA sunset and the 19:00 lockout start.
* Morning: from the later of SPA sunrise and the 06:30 lockout end to `daily_start` (08:00).
* Sun events are taken at geometric elevation −0.8333° (Reda & Andreas 2004, via pvlib).
* Each end is labelled with what limited it.

**Clear-sky energy** (`window_clear_sky`).

* Ineichen-Perez GHI, DNI and DHI, sampled every 60 s and integrated with the trapezoid rule
  to Wh/m².
* Labelled `CLEAR_SKY_MODEL`.
* After sunset the value is exactly 0.0 (rule 5).

**Forecast energy** (`window_forecast_energy`).

* Open-Meteo steps are preceding-hour means. Hour `(t−1h, t]` contributes
  `mean × 1 h × E_cs(hour ∩ window) / E_cs(hour)`, which is clear-sky-index interpolation.
* An hour with zero clear-sky energy contributes 0.
* If an overlapping hour is missing, that variable is `None`; no partial sum is reported.
* If the radiation is instantaneous, forecast energy is not computed.

**Gusts** (`window_gusts`).

* The peak 10 m gust over the overlapping provider hours.
* `gust_warning` is set only when `RWI_CAMP_GUST_WARNING_M_S` is configured.
* The 10 m gust is higher than the gust at array height, so the flag errs on the safe side.

**Horizon cloud** (`horizon_points`, `horizon_cloud`).

* Points at `RWI_CAMP_HORIZON_OFFSETS_KM` along the sunset azimuth (west) and the next sunrise
  azimuth (east), on a spherical earth.
* Total, low, mid and high cloud is taken at the step nearest the event, within half a step.

**Dew at dawn** (`dew_hours`). The steps, for each forecast hour from sunset to sunrise:

1. Saturation vapour pressure at the dew point: Magnus, 6.1094 exp(17.625 T / (T + 243.04))
   hPa (Alduchov & Eskridge 1996).
2. Clear-sky emissivity: 1.24 (e/T)^(1/7) (Brutsaert 1975).
3. Cloud correction: ε = ε_clr (1 − c) + c (Crawford & Duchon 1999). Here c is NWP total
   cloud, not the solar-deficit cloud fraction Crawford & Duchon derived.
4. Downwelling longwave: IR = ε σ T_air⁴.
5. Array temperature from `pvlib.temperature.faiman_rad` (Driesse et al. 2022).
   * POA is 0.
   * The u0 = 25 and u1 = 6.84 coefficients come from Faiman 2008.
   * Emissivity is 0.88 and sky view is 1.0.
   * Wind is the 10 m value times the Faiman height factor (1.3).
6. Risk is flagged when `T_array − T_dew ≤ RWI_CAMP_DEW_MARGIN_C`. There is no default margin.

**Limitations**

* The array is treated as horizontal, but the real array is tilted when raised.
* Solar heating after sunrise is ignored (POA 0), so dew risk is overstated late in the
  morning window.
* Offsets, gust limit and dew margin are not calibrated.
* Forecasts reach only about 384 h ahead; past that, a candidate is clear-sky only.

**References**

* Brutsaert, W. (1975), Water Resour. Res. 11(5), 742–744.
* Alduchov, O. A. & Eskridge, R. E. (1996), J. Appl. Meteor. 35, 601–609.
* Crawford, T. M. & Duchon, C. E. (1999), J. Appl. Meteor. 38, 474–480.
* Driesse, A., Stein, J. S. & Theristis, M. (2022), pvlib `faiman_rad`.
* Faiman, D. (2008), Prog. Photovolt. 16, 307–315.
* Reda, I. & Andreas, A. (2004), Solar Energy 76, 577–589.

## 2. `DEFAULT_ARRAY` assumptions (all `parameter_source = "assumed"`)

These are generic placeholders, **not** the JU Solar Team vehicle.

| Parameter | Value | Status | Measurement that replaces it |
|---|---|---|---|
| area_m2 | 4.0 | assumed | cell area of the built, encapsulated array (and BWSC class limit check) |
| cell technology / eta_stc | high-efficiency back-contact Si, 0.24 | assumed | cell/module datasheet or flash-test IV curve of the finished array |
| gamma (Pmax) | -0.0029 /degC | assumed | datasheet value or measured Pmax vs T sweep |
| mppt_efficiency | 0.98 | assumed | measured MPPT efficiency curve vs power and voltage |
| wiring_efficiency | 0.98 | assumed | array-terminal vs MPPT-input power; includes string mismatch and diodes |
| mounting | horizontal, tilt 0 | assumed | CAD/3-D scan of the array surface (camber, per-facet normals) |
| noct_c | 48 degC (only for `noct`) | assumed | cell temperature sensors on the array |
| albedo | 0.2 (irrelevant when flat) | assumed | n/a for a flat array |

`MONO_SI_ARRAY` (0.20, -0.0037 /degC, MPPT 0.97, NOCT 45) is a second assumed
configuration for comparison. Remember that eta_stc here is a *module* value at
STC; it is treated as constant over irradiance.

## 3. Known weaknesses

* No partial shading, no cell-string mismatch (a single plane and a single T_cell are used).
* Curved/cambered arrays are treated as one plane.
* No angle-of-incidence reflection loss (cover glass); optimistic at AOI > 60 deg.
* No spectral correction, no soiling, no degradation, no low-light efficiency roll-off.
* Wind: 10 m provider wind versus the 1 m reality (see 1.3); the conversion is an assumption.
* Vehicle motion (forced convection) and body heating are not modelled; thermal model unvalidated.
* No MPPT dynamics, tracking error or voltage-window limits; efficiencies are constants.
* Transposition models were validated on fixed free-standing panels, not a moving vehicle.
* Clear-sky or forecast irradiance errors dominate: the model cannot be better than its input
  GHI/DNI/DHI.
* Albedo is uniform and fixed.

## 4. Validation status per function

| Function | Status |
|---|---|
| `poa_irradiance` | tested |
| `surface_azimuth_for_heading` | tested |
| `cell_temperature_c` (faiman, sapm, sapm_open_rack, noct) | tested |
| `log_wind_height_factor` / `DEFAULT_WIND_HEIGHT_FACTOR` | implemented, not validated |
| `dc_power_w` | tested |
| `array_power` | tested |
| `DEFAULT_ARRAY`, `MONO_SI_ARRAY` | implemented, not validated |
| `power_series` | tested |
| `integrate_energy` | tested |
| `energy_along_trajectory` | tested |
| `compare_with_measured` | tested |
| `charge_windows`, `window_clear_sky`, `window_forecast_energy`, `window_gusts` | tested |
| `horizon_points`, `horizon_cloud` | tested |
| `saturation_vapour_pressure_hpa`, `downwelling_longwave_w_m2`, `dew_hours` | tested (references); dew model not validated |

"tested" means unit tests with exact or hand-computed references pass (these check the
implementation against the stated equations and, for cell temperature, hand-computed
published coefficients). It does **not** mean the model reproduces a real vehicle: nothing
here is "validated against real data". The thermal model for a vehicle-mounted array is
the least certain element.

## 5. Contract notes

* `ArrayConfig.surface_azimuth_deg` is documented as `None` for `vehicle_relative`;
  this module additionally accepts a number there, read as a bearing relative to the heading (1.2).
  A dedicated field would be cleaner.
* `ArrayConfig` has no fields for soiling, glass reflection, string mismatch or module
  thermal parameters (Faiman u0/u1); these are module constants.
* Extra keyword-only parameters were added beyond the binding signatures (defaults keep
  the binding behaviour): `cell_temperature_c(..., wind_height_factor)`;
  `array_power(..., cell_temperature_model, cell_temperature_override_c, wind_height_factor)`.
