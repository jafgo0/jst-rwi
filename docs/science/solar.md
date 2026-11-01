# Solar geometry and irradiance (`rwi.solar`)

Owner: solar. Code: `src/rwi/solar/`. Tests: `tests/unit/test_solar_*.py`.
All angles are degrees, irradiance W/m2, times aware UTC. Azimuth is clockwise from
true north. pvlib 0.16.1 is the only numerical engine; nothing is hand-rolled except
a spherical-trigonometry identity (declination, below) and a secant polish of
sunrise/sunset times.

## 1. Models

### 1.1 Solar position - NREL SPA
`rwi.solar.geometry.solar_position(s)` calls `pvlib.solarposition.get_solarposition(method="nrel_numpy")`,
the pure-numpy port of the NREL Solar Position Algorithm.
* Reda, I. and Andreas, A. (2004), *Solar position algorithm for solar radiation
  applications*, Solar Energy 76(5), 577-589; corrigendum Solar Energy 81(6), 838 (2007).
  Stated uncertainty +/-0.0003 deg for years -2000..6000.
* `solar_positions` is vectorised: one pvlib call over a `DatetimeIndex`, then a loop
  only to build the pydantic result objects.

Fields filled: `elevation_deg` / `zenith_deg` (geometric, no refraction),
`apparent_elevation_deg` / `apparent_zenith_deg` (with refraction), `azimuth_deg`,
`equation_of_time_min` (SPA), `air_mass`, and `declination_deg`.
`declination_deg` is not returned by `get_solarposition`; it is recovered with the exact
identity `sin(dec) = sin(lat) sin(el) + cos(lat) cos(el) cos(az)` applied to SPA's
topocentric elevation and azimuth (checked against the NREL published topocentric
declination to 0.01 deg in the reference test).

`air_mass`: Kasten and Young (1989), *Revised optical air mass tables and approximation
formula*, Applied Optics 28(22), 4735-4738, via `pvlib.atmosphere.get_relative_airmass`
on the **apparent** zenith. It is `None` whenever `apparent_elevation_deg <= 0`.

### 1.2 What is passed to SPA, and why
| Input | Value | Why |
|---|---|---|
| `altitude` | `point.elevation_or_zero_m` (0 m, recorded in provenance notes, if elevation unknown) | Enters SPA only via the topocentric parallax term (< 0.003 deg). |
| `pressure` | Caller's `pressure_pa` if given, else ICAO standard-atmosphere pressure for the altitude (`pvlib.atmosphere.alt2pres`, e.g. 94 947 Pa at 545 m) | Only the refraction correction uses pressure. `GeoPoint` has no pressure, so the standard atmosphere is the best available default; a provider surface-pressure value should be passed when known. |
| `temperature` | Caller's `temperature_c` if given, else 12 degC (pvlib default) | Only the refraction correction uses temperature. |
| `delta_t` | 67.0 s (pvlib default) | Contributes < 0.001 deg over 2026-2030; not worth a table lookup. |

The optional `pressure_pa` / `temperature_c` keyword arguments on `solar_position(s)` are
an extension of the contract signature (additive, keyword-only, default None).

### 1.3 Refraction convention
* `elevation_deg` is geometric: the true direction of the Sun's centre, no atmosphere.
* `apparent_elevation_deg` = geometric + SPA refraction correction (SPA's refraction
  formula, ~0.57 deg at the horizon for standard pressure and ~10 degC). SPA applies the
  correction only above about -0.83 deg, so the apparent elevation is discontinuous
  (a jump of ~0.57 deg) where it switches on.
* **Day/night is decided by `apparent_elevation_deg > 0`** (`SolarPosition.is_daylight`),
  and every night rule in this package uses it. Consequence: the band where the geometric
  elevation is in (-0.57, 0] but the apparent elevation is positive counts as daylight;
  the extraterrestrial horizontal irradiance (geometric zenith) is 0 there while
  the clear-sky GHI is tiny but positive (Ineichen is fed the apparent zenith). The
  clearness index is reported as `None` when extraterrestrial irradiance < 1 W/m2.
* Sunrise/sunset events use SPA's own convention: the Sun's geometric centre at
  **-0.8333 deg** (34' refraction + 16' semi-diameter). This differs from
  `is_daylight` by a few minutes at the horizon; they answer different questions.

### 1.4 Sunrise, sunset, solar noon
`solar_day_events` uses `pvlib.solarposition.sun_rise_set_transit_spa`
(Reda and Andreas 2003, NREL/TP-560-34302) for the **local** day containing `when`.
Local day = IANA zone from `rwi.core.timebase.timezone_for_point` (UTC over open ocean),
never the machine zone or a fixed offset; DST transition days work.

* Polar day / polar night: pvlib returns NaT for rise and set; we return `None` for both,
  and decide polar day vs night from the geometric elevation at transit (> -0.8333 deg is
  polar day, `day_length_s = 86400`; otherwise polar night, `day_length_s = 0`).
* `transit_elevation_deg` is the **geometric** elevation at solar noon.
* **Polish step.** pvlib's rise/set routine uses the ephemeris of the UTC date label; when
  the event falls on a different UTC date from the local date (all Australian sunrises,
  UTC+9:30..+10:30), the declination used is up to a day out. Measured: the geometric
  elevation at pvlib's sunrise was -1.035 deg at Alice Springs and -0.974 deg at Darwin
  (target -0.8333) - about one minute of time; sunsets and Swedish events were exact.
  We therefore refine sunrise and sunset with up to four secant steps on the SPA elevation
  (`_refine_crossing`, limited to a 15 min correction). After the polish the target is
  met to < 0.001 deg (tested to 0.01 deg). Solar noon is pvlib's transit, unmodified.
* `day_length_s` = sunset - sunrise.

### 1.5 Clear-sky irradiance - Ineichen-Perez
`rwi.solar.clearsky` uses `pvlib.clearsky.ineichen(..., perez_enhancement=True)`.
* Ineichen, P. and Perez, R. (2002), *A new airmass independent formulation for the
  Linke turbidity coefficient*, Solar Energy 73(3), 151-157.
* Perez, R., Ineichen, P., Seals, R., Zelenka, A. (1990), *Making full use of the
  clearness index for parameterizing hourly insolation conditions*, Solar Energy 45(2), 111-114,
  and Perez, R. et al. (2002), *A new operational model for satellite-derived irradiances*,
  Solar Energy 73(5), 307-317, for the DNI enhancement that pvlib exposes as `perez_enhancement`.
* Linke turbidity: `pvlib.clearsky.lookup_linke_turbidity` with pvlib's bundled
  `LinkeTurbidities.h5` (SoDa/Remund climatology, monthly, interpolated in time).
  **Verified to work with no network access** (Alice Springs, August: TL = 2.86;
  Jonkoping, January: TL = 2.05). If the lookup ever raised, the code falls back to a
  constant TL = 3.0 and writes `CONSTANT FALLBACK` into the provenance notes; this path
  has not been exercised in practice.
* Inputs: apparent zenith and absolute airmass (relative airmass x pressure/101325 Pa)
  from the same SPA call; altitude; extraterrestrial normal irradiance from
  `pvlib.irradiance.get_extra_radiation(method="nrel")`. Pressure is the standard
  atmosphere for the altitude unless supplied. **Temperature plays no role in Ineichen**;
  it only affects the refraction correction of the apparent zenith.
* Output is clipped to the `IrradianceComponents` bounds, with 0 <= DHI <= GHI. When
  `apparent_elevation_deg <= 0` the three components are set to exactly `0.0` before the
  object is built. `kind` is always `DataKind.CLEAR_SKY_MODEL`; provenance names the model,
  pvlib version, pressure, temperature and turbidity source.
* `extraterrestrial_horizontal_w_m2` = `get_extra_radiation` x cos(**geometric** zenith),
  clipped at 0.
* Sanity numbers (Alice Springs, 2027-08-24 03:00Z): GHI 858 W/m2, DNI 925, DHI 100,
  kt 0.78; closure `DNI cos z + DHI` = 858.0.

### 1.6 GHI decomposition
`rwi.solar.decomposition.decompose_ghi` turns a (forecast) GHI into DNI/DHI:
* `"erbs"` (default) - Erbs, D.G., Klein, S.A., Duffie, J.A. (1982), *Estimation of the
  diffuse radiation fraction for hourly, daily and monthly-average global radiation*,
  Solar Energy 28(4), 293-302; `pvlib.irradiance.erbs`.
* `"disc"` - Maxwell, E.L. (1987), *A quasi-physical model for converting hourly global
  horizontal to direct normal insolation*, SERI/TR-215-3087; `pvlib.irradiance.disc`;
  DHI = GHI - DNI cos z. pvlib's default pressure (101 325 Pa) is used for DISC's airmass.
* `"dirint"` - Perez, R., Ineichen, P., Maxwell, E., Seals, R., Zelenka, A. (1992),
  *Dynamic global-to-direct irradiance conversion models*, ASHRAE Trans. 98(1), 354-369;
  `pvlib.irradiance.dirint` with `use_delta_kt_prime=False` and no dew point, because a
  single value has no time-series context. This is only the static part of DIRINT.

Behaviour: geometric zenith for cos z and kt; apparent elevation <= 0 or GHI = 0 gives
exact zeros (a provider GHI > 5 W/m2 at night is zeroed and flagged `QualityFlag.OUT_OF_RANGE`);
negative, NaN or > 1600 W/m2 input raises `ValueError`; erbs/disc/dirint cap DNI to 0 at
zenith > 87 deg (pvlib `max_zenith`), so DHI = GHI there.
Provenance: a **new** `Provenance` is built from the input; `kind` is preserved (forecast stays
forecast) and `processing` becomes `"<model>_decomposition"` (appended with `+` if the input already
had a processing step). **Deviation from "preserve kind":** an `OBSERVATION` input is relabelled
`DERIVED`, because only GHI was measured and DNI/DHI are model estimates.

## 2. Known limitations
* **Linke turbidity is a monthly climatology**, not today's aerosol load. Smoke, dust or
  volcanic aerosol (relevant on the BWSC route: bushfire smoke, dust storms) will make real
  clear-sky irradiance lower than modelled, mostly in DNI. Clear sky is an upper-bound
  estimate under *typical* aerosol, not under measured conditions.
* Ineichen-Perez is a broadband, clear-sky-only model; no water-vapour input (the Linke
  factor absorbs it). Typical published clear-sky RMSE is 3-6 % of GHI and larger for DNI.
* Erbs/DISC/DIRINT are hourly-mean correlations fitted mostly to mid-latitude Northern
  Hemisphere data. Skill for sub-hourly steps, for GHI forecasts that are themselves
  smoothed by the NWP model, at very low Sun, and in Australian desert dust conditions is
  unquantified here. Forecast GHI from a model is an *interval mean* while the decomposition
  is applied at an instant; the caller owns that mismatch.
* Standard-atmosphere pressure ignores weather; refraction error from a 20 hPa pressure
  error is ~0.003 deg near the horizon. Negligible except for sunrise timing (seconds).
* Near-horizon (apparent elevation within about 0.6 deg of 0) the refraction discontinuity
  means tiny positive clear-sky irradiance can coexist with zero extraterrestrial
  horizontal irradiance (geometric); see 1.3.
* Atmospheric refraction at the horizon is highly variable in reality (inversions); SPA
  uses a standard formula.
* Sunrise/sunset do not account for terrain horizon (mountains), only the flat
  astronomical horizon. Alice Springs and the MacDonnell Ranges can delay real sunrise.
* The secant polish of rise/set times has a 15 min safeguard; grazing near-polar events
  (slope < 1e-5 deg/s) keep pvlib's raw value.
* Unknown elevation is replaced by 0 m for the parallax and pressure terms, and says so in
  the provenance notes.

## 3. Validation status
Vocabulary: not started / researching / implementing / implemented, not validated /
tested / validated against real data / operationally ready.

| Function | Status | Evidence |
|---|---|---|
| `solar_position`, `solar_positions` | **tested** | Unit tests, including the NREL SPA published example (zenith 50.11162, azimuth 194.34024 deg, tolerance 0.001 deg), equator-equinox zenith, solar-noon azimuth, night and Australia checks. Not compared with a ground sky-camera or tracker data. |
| `solar_day_events` | **tested** | Day-length checks (Jonkoping 18.1 h June, 6.5 h December), polar day/night at 78.2 N, local-day boundaries, DST day, elevation at rise/set = -0.8333 deg. Not compared with an external almanac (USNO/NOAA) table - that comparison has not been done. |
| `clear_sky_irradiance`, `clear_sky_series` | **tested** | Night zero, bounds, GHI <= ET, noon peak, closure, turbidity sensitivity, range check against published clear-sky peaks. **Not validated against pyranometer data**; no ground observations for the BWSC corridor have been compared. |
| `extraterrestrial_horizontal_w_m2` | **tested** | Equinox-noon equator value matches the solar constant within 15 W/m2. |
| `decompose_ghi` (erbs) | **tested** | Closure, clear/overcast behaviour, night/zero rules, independent hand-evaluation of the Erbs polynomial. Not validated against BSRN/BoM measured DNI/DHI. |
| `decompose_ghi` (disc, dirint) | **implemented, not validated** | Closure and smoke tests only; DIRINT is used in a degenerate single-point form. |

"Tested" means the unit tests pass; it does **not** mean the outputs have been compared
against real measurements.
