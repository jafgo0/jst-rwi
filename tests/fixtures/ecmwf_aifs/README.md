# ECMWF AIFS Single open-data fixture

One real AIFS run, cropped to a small grid box, so `run_from_grib` is tested
against a genuine ECMWF GRIB2 file without touching the network (ADR 0008,
open decision C).

| | |
|---|---|
| File | `aifs_single_20261126T06Z_corridor.grib2` (31,824 bytes, 60 messages) |
| Source | ECMWF open data, `model=aifs-single`, `resol=0p25`, `stream=oper`, `type=fc` |
| URLs | `https://data.ecmwf.int/forecasts/20261126/06z/aifs-single/0p25/oper/20261126060000-{0,6,12,18,24}h-oper-fc.grib2` (byte ranges from the `.index` files) |
| Run | init 2026-11-26 06:00 UTC, steps 0/6/12/18/24 h |
| Parameters | `2t 2d msl sp 10u 10v tcc lcc mcc hcc tp ssrd` |
| Retrieved | 2026-11-26 ~13:52 UTC with `ecmwf-opendata` 0.3.35 (`Client(source="ecmwf", model="aifs-single", resol="0p25").retrieve(...)`) |
| Original | 23,897,118 bytes, SHA-256 `f649baba1bd9bc327e6c4999915113efc2ebf9d298bb6d0a029d6dbea33cd11a` (not committed) |
| Fixture SHA-256 | `e17f6387080989f88b4212063caf2aa85d4492fb0ef338905478ff5d07398c4d` |
| License | CC BY 4.0. Contains ECMWF open data, © ECMWF. |

## How it was cropped

`crop_grib.py` (kept here for auditability; not imported by tests):

```
python -I crop_grib.py aifs_full.grib2 aifs_single_20261126T06Z_corridor.grib2 -21 -17 132 136
```

- Keeps the 0.25° grid nodes with -21 ≤ lat ≤ -17 and 132 ≤ lon ≤ 136 (17 × 17 nodes). No interpolation or regridding.
- Rewrites each message's grid section (`Ni`, `Nj`, first/last lat/lon) and repacks from CCSDS to `grid_simple` at the original `bitsPerValue`. Every other key, including units, run date, step and parameter, is untouched.
- Verified after cropping: decoded `2t` and `ssrd` values are identical (max abs diff 0.0) to the same nodes in the original file.

The box is a test region only. Production code takes its bbox from the active route (hard rule 4).
