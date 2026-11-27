# Architecture decision records

| # | Title | Status |
|---|---|---|
| [0001](0001-python-fastapi-static-maplibre.md) | Python + FastAPI backend with a static MapLibre dashboard | Accepted (phase 1) |
| [0002](0002-license.md) | Licence decision for the repository | **Open**, recommendation given |
| [0003](0003-pvlib-solar-authority.md) | pvlib as the solar-geometry and irradiance authority | Accepted |
| [0004](0004-provider-abstraction-and-fallback.md) | Provider abstraction with capabilities and fallback; forecast never substituted for observation | Accepted |
| [0005](0005-contracts-module.md) | One cross-module contracts module | Accepted |
| [0006](0006-storage-no-database-phase-1.md) | Storage: no database in phase 1 | Accepted (phases 1-2) |
| [0007](0007-sqlite-forecast-archive.md) | SQLite for the forecast/observation archive | Accepted (phase 2) |
| [0008](0008-aifs-provider-and-ml-postprocessing.md) | ECMWF AIFS as a gridded provider; quantile ML post-processing | **Accepted** |
| [0009](0009-nmea2000-decoding-via-canboat.md) | NMEA 2000 decoding through canboat, not an in-house decoder | Accepted |
| [0010](0010-onboard-sensor-ingestion.md) | On-board sensor ingestion: Airmar 200WX and EKO ML-01 | Accepted |
| [0011](0011-quantile-bands-and-stable-v1-api.md) | Quantile bands and the stable `/api/v1` | Accepted |
| [0012](0012-ensemble-cold-front-passage.md) | Ensemble cold-front passage time per control stop | Accepted |
| [0013](0013-bom-access-g-rejected.md) | BoM ACCESS-G via Open-Meteo rejected; stale-model guard | Accepted |
| [0014](0014-cams-aerosol-dynamic-turbidity-firms.md) | CAMS aerosol, dynamic Linke turbidity and NASA FIRMS hotspots | Accepted |
| [0015](0015-camp-charging-forecast.md) | Camp-site charging forecast per candidate overnight stop | Accepted |

Format: Context / Decision / Consequences / Alternatives considered / Status. A decision is changed by a new ADR that supersedes the old one, not by editing history.
