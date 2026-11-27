# Provider fixtures

Real responses captured on 2026-11-01/02 (UTC) with `curl -A "jst-rwi-research/0.1 (JU Solar Team)"`; replayed
by `respx` in `tests/unit/test_provider_*.py` and `test_quality_checks.py`. Unit tests never touch the network.

| File | Request (all `timezone=UTC`) |
|---|---|
| `openmeteo_forecast_darwin_ifs025.json` | `api.open-meteo.com/v1/forecast?latitude=-12.4634&longitude=130.8456&models=ecmwf_ifs025&start_hour=2026-11-02T01:00&end_hour=2026-11-02T05:00&hourly=temperature_2m,dew_point_2m,relative_humidity_2m,pressure_msl,wind_speed_10m,wind_direction_10m,wind_gusts_10m,cloud_cover,shortwave_radiation,direct_normal_irradiance,diffuse_radiation,is_day` |
| `openmeteo_archive_darwin_ifs.json` | `archive-api.open-meteo.com/v1/archive?...&models=ecmwf_ifs&start_date=2026-10-20&end_date=2026-10-20&hourly=temperature_2m,dew_point_2m,pressure_msl,wind_speed_10m,wind_gusts_10m,shortwave_radiation,direct_normal_irradiance,diffuse_radiation,is_day` |
| `openmeteo_ensemble_darwin_ifs025.json` | `ensemble-api.open-meteo.com/v1/ensemble?...&models=ecmwf_ifs025&start_hour=2026-11-02T03:00&end_hour=2026-11-02T05:00&hourly=temperature_2m,wind_speed_10m,pressure_msl` |
| `openmeteo_meta_ecmwf_ifs025.json` | `api.open-meteo.com/data/ecmwf_ifs025/static/meta.json` (model Metadata API) |
| `openmeteo_meta_bom_access_global.json` | `api.open-meteo.com/data/bom_access_global/static/meta.json`, captured 2026-11-27: run 2025-06-26 18Z, data end 2025-06-30 07Z (ADR 0013) |
| `openmeteo_meta_bom_access_global_ensemble.json` | `api.open-meteo.com/data/bom_access_global_ensemble/static/meta.json`, captured 2026-11-27: run 2025-06-26 18Z, data end 2025-07-07 03Z (ADR 0013) |
| `openmeteo_forecast_all_null_bom_access.json` | `models=bom_access_global&hourly=temperature_2m,visibility&forecast_hours=2` - the model is stale in Open-Meteo and returns only `null` |
| `openmeteo_error_400_out_of_range.json` | `start_hour=2026-12-30T00:00` (beyond the 16-day horizon) -> HTTP 400 body |
| `openmeteo_429_daily_limit.json` | **Wrapper** (status, headers, body) of the HTTP 429 from `ensemble-api.open-meteo.com` when the daily quota was exhausted |
| `bom_idd60910_94120_darwin_trimmed.json` | `www.bom.gov.au/fwo/IDD60910/IDD60910.94120.json` (Darwin Airport), trimmed to the header + newest 6 of 149 records |
| `ecmwf_aifs/aifs_single_20261126T06Z_corridor.grib2` | ECMWF open data AIFS Single 2026-11-26 06Z, steps 0-24 h, cropped to a 17x17 grid box; provenance in `ecmwf_aifs/README.md` |
| `onboard/airmar_canboat.jsonl` | **Synthetic** canboat `analyzer -json -si` stream (Airmar 200WX PGNs, 3 s near Tennant Creek, 2027-08-22 03:15Z) built by hand for the true-wind and window tests; not a capture |
| `onboard/ml01.csv` | **Synthetic** EKO ML-01 CSV (`time_utc,value[,pitch_deg,roll_deg]`, mV, sensitivity 7.00 uV/(W/m2) in the tests), including a bad value and blank tilt; not a capture |
| `openmeteo_aq_cams_darwin.json` | Open-Meteo Air Quality `domains=cams_global`, Darwin, 48 h from 2026-11-27 00Z (AOD550 + dust), recorded unmodified |
| `firms/j1_viirs_c2_aus_nz_24h_route_trimmed.csv` | NASA FIRMS VIIRS NOAA-20 NRT, AUS/NZ region, last 24 h on 2026-11-26, trimmed to 52 rows (route-near rows plus a few far ones); no MAP_KEY in the file |

Re-capture sparingly: the free Open-Meteo ensemble pool is small (see docs/science/providers.md, sec. 1.1).
