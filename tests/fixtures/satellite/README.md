# Satellite fixtures

Crops of real Himawari-9 `AHI-L2-FLDK-ISatSS` tiles (2026-11-02), real attributes and real pixels, 23-68 kB each.
Derived by `build_fixtures.py` from the 5 MB originals (not committed; download from
`https://noaa-himawari9.s3.amazonaws.com/AHI-L2-FLDK-ISatSS/2026/11/02/{0300,0600,0800}/`). Data (c) JMA, distributed by NOAA;
see `docs/science/satellite.md` section 6 for the attribution these require.

| File | Source tile / slot | Window (tile-local rows, cols) | Used for |
|---|---|---|---|
| `satellite_T059_c03_0300_centre.nc` | T059, 03:00Z | 1000:1200, 1000:1200 | tile-centre reference (pixel 1100), decoding |
| `satellite_T059_c03_0300_south_edge.nc` | T059, 03:00Z | 2150:2200, 1000:1200 | two-tile mosaic (rows 15350-15399) |
| `satellite_T069_c03_0300_north_edge.nc` | T069, 03:00Z | 0:50, 1000:1200 | two-tile mosaic (rows 15400-15449) |
| `satellite_T069_c03_0{300,600,800}_desert.nc` | T069, 03/06/08Z | 800:900, 800:900 | sun-normalisation evidence |
| `satellite_T044_c03_0300_limb.nc` | T044, 03:00Z | 250:350, around the eastern limb | space pixels are not flagged in the file |
