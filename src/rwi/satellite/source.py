"""Discovery and download of Himawari AHI L2 tiles from the public NOAA bucket.

No boto3, no credentials: the bucket allows anonymous S3 REST (``ListObjectsV2``)
and plain HTTPS ``GET``. The XML listing is parsed with the standard library.

Two traps this module exists to avoid, both measured on 2026-11-02:

* **Listings truncate silently.** One full-disk slot holds 1,408 objects (16 bands
  x 88 tiles); S3 returns at most 1,000 per page and says so only through
  ``<IsTruncated>true</IsTruncated>``. A reader that ignores it sees a slot
  missing its last bands and nothing looks wrong. :func:`_list_objects` follows
  ``NextContinuationToken`` until the listing is complete and raises if S3 says
  "truncated" without giving a token.
* **The product folder is not only the full disk.** See
  :data:`rwi.satellite.tiles.FULL_DISK_SCAN`.

Latency is exposed, never hidden: :attr:`SatelliteTile.published_at_utc` is the
``c`` (created) stamp of the file name. The S3 ``LastModified`` is typically
2-6 minutes *later* (the object appears in the bucket after it was created), so
the creation stamp is a lower bound on the true age of what you hold.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx

from rwi.config import Settings, get_settings
from rwi.contracts import SatelliteTile
from rwi.core.errors import ProviderUnavailableError
from rwi.core.timebase import ensure_utc, utc_now
from rwi.satellite.tiles import (
    BUCKET_URL,
    DEFAULT_PRODUCT,
    EXPECTED_TILES,
    FULL_DISK_SCAN,
    TileNotFoundError,
    band_file_prefix,
    normalise_band,
    parse_tile_key,
    slot_prefix,
)

__all__ = [
    "ATTRIBUTION",
    "LICENSE",
    "CacheRecord",
    "fetch_tile",
    "latest_slot",
    "list_cached_tiles",
    "list_slots",
    "list_tiles",
    "read_cache_record",
    "total_size_bytes",
]

log = logging.getLogger(__name__)

# Licence and attribution (single source of truth; scene.py imports these).
# Read from https://registry.opendata.aws/noaa-himawari on 2026-11-02.
LICENSE: str = (
    "JMA Himawari-8/9 data distributed by NOAA (NODD). NOAA has rights to distribute freely "
    "and openly; NOAA and JMA request attribution for use or dissemination of unaltered data; "
    "do not state or imply endorsement by or affiliation with NOAA or JMA; modified data must "
    "not be presented as original, unaltered data. Terms: "
    "https://registry.opendata.aws/noaa-himawari"
)

ATTRIBUTION: str = (
    "Contains JMA Himawari-9 data (Japan Meteorological Agency) distributed by NOAA; accessed "
    "from https://registry.opendata.aws/noaa-himawari. Resampled and processed by JST-RWI: "
    "this is modified data, not the original product, and carries no NOAA or JMA endorsement."
)


USER_AGENT = "jst-rwi/0.1 (JU Solar Team; research; +https://github.com/ju-solar-team)"
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_MAX_PAGES = 50
"""Safety stop: a listing that never ends is an error, not an infinite loop."""
_CACHE_SCHEMA = 1


# ---------------------------------------------------------------------------
# S3 listing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Listing:
    keys: list[tuple[str, int, datetime | None]]  # (key, size, last_modified)
    prefixes: list[str]
    pages: int


def _require_network(settings: Settings, what: str) -> None:
    if not settings.allow_network:
        raise ProviderUnavailableError(
            f"network disabled (RWI_ALLOW_NETWORK=false): cannot {what}. "
            "Satellite data is served from the on-disk cache or not at all."
        )


def _parse_listing(xml_bytes: bytes) -> tuple[_Listing, bool, str | None]:
    """Parse one page of an S3 ``ListObjectsV2`` response.

    Returns ``(listing, is_truncated, next_token)``.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ProviderUnavailableError(f"unparseable S3 listing: {exc}") from exc
    tag = root.tag.removeprefix(_S3_NS)
    if tag == "Error":
        code = root.findtext(f"{_S3_NS}Code") or root.findtext("Code") or "?"
        msg = root.findtext(f"{_S3_NS}Message") or root.findtext("Message") or ""
        raise ProviderUnavailableError(f"S3 error {code}: {msg}")
    if tag != "ListBucketResult":
        raise ProviderUnavailableError(f"unexpected S3 document <{tag}>")

    keys: list[tuple[str, int, datetime | None]] = []
    for item in root.findall(f"{_S3_NS}Contents"):
        key = item.findtext(f"{_S3_NS}Key")
        if not key:
            continue
        size = int(item.findtext(f"{_S3_NS}Size") or 0)
        modified_text = item.findtext(f"{_S3_NS}LastModified")
        modified = (
            datetime.fromisoformat(modified_text.replace("Z", "+00:00")) if modified_text else None
        )
        keys.append((key, size, modified))
    prefixes = [
        p.findtext(f"{_S3_NS}Prefix") or ""
        for p in root.findall(f"{_S3_NS}CommonPrefixes")
        if p.findtext(f"{_S3_NS}Prefix")
    ]
    truncated = (root.findtext(f"{_S3_NS}IsTruncated") or "false").strip().lower() == "true"
    token = root.findtext(f"{_S3_NS}NextContinuationToken")
    return _Listing(keys, prefixes, 1), truncated, token


def _get_with_retry(
    client: httpx.Client, url: str, params: dict[str, str], settings: Settings
) -> httpx.Response:
    last: Exception | None = None
    for attempt in range(max(1, settings.http_retries)):
        try:
            response = client.get(url, params=params)
            if response.status_code >= 500 or response.status_code == 429:
                last = ProviderUnavailableError(f"HTTP {response.status_code} from {url}")
            else:
                return response
        except httpx.HTTPError as exc:
            last = exc
        if attempt + 1 < max(1, settings.http_retries):
            time.sleep(settings.http_backoff_s * (2**attempt))
    raise ProviderUnavailableError(f"giving up on {url}: {last}") from last


def _list_objects(
    prefix: str,
    *,
    delimiter: str | None,
    settings: Settings,
    max_keys: int = 1000,
    first_page_only: bool = False,
) -> _Listing:
    """Complete listing under ``prefix``, following every continuation token.

    ``first_page_only`` is for existence probes (``max_keys=1``): the result is by
    construction not complete and must not be used as a listing.
    """
    _require_network(settings, f"list s3://{prefix}")
    keys: list[tuple[str, int, datetime | None]] = []
    prefixes: list[str] = []
    token: str | None = None
    with httpx.Client(
        timeout=settings.http_timeout_s, headers={"User-Agent": USER_AGENT}
    ) as client:
        for page in range(1, _MAX_PAGES + 1):
            params = {"list-type": "2", "prefix": prefix, "max-keys": str(max_keys)}
            if delimiter:
                params["delimiter"] = delimiter
            if token:
                params["continuation-token"] = token
            response = _get_with_retry(client, f"{BUCKET_URL}/", params, settings)
            if response.status_code != 200:
                raise ProviderUnavailableError(
                    f"S3 listing of {prefix!r} returned HTTP {response.status_code}"
                )
            chunk, truncated, token = _parse_listing(response.content)
            keys.extend(chunk.keys)
            prefixes.extend(chunk.prefixes)
            if not truncated or first_page_only:
                return _Listing(keys, prefixes, page)
            if not token:
                raise ProviderUnavailableError(
                    f"S3 listing of {prefix!r} is truncated but gave no continuation token; "
                    "refusing to return a partial listing"
                )
    raise ProviderUnavailableError(f"S3 listing of {prefix!r} exceeded {_MAX_PAGES} pages")


# ---------------------------------------------------------------------------
# Public discovery API
# ---------------------------------------------------------------------------


def _slot_from_folder(folder: str) -> datetime | None:
    """``AHI-L2-FLDK-ISatSS/2026/11/02/0300/`` -> 2026-11-02T03:00Z, else ``None``."""
    match = re.search(r"/(\d{4})/(\d{2})/(\d{2})/(\d{2})(\d{2})/?$", folder)
    if match is None:
        return None
    y, mo, d, hh, mm = (int(g) for g in match.groups())
    try:
        return datetime(y, mo, d, hh, mm, tzinfo=UTC)
    except ValueError:
        return None


def _is_full_disk_slot(slot: datetime) -> bool:
    """Full-disk scans start on a multiple of 10 minutes (periodicity attribute = 10).

    VERIFIED 2026-11-02: every rapid-scan (HR3) folder starts at minute x2, x4, x7
    or x9, never x0, while the 10-minute full-disk folders are exactly
    0000, 0010, 0020 ... A folder passing this test can still be empty for a band
    (the 02:40Z and 14:40Z housekeeping slots are absent altogether), so callers
    that need data use :func:`list_tiles` rather than trusting a folder name.
    """
    return slot.minute % 10 == 0


def list_slots(
    *, day: date, product: str = DEFAULT_PRODUCT, settings: Settings | None = None
) -> list[datetime]:
    """Nominal start times (UTC) of the full-disk scans published for ``day``, ascending.

    Rapid-scan sector folders sharing the product directory are excluded. An empty
    list means nothing is published for that day (not an error). ``settings`` is
    only for tests and offline drills; it defaults to the process settings.
    """
    settings = settings or get_settings()
    listing = _list_objects(f"{product}/{day:%Y/%m/%d}/", delimiter="/", settings=settings)
    slots = [s for p in listing.prefixes if (s := _slot_from_folder(p)) is not None]
    return sorted(s for s in slots if _is_full_disk_slot(s))


def _tiles_from_keys(
    entries: Iterable[tuple[str, int, datetime | None]], band: str
) -> list[SatelliteTile]:
    best: dict[str, SatelliteTile] = {}
    for key, size, _modified in entries:
        parsed = parse_tile_key(key)
        if parsed is None or parsed.scan != FULL_DISK_SCAN or parsed.band != band:
            continue
        tile = SatelliteTile(
            satellite=parsed.satellite,
            band=parsed.band,
            tile_id=parsed.tile_id,
            slot_time_utc=parsed.scan_start_utc,
            key=key,
            size_bytes=size,
            published_at_utc=parsed.created_utc,
        )
        previous = best.get(tile.tile_id)
        if previous is not None:
            log.warning("duplicate tile %s in one slot: %s and %s", tile.tile_id, previous.key, key)
            if (previous.published_at_utc or datetime.min.replace(tzinfo=UTC)) >= (
                tile.published_at_utc or datetime.min.replace(tzinfo=UTC)
            ):
                continue
        best[tile.tile_id] = tile
    return [best[k] for k in sorted(best)]


def list_tiles(
    slot_time_utc: datetime,
    *,
    band: str = "C03",
    product: str = DEFAULT_PRODUCT,
    settings: Settings | None = None,
) -> list[SatelliteTile]:
    """All tiles of ``band`` published for the slot, ordered by tile id.

    ``[]`` when the slot folder is empty or has no such band (a slot that has not
    been published yet, or the 02:40Z/14:40Z housekeeping gap). Never raises for
    emptiness; raises :class:`ProviderUnavailableError` if the *listing itself*
    cannot be completed, so an incomplete listing cannot pass for an empty slot.

    The listing is narrowed with the band's file-name prefix so it costs ~30 kB
    (one page, 88 objects) instead of ~760 kB (two pages, all 1,408 objects). If
    the narrow prefix finds nothing but the slot has full-disk files, the wide
    paginated listing is used instead, because the group code in the file name
    belongs to the provider and may change.
    """
    settings = settings or get_settings()
    slot = ensure_utc(slot_time_utc)
    band = normalise_band(band)
    folder = slot_prefix(product, slot)

    narrow = _list_objects(folder + band_file_prefix(band), delimiter=None, settings=settings)
    tiles = _tiles_from_keys(narrow.keys, band)
    if tiles:
        return tiles

    probe = _list_objects(
        folder + f"OR_{FULL_DISK_SCAN}-", delimiter=None, settings=settings,
        max_keys=1, first_page_only=True,
    )  # fmt: skip
    if not probe.keys:
        return []
    wide = _list_objects(folder + f"OR_{FULL_DISK_SCAN}-", delimiter=None, settings=settings)
    return _tiles_from_keys(wide.keys, band)


def latest_slot(
    *,
    now: datetime | None = None,
    band: str = "C03",
    product: str = DEFAULT_PRODUCT,
    min_tiles: int = EXPECTED_TILES,
    lookback_hours: int = 4,
    settings: Settings | None = None,
) -> datetime | None:
    """Most recent full-disk slot at or before ``now`` whose ``band`` is *complete*.

    "Complete" means at least ``min_tiles`` tiles (88): a slot is uploaded tile by
    tile over about a minute, and the newest folder is often half-written. Returning
    it would hand the caller a mosaic with holes. ``None`` when nothing complete is
    found within ``lookback_hours`` (a stale feed is reported, not papered over).
    """
    settings = settings or get_settings()
    reference = ensure_utc(now) if now is not None else utc_now()
    band = normalise_band(band)

    candidates: list[datetime] = []
    seen: set[datetime] = set()
    hour = reference.replace(minute=0, second=0, microsecond=0)
    for back in range(lookback_hours + 1):
        h = hour - timedelta(hours=back)
        # prefix '.../DD/HH' + delimiter '/' -> the minute folders of that hour only
        listing = _list_objects(f"{product}/{h:%Y/%m/%d/%H}", delimiter="/", settings=settings)
        for folder in listing.prefixes:
            slot = _slot_from_folder(folder)
            if slot and slot <= reference and _is_full_disk_slot(slot) and slot not in seen:
                seen.add(slot)
                candidates.append(slot)
        if len(candidates) >= 3:  # newest few are enough: older ones are complete or gone
            break
    for slot in sorted(candidates, reverse=True):
        count = len(list_tiles(slot, band=band, product=product, settings=settings))
        if count >= min_tiles:
            return slot
        log.info("slot %s has %d/%d tiles of %s; trying an older one", slot, count, min_tiles, band)
    return None


def total_size_bytes(tiles: Iterable[SatelliteTile]) -> int | None:
    """Sum of the listed object sizes; ``None`` if any size is unknown.

    The bandwidth budget of a request, known *before* downloading.
    """
    total = 0
    for tile in tiles:
        if tile.size_bytes is None:
            return None
        total += tile.size_bytes
    return total


# ---------------------------------------------------------------------------
# Download with an on-disk cache
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheRecord:
    """What was written next to a cached tile: when we got it and what we asked for."""

    key: str
    retrieved_at_utc: datetime
    size_bytes: int
    source_url: str


def _cache_path(cache_dir: Path, key: str) -> Path:
    """Where the object for ``key`` lives: ``<cache_dir>/satellite/<key>``."""
    parts = key.split("/")
    if not key or key.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"refusing unsafe object key {key!r}")
    return Path(cache_dir, "satellite", *parts)


def _sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".meta.json")


def read_cache_record(path: Path) -> CacheRecord | None:
    """The cache record of a cached tile file, or ``None`` if absent or inconsistent.

    A record whose ``size_bytes`` differs from the file on disk is treated as
    absent: that file is not what was downloaded.
    """
    try:
        raw = json.loads(_sidecar(path).read_text(encoding="utf-8"))
        if raw.get("schema") != _CACHE_SCHEMA:
            return None
        record = CacheRecord(
            key=str(raw["key"]),
            retrieved_at_utc=ensure_utc(datetime.fromisoformat(raw["retrieved_at_utc"])),
            size_bytes=int(raw["size_bytes"]),
            source_url=str(raw["source_url"]),
        )
        if path.stat().st_size != record.size_bytes:
            return None
        return record
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


async def _download(url: str, dest_tmp: Path, settings: Settings, expected: int | None) -> int:
    """Stream ``url`` into ``dest_tmp``; returns the byte count. Retries transient errors."""
    last: Exception | None = None
    attempts = max(1, settings.http_retries)
    for attempt in range(attempts):
        try:
            async with (
                httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        settings.http_timeout_s, read=settings.http_timeout_s * 4
                    ),
                    headers={"User-Agent": USER_AGENT},
                ) as client,
                client.stream("GET", url) as response,
            ):
                if response.status_code in (403, 404):
                    raise TileNotFoundError(f"{url} -> HTTP {response.status_code}")
                if response.status_code != 200:
                    raise ProviderUnavailableError(f"{url} -> HTTP {response.status_code}")
                written = 0
                with dest_tmp.open("wb") as fh:
                    async for chunk in response.aiter_bytes(262_144):
                        fh.write(chunk)
                        written += len(chunk)
                    fh.flush()
                    os.fsync(fh.fileno())
                declared = response.headers.get("content-length")
                for want in (int(declared) if declared is not None else None, expected):
                    if want is not None and written != want:
                        raise ProviderUnavailableError(
                            f"short download of {url}: got {written} of {want} bytes"
                        )
                return written
        except TileNotFoundError:
            raise
        except (httpx.HTTPError, ProviderUnavailableError) as exc:
            last = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(settings.http_backoff_s * (2**attempt))
    raise ProviderUnavailableError(f"giving up on {url}: {last}") from last


async def fetch_tile(
    tile: SatelliteTile, *, cache_dir: Path, settings: Settings | None = None
) -> Path:
    """Local path of ``tile``'s NetCDF file, downloading it only if it is not cached.

    * **Cache key** is the object key; nothing else can collide.
    * **A hit never touches the network and never refreshes the record**: the
      sidecar ``<file>.meta.json`` keeps the ORIGINAL retrieval time so downstream
      age arithmetic stays honest. Read it with :func:`read_cache_record`.
    * **Atomic**: the body is streamed to a temporary name, fsync'd, checked against
      the advertised length, and only then renamed; the record is written *before*
      the rename, so a crash can leave an orphan record but never a data file
      without one, and never a half-written file under the final name.
    * **Offline** (``settings.allow_network`` false): serves the cache or raises
      :class:`ProviderUnavailableError`. It never returns anything invented.
    """
    settings = settings or get_settings()
    path = _cache_path(cache_dir, tile.key)
    if path.is_file() and read_cache_record(path) is not None:
        return path
    _require_network(settings, f"download {tile.key}")

    url = f"{BUCKET_URL}/{quote(tile.key, safe='/')}"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        size = await _download(url, tmp, settings, tile.size_bytes)
        retrieved = utc_now()
        record = {
            "schema": _CACHE_SCHEMA,
            "key": tile.key,
            "retrieved_at_utc": retrieved.isoformat(),
            "size_bytes": size,
            "source_url": url,
        }
        _atomic_write_bytes(_sidecar(path), json.dumps(record, indent=1).encode("utf-8"))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return path


def list_cached_tiles(
    slot_time_utc: datetime,
    *,
    band: str = "C03",
    cache_dir: Path,
    product: str = DEFAULT_PRODUCT,
) -> list[SatelliteTile]:
    """Tiles of the slot that are in the on-disk cache with a valid record, by tile id.

    The offline counterpart of :func:`list_tiles`: it never touches the network, and
    only reports files that :func:`read_cache_record` accepts (complete, with a
    retrieval time), so a half-written file is not offered.
    """
    slot = ensure_utc(slot_time_utc)
    band = normalise_band(band)
    folder = Path(cache_dir, "satellite", *slot_prefix(product, slot).strip("/").split("/"))
    out: dict[str, SatelliteTile] = {}
    for path in sorted(folder.glob("*.nc")) if folder.is_dir() else []:
        parsed = parse_tile_key(path.name)
        record = read_cache_record(path)
        if parsed is None or record is None or parsed.scan != FULL_DISK_SCAN:
            continue
        if parsed.band != band:
            continue
        out[parsed.tile_id] = SatelliteTile(
            satellite=parsed.satellite,
            band=parsed.band,
            tile_id=parsed.tile_id,
            slot_time_utc=parsed.scan_start_utc,
            key=record.key,
            size_bytes=record.size_bytes,
            published_at_utc=parsed.created_utc,
        )
    return [out[k] for k in sorted(out)]
