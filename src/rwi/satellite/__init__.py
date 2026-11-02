"""Satellite cloud intelligence: imagery, irradiance retrieval and nowcasting.

Cloud shading is the fastest way a solar car loses power. A numerical weather
model on a 9-25 km grid refreshed every six hours cannot see a cloud band
arriving in twenty minutes; geostationary imagery at 0.5 km every ten minutes
can. That is what this package is for.

Three groups, deliberately independent at import time:

  source, tiles, geo, scene   discovery, download, decoding and geolocation
  cloudindex, retrieval       reflectance -> cloud index -> clear-sky index -> GHI
  motion, nowcast             cloud motion by phase correlation, and advection

**This module intentionally re-exports nothing.** Import the submodule you
need. Two reasons: a package that imports everything fails entirely when one
part is broken, which is the opposite of how the rest of this platform
degrades; and keeping the groups uncoupled at import time is what let them be
built in parallel and is enforced by
`tests/unit/test_satellite_integration.py`.

Read before using any of it:

* Nothing here is a measurement. A satellite irradiance is a RETRIEVAL - a
  model applied to radiances observed from orbit - and no instrument on the
  ground measured it. `IrradianceNowcast` rejects `DataKind.OBSERVATION`
  outright for that reason.
* Nothing here is validated. This platform has no ground-truth irradiance at
  all, so the error of every retrieval and every nowcast below is unknown. A
  pyranometer on the vehicle or a support car is what would change that, and
  it is the single highest-value instrument the team could add.
* The cloud-index-to-irradiance relation itself is not yet confirmed against
  its primary source; `cloudindex.py` carries the exact verification record
  above the function, and resolving it is a contained task for anyone with
  library access.
* A visible-band product is meaningless at night. The code refuses rather than
  returning a number, because a dark image means darkness, not clear sky.

See docs/science/satellite.md, satellite-irradiance.md and nowcasting.md.
"""
