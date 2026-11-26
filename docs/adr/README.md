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

Format: Context / Decision / Consequences / Alternatives considered / Status. A decision is changed by a new ADR that supersedes the old one, not by editing history.
