# NASA Earthdata datasets for BWSC race strategy (2026-11-03)

Catalogued via the NASA Earthdata CMR MCP server
(`https://cmr.earthdata.nasa.gov/mcp/v1`). Every row below is a collection
whose metadata the MCP returned AND whose granule-availability was verified
against BWSC-2025 race week at a route point. Collections that declare
global coverage but have no granules over Australia for that week are left
out.

Scope: the Darwin -> Adelaide corridor (bounding box
POLYGON(128 -36, 142 -36, 142 -12, 128 -12, 128 -36)) and race-week dates
(2027-08-22 to 2027-08-29 for the live race; 2025-08-24 to 2025-08-30 used
as a surrogate for availability-checking because 2027 granules don't exist
yet). All collections marked `ongoing` publish granules on a rolling basis,
so race-week 2027 data will be there when needed.

Access on all collections requires a free NASA Earthdata login
(urs.earthdata.nasa.gov) and the `earthaccess` Python library, which is
now a declared dependency in `pyproject.toml`.

## Dataset catalogue

### Cloud + radiation (primary meteorological input)

| Field | SatCORPS Himawari-9 SH |
|---|---|
| CMR id | `C3880513838-LARC_CLOUD` |
| short name | `CER_GEO_Ed4_HIM09_SH` V01.4 |
| doi | `10.5067/HIM09/CERES/GEO_ED4_SH_V01.4` |
| spatial resolution | 2 km nadir, subsampled to 6 km |
| temporal resolution | 1 hour |
| coverage | 2022-11-30 -> 2026-07-31 verified in CMR; publication latency observed on 2026-11-07 was ~99 days, so this is NOT a real-time product. |
| variables | cloud fraction, cloud optical depth, cloud top height, cloud top temperature, liquid/ice water path, radiative properties |
| format | netCDF-4 |
| access | Earthdata login via `earthaccess`; the native asdc S3/HTTPS bucket |
| race-strategy use | **Retrospective cross-check against our own Heliosat retrieval, not a live input.** The ~99-day publication latency rules it out for race-day forecasting; the live path stays `noaa-himawari9` raw tiles via `rwi.satellite.tiles`. Its value is as an independent cloud-property retrieval (phase, optical depth, top height) against which our `rwi.satellite` output can be audited after the fact, and as a bias-correction input against archived ECMWF for the corridor. |

| Field | CERES SYN1deg 1-Hour |
|---|---|
| CMR id | `C3181056140-LARC_CLOUD` |
| short name | `CER_SYN1deg-1Hour_Terra-Aqua-NOAA20` Edition4B |
| doi | `10.5067/Terra-Aqua-NOAA20/CERES/SYN1deg1Hour_L3.004B` |
| spatial resolution | 1 degree (~110 km) |
| temporal resolution | 1 hour |
| coverage | 2000-03-01 -> ongoing (global) |
| variables | surface + TOA shortwave + longwave fluxes, cloud fractions, cloud radiative effects |
| format | HDF4 |
| access | asdc.earthdata.nasa.gov; granule-URL example at `https://data.asdc.earthdata.nasa.gov/.../CER_SYN1deg-1Hour_Terra-Aqua-NOAA20_Edition4B_415412.20250824` |
| race-strategy use | **Independent ground truth for our ECMWF-vs-NASA-POWER retrospective** (same satellite source POWER uses but Edition4B is newer than what POWER serves). The 1-degree grid is coarse for one point along the route but adequate for climatological bias correction, which is what the strategy layer wants on a weekly basis. |

| Field | MERRA-2 M2T1NXRAD |
|---|---|
| CMR id | `C1276812851-GES_DISC` |
| short name | `M2T1NXRAD` V5.12.4 |
| doi | `10.5067/Q9QMY5PBNV1T` |
| spatial resolution | 0.625 deg x 0.5 deg (~55 x 55 km) |
| temporal resolution | 1 hour, time-stamped at :30 past the hour |
| coverage | 1980-01-01 -> ongoing (global, ~3 week latency) |
| key variables | `SWGDN` (surface incoming shortwave), `LWGAB` (longwave absorbed), `CLDTOT` (total cloud), `ALBEDO` |
| format | netCDF-4 |
| access | gesdisc.earthdata.nasa.gov; granule-URL example `https://data.gesdisc.earthdata.nasa.gov/data/MERRA2/M2T1NXRAD.5.12.4/2025/08/MERRA2_400.tavg1_2d_rad_Nx.20250824.nc4` |
| race-strategy use | **Fourth independent product for triangulating ECMWF, NASA POWER and SILO.** MERRA-2 is NASA's own reanalysis (GEOS-5 model, independent of ECMWF). ~3-week latency means it is NOT useful for live race-day forecasting; it is a post-hoc calibration source and the one that would turn our three-way triangulation into a four-way one. |

### Aerosol + atmosphere

| Field | MODIS MAIAC AOD |
|---|---|
| CMR id | `C2324689816-LPCLOUD` |
| short name | `MCD19A2` V061 |
| spatial resolution | 1 km |
| temporal resolution | daily, Terra+Aqua combined |
| coverage | 2000-02-24 -> ongoing |
| key variables | AOD at 0.47 um and 0.55 um, column water vapour, smoke injection height, QA flags |
| race-strategy use | **Direct input to the clear-sky Linke-turbidity we use in `rwi.solar.clearsky`.** The Red Centre is dust-prone; smoke/dust loading drops GHI 5-20 % when heavy. Lets us flag a race-day Linke correction rather than running on the climatological default. |

### Precipitation

| Field | GPM IMERG |
|---|---|
| CMR ids | `C2723758340-GES_DISC` (Early, 4 h latency), `C2723754845-GES_DISC` (Late, 14 h), `C2723754847-GES_DISC` (Final, ~4 month) |
| short names | `GPM_3IMERGHHE` / `GPM_3IMERGHHL` / `GPM_3IMERGHH` V07 |
| spatial resolution | 0.1 deg x 0.1 deg (~10 km) |
| temporal resolution | 30 minutes |
| coverage | 1998 -> ongoing |
| key variables | precipitation rate (mm/h), precipitation phase, quality index |
| race-strategy use | **A rare-event rain warning along the route.** August is dry season for the whole corridor but convective pop-up systems do appear, and the Alice-Springs-to-Coober-Pedy stretch has unforgiving terrain. The Early (4 h latency) feed is live-usable; Final is for post-race calibration. |

## What is NOT in this catalogue

* **BoM observatory 1-min GHI** (Alice Springs 015540 and other BSRN-class
  stations): the ground truth, but not on NASA Earthdata. BoM Climate Data
  Online hands these out as per-station CSV; no stable scriptable URL. A
  one-off manual download remains the gap.
* **Himawari-9 raw full-disk tiles**: already ingested by our own
  `rwi.satellite` pipeline directly from the `noaa-himawari9` S3 bucket.
  Preferred over the SatCORPS re-processed product for live nowcasting
  because it is 10 min resolution and we control the processing.
* **Live ECMWF forecasts**: via Open-Meteo. Not NASA data.
* **CERES EBAF monthly** and **ES4 fluxes**: too coarse for a race strategy
  (monthly means) or too high in the atmosphere (TOA, not surface).

## Setup for an actual download

The library and config needed to pull any of the above is:

```bash
# one-time per machine
./.venv/bin/python -c "import earthaccess; earthaccess.login(persist=True)"
# this prompts for Earthdata username+password and writes ~/.netrc.
```

After that, `earthaccess.search_data(short_name=..., temporal=..., bounding_box=...)` returns granules and `earthaccess.download(...)` fetches them with the Earthdata token automatically.

Validation and strategy scripts under `scripts/validation/` can import
earthaccess directly; it is a declared dependency.
