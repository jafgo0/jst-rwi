"""Names, errors and constants of the Himawari AHI L2 sectorised tile product.

Pure parsing and bookkeeping: no I/O, no network. Everything that is *measured
from the live bucket* (and when) is stated next to the constant it justifies, and
repeated with the numbers in ``docs/science/satellite.md``.

File names (VERIFIED against the bucket listing of 2026-11-02)::

    OR_HFD-005-B11-M1C03-T059_GH9_s20263060300000_c20263060309090.nc
       |   |   |   |  |   |    |    |              |
       |   |   |   |  |   |    |    |              +-- c: product CREATED, YYYYDDDhhmmss + tenths
       |   |   |   |  |   |    |    +-- s: scan START, YYYYDDDhhmmss + tenths  (DDD = day of year!)
       |   |   |   |  |   |    +-- satellite (GH9 = Himawari-9)
       |   |   |   |  |   +-- tile number T001..T088
       |   |   |   |  +-- band, C01..C16
       |   |   |   +-- mode digit (M1)
       |   |   +-- B11/B12/B14: file-group code, differs by band
       |   +-- nominal resolution in units of 0.1 km: 005 = 0.5 km, 010 = 1 km, 020 = 2 km
       +-- HFD = full disk; HR3 etc. = rapid-scan regional sectors (see below)

The ``s``/``c`` stamps are *day of year*, not month and day: ``2026306`` is
2026-11-02. Reading them as ``YYYYMMDD`` is the classic mistake.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from rwi.core.errors import RwiError

__all__ = [
    "BUCKET_URL",
    "DEFAULT_PRODUCT",
    "EXPECTED_TILES",
    "FULL_DISK_SCAN",
    "ParsedKey",
    "SatelliteDataError",
    "TileMissingError",
    "TileNotFoundError",
    "band_file_prefix",
    "band_resolution_km",
    "normalise_band",
    "parse_stamp",
    "parse_tile_key",
    "read_attrs",
    "satellite_name",
    "slot_prefix",
]

BUCKET_URL = "https://noaa-himawari9.s3.amazonaws.com"
"""Public anonymous bucket (NOAA Open Data Dissemination)."""

DEFAULT_PRODUCT = "AHI-L2-FLDK-ISatSS"
"""NOAA STAR 'Sectorized Full Disk' imagery in 88 tiles per band."""

FULL_DISK_SCAN = "HFD"
"""File-name scan code of the 10-minute full disk.

The same product folder also holds ``HR3`` files: a single-tile rapid-scan
*target area* every 2.5 minutes. Measured 2026-11-02: HR3 is centred on
22.5 N, 140.8 E (Ogasawara, Japan) and does not cover Australia. Those files sit
in their own minute folders (xx:02, xx:04, xx:07, xx:09 ...), so a naive 'list
every minute folder of the day' reports ~550 'slots' of which only 144 are full
disk scans.
"""

EXPECTED_TILES = 88
"""Tiles per band per full-disk slot (attribute ``number_product_tiles``)."""

# Resolution tiers (VERIFIED from product_name / source_spatial_resolution):
#   0.5 km: C03;  1 km: C01 C02 C04;  2 km: all the others.
_RES_KM: dict[str, float] = {"C03": 0.5, "C01": 1.0, "C02": 1.0, "C04": 1.0}

# File-group code between the resolution and the mode (VERIFIED on 2026-11-02).
_GROUP: dict[str, str] = {
    "C01": "B11", "C02": "B11", "C03": "B11", "C04": "B11",
    "C05": "B11", "C06": "B11", "C08": "B11", "C09": "B11", "C16": "B11",
    "C10": "B12", "C11": "B12", "C12": "B12", "C13": "B12", "C14": "B12", "C15": "B12",
    "C07": "B14",
}  # fmt: skip


class SatelliteDataError(RwiError):
    """Satellite data that is missing, inconsistent or cannot be trusted."""


class TileNotFoundError(SatelliteDataError):
    """The object does not exist at the source (HTTP 403/404)."""


class TileMissingError(SatelliteDataError):
    """A slot is partly published: some tiles the region needs are not there (yet).

    Raised rather than returning a shorter list, because a mosaic with a hole in
    it looks like a complete image.
    """


def normalise_band(band: str) -> str:
    """'C03', 'c3', '3' and 'M1C03' all mean band 3; returns 'C03'."""
    match = re.fullmatch(r"(?:M\d)?[Cc]?(\d{1,2})", band.strip())
    if match is None or not 1 <= int(match.group(1)) <= 16:
        raise ValueError(f"not an AHI band (expected C01..C16): {band!r}")
    return f"C{int(match.group(1)):02d}"


def band_resolution_km(band: str) -> float:
    """Nominal sub-satellite pixel size of a band in km (0.5, 1 or 2)."""
    return _RES_KM.get(normalise_band(band), 2.0)


def band_file_prefix(band: str) -> str:
    """Leading part of the object name for a band, e.g. ``OR_HFD-005-B11-M1C03-T``.

    This is a *hint* used to narrow an S3 listing from ~1,400 objects to 88; the
    caller must fall back to a wide listing when it matches nothing, because the
    group/mode codes belong to the provider and may change with the satellite.
    """
    band = normalise_band(band)
    res = round(band_resolution_km(band) * 10)
    return f"OR_{FULL_DISK_SCAN}-{res:03d}-{_GROUP[band]}-M1{band}-T"


def slot_prefix(product: str, slot_time_utc: datetime) -> str:
    """Folder of one slot: ``AHI-L2-FLDK-ISatSS/2026/11/02/0300/``."""
    t = slot_time_utc.astimezone(UTC)
    return f"{product}/{t:%Y/%m/%d}/{t:%H%M}/"


# ---------------------------------------------------------------------------
# File-name parsing
# ---------------------------------------------------------------------------

_KEY_RE = re.compile(
    r"(?P<prefix>.*/)?"
    r"OR_(?P<scan>[A-Z0-9]+)-(?P<res>\d{3})-B(?P<group>\d+)-M(?P<mode>\d)C(?P<band>\d{2})"
    r"-T(?P<tile>\d{3})_G(?P<sat>H\d+)"
    r"_s(?P<s>\d{14})_c(?P<c>\d{14})\.nc$"
)


@dataclass(frozen=True)
class ParsedKey:
    """The facts encoded in one object key."""

    scan: str
    resolution_km: float
    band: str
    tile_id: str
    satellite: str
    scan_start_utc: datetime
    created_utc: datetime


def satellite_name(code: str) -> str:
    """'H9' -> 'Himawari-9'. Unknown shapes are returned unchanged, not guessed."""
    match = re.fullmatch(r"H(\d+)", code)
    return f"Himawari-{int(match.group(1))}" if match else code


def parse_stamp(stamp: str) -> datetime:
    """Parse ``YYYYDDDhhmmsst`` (t = tenths of a second) into an aware UTC datetime.

    >>> parse_stamp("20263060300000").isoformat()
    '2026-11-02T03:00:00+00:00'
    """
    if len(stamp) != 14 or not stamp.isdigit():
        raise ValueError(f"not a YYYYDDDhhmmsst stamp: {stamp!r}")
    year, doy = int(stamp[0:4]), int(stamp[4:7])
    hh, mm, ss, tenth = int(stamp[7:9]), int(stamp[9:11]), int(stamp[11:13]), int(stamp[13])
    try:
        base = datetime(year, 1, 1, tzinfo=UTC) + timedelta(days=doy - 1)
        if base.year != year:
            raise ValueError("day of year out of range")
        return base.replace(hour=hh, minute=mm, second=ss, microsecond=tenth * 100_000)
    except ValueError as exc:
        raise ValueError(f"invalid stamp {stamp!r}: {exc}") from exc


def parse_tile_key(key: str) -> ParsedKey | None:
    """Decode an object key, or ``None`` when it is not an AHI tile file.

    ``None`` (rather than an exception) because a listing can legitimately hold
    other objects; the caller decides whether that is worth a warning.
    """
    match = _KEY_RE.search(key)
    if match is None:
        return None
    s, c = match.group("s"), match.group("c")
    try:
        return ParsedKey(
            scan=match.group("scan"),
            resolution_km=int(match.group("res")) / 10.0,
            band=f"C{match.group('band')}",
            tile_id=f"T{match.group('tile')}",
            satellite=satellite_name(match.group("sat")),
            scan_start_utc=parse_stamp(s),
            created_utc=parse_stamp(c),
        )
    except ValueError:
        return None


def read_attrs(node: Any) -> Mapping[str, Any]:
    """Attributes of an HDF5 group/dataset as plain Python values.

    The tiles are NetCDF-4 files, i.e. HDF5. They are read with ``h5py``, which
    this project declares directly in pyproject rather than relying on ``pvlib``
    to pull it in: depending on a transitive dependency for a module's core
    reader is how you get a mystery failure the day upstream drops it.
    ``netCDF4`` would also work and is not required. ``h5py`` returns ``bytes``
    for strings and
    one-element arrays for scalars; both are normalised, and reference-typed
    attributes (the HDF5 dimension-scale bookkeeping) are skipped.
    """
    out: dict[str, Any] = {}
    for key in node.attrs:
        try:
            value = node.attrs[key]
        except (OSError, TypeError, ValueError):
            continue
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        elif isinstance(value, np.ndarray):
            if value.dtype.kind == "O":
                continue  # object / reference arrays (HDF5 dimension-scale bookkeeping)
            if value.dtype.kind == "S":
                value = [v.decode("utf-8", "replace") for v in value.ravel()]
                value = value[0] if len(value) == 1 else value
            elif value.size == 1:
                value = value.reshape(-1)[0].item()
        elif isinstance(value, np.generic):
            value = value.item()
        out[str(key)] = value
    return out
