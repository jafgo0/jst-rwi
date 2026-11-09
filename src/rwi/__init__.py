"""JST-RWI - JU Solar Team Race Weather Intelligence platform (BWSC 2027).

Package layout:
  rwi.core      shared scientific primitives: time, geo, units, wind, provenance, position state
  rwi.solar     solar geometry and clear-sky irradiance (physical models)
  rwi.providers external weather data adapters (observations and forecasts)
  rwi.quality   data quality control and freshness gating
  rwi.route     race route geometry, projection, ETA trajectory
  rwi.energy    photovoltaic irradiance-to-power-to-energy modelling
  rwi.vehicle   vehicle longitudinal dynamics and energy consumption
  rwi.strategy  scenario evaluation and race decision support
  rwi.api       FastAPI application layer
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("jst-rwi")
except PackageNotFoundError:  # pragma: no cover - editable/source checkout fallback
    __version__ = "0+unknown"

__all__ = ["__version__"]
