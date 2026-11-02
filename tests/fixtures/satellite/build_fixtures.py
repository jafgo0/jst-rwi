"""Derive the small satellite fixtures from real Himawari-9 tiles.

Usage (needs the real tiles, ~5 MB each; they are NOT committed)::

    .venv/bin/python tests/fixtures/satellite/build_fixtures.py RAW_DIR

``RAW_DIR`` holds the object files downloaded from
``https://noaa-himawari9.s3.amazonaws.com/AHI-L2-FLDK-ISatSS/2026/11/02/{0300,0600,0800}/``
under their original names. Each fixture keeps the real global attributes and the real
``x``/``y``/``fixedgrid_projection`` attributes; only the pixel window is cropped. ``x``
and ``y`` stay the packed full-disk indices, so the crops are geolocated exactly like
the originals.
"""

from __future__ import annotations

import sys
from pathlib import Path

import h5py
import numpy as np

OUT = Path(__file__).parent

# (source object prefix, output name, first row, last row+1, first col, last col+1)  tile-local
CROPS = [
    ("s20263060300000", "T059", "T059_c03_0300_centre.nc", 1000, 1200, 1000, 1200),
    ("s20263060300000", "T059", "T059_c03_0300_south_edge.nc", 2150, 2200, 1000, 1200),
    ("s20263060300000", "T069", "T069_c03_0300_north_edge.nc", 0, 50, 1000, 1200),
    ("s20263060300000", "T069", "T069_c03_0300_desert.nc", 800, 900, 800, 900),
    ("s20263060600000", "T069", "T069_c03_0600_desert.nc", 800, 900, 800, 900),
    ("s20263060800000", "T069", "T069_c03_0800_desert.nc", 800, 900, 800, 900),
]


def _copy_attrs(src: h5py.HLObject, dst: h5py.HLObject) -> None:
    for key in src.attrs:
        if key in ("DIMENSION_LIST", "REFERENCE_LIST", "_Netcdf4Dimid", "_Netcdf4Coordinates"):
            continue
        dst.attrs[key] = src.attrs[key]


def crop(src_path: Path, dst_path: Path, r0: int, r1: int, c0: int, c1: int) -> None:
    with h5py.File(src_path, "r") as src, h5py.File(dst_path, "w") as dst:
        _copy_attrs(src, dst)
        for name in ("x", "y"):
            sl = slice(c0, c1) if name == "x" else slice(r0, r1)
            d = dst.create_dataset(name, data=src[name][sl])
            _copy_attrs(src[name], d)
        cmi = dst.create_dataset(
            "Sectorized_CMI", data=src["Sectorized_CMI"][r0:r1, c0:c1], compression="gzip"
        )
        _copy_attrs(src["Sectorized_CMI"], cmi)
        proj = dst.create_dataset("fixedgrid_projection", data=src["fixedgrid_projection"][()])
        _copy_attrs(src["fixedgrid_projection"], proj)
        dst.attrs["fixture_note"] = (
            f"crop of tile rows {r0}:{r1}, cols {c0}:{c1}; real attributes, real pixels"
        )


def limb_crop(src_path: Path, dst_path: Path) -> None:
    """A 100 x 100 window straddling the eastern limb of T044 (real space pixels included)."""
    sys.path.insert(0, str(OUT.parents[2] / "src"))
    from rwi.satellite.geo import FullGridGeometry

    geom = FullGridGeometry.from_file(src_path)
    r0, c0 = geom.tile_offsets("T044")
    row = r0 + 300
    cols = np.arange(c0, c0 + geom.tile_cols)
    lat, _ = geom.pixel_to_latlon(np.full(cols.shape, row), cols)
    edge = int(np.flatnonzero(np.isfinite(lat)).max())  # last on-disk column in this row
    c_lo = max(0, edge - 50)
    crop(src_path, dst_path, 250, 350, c_lo, c_lo + 100)


def main(raw: Path) -> None:
    for stamp, tile, name, r0, r1, c0, c1 in CROPS:
        src = next(raw.glob(f"*M1C03-{tile}_*_{stamp}_*.nc"))
        crop(src, OUT / f"satellite_{name}", r0, r1, c0, c1)
    src = next(raw.glob("*M1C03-T044_*_s20263060300000_*.nc"))
    limb_crop(src, OUT / "satellite_T044_c03_0300_limb.nc")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
