"""Crop regular_ll GRIB2 messages to a lat/lon box (grid nodes, no interpolation)."""

import sys

import eccodes
import numpy as np

src, dst = sys.argv[1], sys.argv[2]
S, N, W, E = (float(x) for x in sys.argv[3:7])
n = 0
with open(src, "rb") as f, open(dst, "wb") as out:
    while (h := eccodes.codes_grib_new_from_file(f)) is not None:
        ni, nj = eccodes.codes_get(h, "Ni"), eccodes.codes_get(h, "Nj")
        assert eccodes.codes_get(h, "gridType") == "regular_ll"
        assert eccodes.codes_get(h, "jScansPositively") == 0
        assert eccodes.codes_get(h, "iScansNegatively") == 0
        lat0 = eccodes.codes_get(h, "latitudeOfFirstGridPointInDegrees")
        lon0 = eccodes.codes_get(h, "longitudeOfFirstGridPointInDegrees")
        di = eccodes.codes_get(h, "iDirectionIncrementInDegrees")
        dj = eccodes.codes_get(h, "jDirectionIncrementInDegrees")
        vals = eccodes.codes_get_values(h).reshape(nj, ni)
        lats = lat0 - dj * np.arange(nj)
        lons = (lon0 + di * np.arange(ni)) % 360.0
        jj = np.nonzero((lats >= S) & (lats <= N))[0]
        ii = np.nonzero((lons >= W % 360) & (lons <= E % 360))[0]
        assert np.all(np.diff(ii) == 1), "box crosses the grid seam"
        sub = vals[np.ix_(jj, ii)]
        c = eccodes.codes_clone(h)
        eccodes.codes_set(c, "packingType", "grid_simple")
        eccodes.codes_set(c, "Ni", len(ii))
        eccodes.codes_set(c, "Nj", len(jj))
        eccodes.codes_set(c, "latitudeOfFirstGridPointInDegrees", float(lats[jj[0]]))
        eccodes.codes_set(c, "latitudeOfLastGridPointInDegrees", float(lats[jj[-1]]))
        eccodes.codes_set(c, "longitudeOfFirstGridPointInDegrees", float(lons[ii[0]]))
        eccodes.codes_set(c, "longitudeOfLastGridPointInDegrees", float(lons[ii[-1]]))
        eccodes.codes_set_values(c, sub.ravel())
        eccodes.codes_write(c, out)
        eccodes.codes_release(c)
        eccodes.codes_release(h)
        n += 1
print("messages", n)
