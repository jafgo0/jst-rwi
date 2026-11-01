# ADR 0003: pvlib as the solar-geometry and irradiance authority

Status: Accepted
Date: 2026-11-01

## Context

Everything GPS-dependent in the platform rests on one fact: where the Sun is, for this position and
this instant. A wrong sign or a 12-hour offset here silently corrupts irradiance, PV power, the
daily driving window (no driving after sunset, Regulations 3.21.4 and 3.24.8), the dashboard's Sun
marker and every energy forecast. The master specification makes this a hard requirement (section 6)
and prescribes a night-time test at Jonkoping and an Australia test independent of the computer's
location (section 15).

Solar geometry, clear-sky models, decomposition and transposition are mature, published, peer-reviewed
algorithms with reference implementations. Hand-written approximations (declination by a cosine,
equation of time by a sinusoid, "sunrise = 6 am") are typically accurate to a degree or several minutes,
have known failure modes at high latitude and near the horizon, and can only be validated by comparing
them with the reference anyway.

## Decision

1. All solar position, sunrise/sunset/transit, air mass, clear-sky irradiance, GHI decomposition
   (Erbs by default; DISC and DIRINT selectable), plane-of-array transposition (Hay-Davies by default)
   and cell-temperature models are obtained from **pvlib** (>= 0.11).
2. Position uses the **NREL SPA** algorithm (Reda and Andreas 2004) through pvlib; clear-sky uses
   **Ineichen-Perez** with Linke turbidity from pvlib's bundled climatology (no network at run time).
   These are the choices implemented in `src/rwi/solar/` as of 2026-11-01.
3. No other module computes solar angles. `rwi.solar` returns `SolarPosition`,
   `SolarDayEvents`, `IrradianceComponents`; everyone else consumes those.
4. The wrapper adds the platform's own invariants, not new physics: aware UTC input only, `apparent_elevation_deg <= 0`
   gives exactly zero GHI, DNI and DHI, output tagged `DataKind.CLEAR_SKY_MODEL`, polar day and night
   represented as `None` event times, local day from the position's timezone.
5. The dashboard's 3D or 2D Sun visualisation, if any, must call the same API endpoints (master spec section 6).
6. Scientific tests use external reference values (`@pytest.mark.science`), not values produced by
   the same code.

## Consequences

Positive
* Correctness inherited from a widely used, tested library with documented uncertainty
  (SPA: +/-0.0003 degrees as stated by its authors).
* Vectorised computation over trajectories and days.
* The same library provides the PV chain (`energy`), so conventions (azimuth clockwise from north,
  zenith/elevation) are consistent.

Negative / risks
* New hard dependency (pvlib pulls in pandas, NumPy, SciPy and h5py for the turbidity file); version changes
  can change results slightly. `pyproject.toml` currently gives only lower bounds (no lock file exists yet), so
  the version must be pinned before any validation run is recorded as evidence.
* Decomposition models (Erbs and others) are empirical, developed on mid-latitude Northern Hemisphere data
  hourly; their skill over the Australian outback in winter is **not validated here**. They estimate
  DNI/DHI from GHI; they do not measure them.
* The Linke-turbidity climatology is monthly and coarse; clear-sky accuracy over the outback (dust, smoke)
  is unvalidated. A clear-sky value is a bound, not a forecast.
* pvlib is a library, not a certified reference; independent cross-checks (published NOAA or USNO
  values for named places and times) are still required in tests.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Hand-rolled astronomy (Meeus or a simple declination model) | Adds a code path that must itself be validated against pvlib or NOAA; no gain. Allowed only as an independent cross-check in tests, never in production. |
| `astral` / `ephem` / `skyfield` for position, own code for irradiance | Position is fine, but irradiance, transposition and cell models would still need a second library; mixing conventions is where bugs live. |
| Provider-supplied solar data only (`is_day`, `terrestrial_radiation`) | Providers' day/night flags are not a substitute for geometry at the exact position and time; the contract forbids it. Also unavailable offline. |
| NREL SAM / pySAM for the PV chain | Heavier, licence and binary dependency; pvlib covers the needed models. May be used later as an independent cross-check. |
| Physical lookup of clear-sky from a satellite product | Needs data access and licences; not available offline. |
