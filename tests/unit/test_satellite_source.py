"""Discovery, pagination and the download cache of rwi.satellite.source.

The bucket is replaced by an in-process fake that implements the parts of S3
``ListObjectsV2`` this module relies on (prefix, delimiter, max-keys, continuation
token, 1,000-key page limit) and is served through ``respx``. No network.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from rwi.config import Settings
from rwi.contracts import SatelliteTile
from rwi.core.errors import ProviderUnavailableError
from rwi.satellite import source
from rwi.satellite.source import (
    fetch_tile,
    latest_slot,
    list_cached_tiles,
    list_slots,
    list_tiles,
    read_cache_record,
    total_size_bytes,
)
from rwi.satellite.tiles import (
    BUCKET_URL,
    TileNotFoundError,
    band_file_prefix,
    normalise_band,
    parse_stamp,
    parse_tile_key,
)

PRODUCT = "AHI-L2-FLDK-ISatSS"
SETTINGS = Settings(allow_network=True, http_retries=2, http_backoff_s=0.0, http_timeout_s=5.0)
OFFLINE = Settings(allow_network=False, http_retries=2, http_backoff_s=0.0)
NS = 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'

SLOT = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
ALL_BANDS = [f"C{n:02d}" for n in range(1, 17)]


def stamp(when: datetime) -> str:
    return when.strftime("%Y%j%H%M%S") + "0"


def tile_key(
    slot: datetime, band: str, tile: int, *, created_after_s: int = 549, scan: str = "HFD",
    prefix: str | None = None,
) -> str:  # fmt: skip
    created = slot + timedelta(seconds=created_after_s)
    head = prefix if prefix is not None else band_file_prefix(band)
    head = head.replace("HFD", scan)
    return (
        f"{PRODUCT}/{slot:%Y/%m/%d/%H%M}/{head}{tile:03d}_GH9_s{stamp(slot)}_c{stamp(created)}.nc"
    )


def full_slot_keys(slot: datetime, *, odd_group_for: str | None = None) -> dict[str, int]:
    """16 bands x 88 tiles = 1,408 objects, like the real bucket."""
    out: dict[str, int] = {}
    for band in ALL_BANDS:
        prefix = band_file_prefix(band)
        if band == odd_group_for:  # the provider changed the group code for this band
            prefix = prefix.replace("B14", "B99").replace("B11", "B99").replace("B12", "B99")
        for tile in range(1, 89):
            out[tile_key(slot, band, tile, prefix=prefix)] = 2_000_000 + tile
    return out


class FakeBucket:
    """Minimal ListObjectsV2 + GET, with the real service's 1,000-key page limit."""

    def __init__(self, objects: dict[str, int] | None = None, *, page_cap: int = 1000) -> None:
        self.objects = dict(objects or {})
        self.page_cap = page_cap
        self.list_requests: list[dict[str, str]] = []
        self.get_requests: list[str] = []
        self.bodies: dict[str, bytes] = {}
        self.fail_next: list[int] = []  # statuses to return before behaving
        self.omit_token = False
        self.error_document = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0))
        params = dict(request.url.params)
        if "list-type" in params:
            return self._list(params)
        key = request.url.path.lstrip("/")
        self.get_requests.append(key)
        if key not in self.bodies:
            return httpx.Response(404, text="<Error><Code>NoSuchKey</Code></Error>")
        return httpx.Response(200, content=self.bodies[key])

    def _list(self, params: dict[str, str]) -> httpx.Response:
        self.list_requests.append(params)
        if self.error_document:
            return httpx.Response(
                200, text="<Error><Code>SlowDown</Code><Message>try later</Message></Error>"
            )
        prefix = params.get("prefix", "")
        delimiter = params.get("delimiter")
        cap = min(int(params.get("max-keys", "1000")), self.page_cap)
        after = params.get("continuation-token", "")
        keys = sorted(k for k in self.objects if k.startswith(prefix) and k > after)
        contents: list[str] = []
        prefixes: list[str] = []
        used = 0
        last = ""
        truncated = False
        for k in keys:
            if delimiter and delimiter in k[len(prefix) :]:
                folder = prefix + k[len(prefix) :].split(delimiter)[0] + delimiter
                if folder in prefixes:
                    continue
                if used >= cap:
                    truncated = True
                    break
                prefixes.append(folder)
            else:
                if used >= cap:
                    truncated = True
                    break
                contents.append(
                    f"<Contents><Key>{k}</Key><LastModified>2026-11-02T03:12:14.000Z</LastModified>"
                    f"<Size>{self.objects[k]}</Size></Contents>"
                )
            used += 1
            last = k
        if truncated and delimiter and prefixes:
            last = prefixes[-1]  # token = last folder, so the next page resumes after it
        token = (
            f"<NextContinuationToken>{last}</NextContinuationToken>"
            if truncated and not self.omit_token
            else ""
        )
        body = (
            f'<?xml version="1.0" encoding="UTF-8"?><ListBucketResult {NS}><Name>b</Name>'
            f"<Prefix>{prefix}</Prefix><KeyCount>{used}</KeyCount><MaxKeys>{cap}</MaxKeys>"
            f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>{token}"
            + "".join(contents)
            + "".join(f"<CommonPrefixes><Prefix>{p}</Prefix></CommonPrefixes>" for p in prefixes)
            + "</ListBucketResult>"
        )
        return httpx.Response(200, text=body)


@pytest.fixture
def bucket() -> FakeBucket:
    return FakeBucket()


@pytest.fixture
def mocked(bucket: FakeBucket):  # type: ignore[no-untyped-def]
    with respx.mock(assert_all_called=False) as router:
        router.route(host="noaa-himawari9.s3.amazonaws.com").mock(side_effect=bucket.handler)
        yield router


# ---------------------------------------------------------------------------
# File names
# ---------------------------------------------------------------------------


def test_stamps_are_day_of_year_not_month_day() -> None:
    """s20263060300000 is 2026, day 306 = 2 November, 03:00:00.0 - not 2026-30-60."""
    assert parse_stamp("20263060300000") == datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
    assert parse_stamp("20263061740005") == datetime(2026, 11, 2, 17, 40, 0, 500_000, tzinfo=UTC)
    assert parse_stamp("20260010000000") == datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_stamp("20243660000000") == datetime(2024, 12, 31, tzinfo=UTC)  # leap year
    with pytest.raises(ValueError, match="invalid stamp"):
        parse_stamp("20263660000000")  # day 366 of a non-leap year
    with pytest.raises(ValueError, match="not a"):
        parse_stamp("2026306")


def test_parse_the_real_example_key() -> None:
    key = (
        "AHI-L2-FLDK-ISatSS/2026/11/02/1750/"
        "OR_HFD-005-B11-M1C03-T044_GH9_s20263061740000_c20263061749130.nc"
    )
    parsed = parse_tile_key(key)
    assert parsed is not None
    assert (parsed.scan, parsed.band, parsed.tile_id, parsed.satellite) == (
        "HFD", "C03", "T044", "Himawari-9",
    )  # fmt: skip
    assert parsed.resolution_km == 0.5
    assert parsed.scan_start_utc == datetime(2026, 11, 2, 17, 40, tzinfo=UTC)
    assert parsed.created_utc == datetime(2026, 11, 2, 17, 49, 13, tzinfo=UTC)
    assert parse_tile_key("AHI-L2-FLDK-ISatSS/2026/11/02/1750/readme.txt") is None


def test_band_names_and_prefixes() -> None:
    assert [normalise_band(b) for b in ("C03", "c3", "3", "M1C03")] == ["C03"] * 4
    with pytest.raises(ValueError, match="not an AHI band"):
        normalise_band("C17")
    assert band_file_prefix("C03") == "OR_HFD-005-B11-M1C03-T"
    assert band_file_prefix("C13") == "OR_HFD-020-B12-M1C13-T"


# ---------------------------------------------------------------------------
# Listing and pagination
# ---------------------------------------------------------------------------


def test_a_full_slot_listing_really_truncates_and_is_fully_followed(
    bucket: FakeBucket, mocked: respx.MockRouter
) -> None:
    """The scenario that bit us: 1,408 objects, 1,000 per page. When the band's group
    code is not the one we expect, the narrow prefix misses and the wide listing must walk
    BOTH pages; reading only the first would silently lose every band after C12."""
    bucket.objects = full_slot_keys(SLOT, odd_group_for="C07")
    tiles = list_tiles(SLOT, band="C07", settings=SETTINGS)
    assert [t.tile_id for t in tiles] == [f"T{i:03d}" for i in range(1, 89)]
    wide = [r for r in bucket.list_requests if r["prefix"].endswith("OR_HFD-")
            and r["max-keys"] == "1000"]  # fmt: skip
    assert len(wide) == 2
    assert "continuation-token" not in wide[0]
    assert "continuation-token" in wide[1]  # the second page was asked for


def test_narrow_listing_is_one_small_page(bucket: FakeBucket, mocked: respx.MockRouter) -> None:
    bucket.objects = full_slot_keys(SLOT)
    tiles = list_tiles(SLOT, band="C03", settings=SETTINGS)
    assert len(tiles) == 88
    assert len(bucket.list_requests) == 1  # not the 760 kB wide listing
    assert bucket.list_requests[0]["prefix"].endswith("OR_HFD-005-B11-M1C03-T")


def test_two_page_response_is_fully_paginated(bucket: FakeBucket, mocked: respx.MockRouter) -> None:
    """A crafted 88-object band served 30 per page needs three requests, all followed."""
    bucket.objects = full_slot_keys(SLOT)
    bucket.page_cap = 30
    tiles = list_tiles(SLOT, band="C03", settings=SETTINGS)
    assert len(tiles) == 88
    assert len(bucket.list_requests) == 3
    tokens = [r.get("continuation-token") for r in bucket.list_requests]
    assert tokens[0] is None and tokens[1] and tokens[2] and tokens[1] != tokens[2]


def test_truncated_listing_without_a_token_is_an_error_not_a_short_list(
    bucket: FakeBucket, mocked: respx.MockRouter
) -> None:
    bucket.objects = full_slot_keys(SLOT)
    bucket.page_cap = 30
    bucket.omit_token = True
    with pytest.raises(ProviderUnavailableError, match="truncated"):
        list_tiles(SLOT, band="C03", settings=SETTINGS)


def test_empty_slot_is_an_empty_list(bucket: FakeBucket, mocked: respx.MockRouter) -> None:
    assert list_tiles(SLOT, band="C03", settings=SETTINGS) == []
    bucket.objects = full_slot_keys(SLOT)
    # the 02:40Z housekeeping slot has no folder at all
    assert list_tiles(datetime(2026, 11, 2, 2, 40, tzinfo=UTC), settings=SETTINGS) == []


def test_tiles_carry_latency_and_the_creation_time(
    bucket: FakeBucket, mocked: respx.MockRouter
) -> None:
    bucket.objects = full_slot_keys(SLOT)
    tile = list_tiles(SLOT, band="C03", settings=SETTINGS)[43]
    assert tile.tile_id == "T044"
    assert tile.satellite == "Himawari-9" and tile.band == "C03"
    assert tile.slot_time_utc == SLOT
    assert tile.published_at_utc == SLOT + timedelta(seconds=549)
    assert tile.latency_s == 549.0  # 9 min 9 s: an image is never 'now'
    assert tile.size_bytes == 2_000_044
    assert tile.key.endswith("T044_GH9_s20263060300000_c20263060309090.nc")


def test_a_reprocessed_duplicate_keeps_the_newest_and_warns(
    bucket: FakeBucket, mocked: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    old = tile_key(SLOT, "C03", 5, created_after_s=540)
    new = tile_key(SLOT, "C03", 5, created_after_s=900)
    bucket.objects = {old: 1, new: 2}
    with caplog.at_level("WARNING"):
        tiles = list_tiles(SLOT, band="C03", settings=SETTINGS)
    assert [t.key for t in tiles] == [new]
    assert "duplicate" in caplog.text


def test_list_slots_excludes_rapid_scan_folders_and_orders(
    bucket: FakeBucket, mocked: respx.MockRouter
) -> None:
    """Measured: 547 minute-folders in a day, only the x0 ones are 10-minute full disks."""
    day = datetime(2026, 11, 2, tzinfo=UTC)
    objs: dict[str, int] = {}
    for hhmm in ("0030", "0000", "0010"):  # full disk, 0020 missing on purpose
        slot = day.replace(hour=int(hhmm[:2]), minute=int(hhmm[2:]))
        objs[tile_key(slot, "C03", 1)] = 1
    for hhmm in ("0002", "0004", "0007", "0009"):  # HR3 rapid scans in their own folders
        slot = day.replace(hour=0, minute=int(hhmm[2:]))
        objs[tile_key(slot, "C03", 1, scan="HR3")] = 1
    bucket.objects = objs
    slots = list_slots(day=date(2026, 11, 2), settings=SETTINGS)
    assert slots == [day, day + timedelta(minutes=10), day + timedelta(minutes=30)]
    assert list_slots(day=date(2026, 11, 3), settings=SETTINGS) == []


def test_latest_slot_skips_a_half_published_slot(
    bucket: FakeBucket, mocked: respx.MockRouter
) -> None:
    complete = datetime(2026, 11, 2, 18, 10, tzinfo=UTC)
    partial = datetime(2026, 11, 2, 18, 20, tzinfo=UTC)
    objs = dict.fromkeys((tile_key(complete, "C03", i) for i in range(1, 89)), 1)
    objs.update({tile_key(partial, "C03", i): 1 for i in range(1, 41)})  # 40 of 88 so far
    objs[tile_key(datetime(2026, 11, 2, 18, 22, tzinfo=UTC), "C03", 1, scan="HR3")] = 1
    bucket.objects = objs
    now = datetime(2026, 11, 2, 18, 25, tzinfo=UTC)
    assert latest_slot(now=now, settings=SETTINGS) == complete
    # nothing newer than 'now' is ever returned
    assert latest_slot(now=datetime(2026, 11, 2, 18, 15, tzinfo=UTC), settings=SETTINGS) == complete
    # nothing complete within the look-back: say so, do not return something stale or partial
    assert latest_slot(now=datetime(2026, 11, 3, 6, 0, tzinfo=UTC), settings=SETTINGS) is None


def test_latest_slot_looks_back_across_midnight(
    bucket: FakeBucket, mocked: respx.MockRouter
) -> None:
    last = datetime(2026, 11, 2, 23, 50, tzinfo=UTC)
    bucket.objects = {tile_key(last, "C03", i): 1 for i in range(1, 89)}
    now = datetime(2026, 11, 3, 0, 12, tzinfo=UTC)
    assert latest_slot(now=now, settings=SETTINGS) == last


def test_s3_error_document_is_an_error(bucket: FakeBucket, mocked: respx.MockRouter) -> None:
    bucket.error_document = True
    with pytest.raises(ProviderUnavailableError, match="SlowDown"):
        list_slots(day=date(2026, 11, 2), settings=SETTINGS)


def test_transient_5xx_is_retried_then_succeeds(
    bucket: FakeBucket, mocked: respx.MockRouter
) -> None:
    bucket.objects = full_slot_keys(SLOT)
    bucket.fail_next = [503]
    assert len(list_tiles(SLOT, settings=SETTINGS)) == 88


def test_persistent_5xx_raises(bucket: FakeBucket, mocked: respx.MockRouter) -> None:
    bucket.fail_next = [503, 503, 503, 503]
    with pytest.raises(ProviderUnavailableError):
        list_tiles(SLOT, settings=SETTINGS)


def test_offline_listing_raises_without_touching_the_network(
    bucket: FakeBucket, mocked: respx.MockRouter
) -> None:
    with pytest.raises(ProviderUnavailableError, match="network disabled"):
        list_tiles(SLOT, settings=OFFLINE)
    with pytest.raises(ProviderUnavailableError, match="network disabled"):
        list_slots(day=date(2026, 11, 2), settings=OFFLINE)
    assert bucket.list_requests == []


def test_total_size_is_known_before_downloading(
    bucket: FakeBucket, mocked: respx.MockRouter
) -> None:
    bucket.objects = full_slot_keys(SLOT)
    tiles = [t for t in list_tiles(SLOT, settings=SETTINGS) if t.tile_id in ("T059", "T069")]
    assert total_size_bytes(tiles) == 2_000_059 + 2_000_069
    unknown = tiles[0].model_copy(update={"size_bytes": None})
    assert total_size_bytes([unknown, tiles[1]]) is None
    assert total_size_bytes([]) == 0


# ---------------------------------------------------------------------------
# Download cache
# ---------------------------------------------------------------------------


def _tile(bucket: FakeBucket, body: bytes = b"\x89HDF-pretend-payload" * 100) -> SatelliteTile:
    key = tile_key(SLOT, "C03", 59)
    bucket.bodies[key] = body
    return SatelliteTile(
        satellite="Himawari-9", band="C03", tile_id="T059", slot_time_utc=SLOT, key=key,
        size_bytes=len(body), published_at_utc=SLOT + timedelta(seconds=549),
    )  # fmt: skip


async def test_fetch_downloads_once_then_serves_the_cache(
    bucket: FakeBucket, mocked: respx.MockRouter, tmp_path: Path
) -> None:
    tile = _tile(bucket)
    path = await fetch_tile(tile, cache_dir=tmp_path, settings=SETTINGS)
    assert path.read_bytes() == bucket.bodies[tile.key]
    assert path == tmp_path / "satellite" / tile.key  # keyed by the object key
    assert len(bucket.get_requests) == 1
    first = read_cache_record(path)
    assert first is not None and first.size_bytes == len(bucket.bodies[tile.key])
    assert first.key == tile.key

    await asyncio.sleep(0.05)
    again = await fetch_tile(tile, cache_dir=tmp_path, settings=SETTINGS)
    assert again == path
    assert len(bucket.get_requests) == 1  # no second download
    second = read_cache_record(path)
    assert second is not None
    assert second.retrieved_at_utc == first.retrieved_at_utc  # the ORIGINAL retrieval time
    assert (datetime.now(UTC) - second.retrieved_at_utc).total_seconds() >= 0.05


async def test_cache_hit_works_offline(
    bucket: FakeBucket, mocked: respx.MockRouter, tmp_path: Path
) -> None:
    tile = _tile(bucket)
    path = await fetch_tile(tile, cache_dir=tmp_path, settings=SETTINGS)
    n = len(bucket.get_requests)
    assert await fetch_tile(tile, cache_dir=tmp_path, settings=OFFLINE) == path
    assert len(bucket.get_requests) == n
    cached = list_cached_tiles(SLOT, band="C03", cache_dir=tmp_path)
    assert [t.tile_id for t in cached] == ["T059"]
    assert cached[0].key == tile.key
    assert list_cached_tiles(SLOT, band="C13", cache_dir=tmp_path) == []


async def test_offline_with_an_empty_cache_raises_instead_of_inventing(
    bucket: FakeBucket, mocked: respx.MockRouter, tmp_path: Path
) -> None:
    tile = _tile(bucket)
    with pytest.raises(ProviderUnavailableError, match="network disabled"):
        await fetch_tile(tile, cache_dir=tmp_path, settings=OFFLINE)
    assert bucket.get_requests == []
    assert not (tmp_path / "satellite" / tile.key).exists()
    assert list_cached_tiles(SLOT, band="C03", cache_dir=tmp_path) == []


async def test_a_truncated_download_leaves_nothing_that_looks_valid(
    bucket: FakeBucket, mocked: respx.MockRouter, tmp_path: Path
) -> None:
    """Server sends 50 bytes but the listing said 1,900: no file, no record, no litter."""
    tile = _tile(bucket)
    bucket.bodies[tile.key] = b"x" * 50
    with pytest.raises(ProviderUnavailableError, match="short download"):
        await fetch_tile(tile, cache_dir=tmp_path, settings=SETTINGS)
    folder = tmp_path / "satellite" / Path(tile.key).parent
    leftovers = [p.name for p in folder.glob("*")] if folder.exists() else []
    assert leftovers == []
    assert list_cached_tiles(SLOT, band="C03", cache_dir=tmp_path) == []


async def test_a_damaged_cache_file_is_not_trusted(
    bucket: FakeBucket, mocked: respx.MockRouter, tmp_path: Path
) -> None:
    tile = _tile(bucket)
    path = await fetch_tile(tile, cache_dir=tmp_path, settings=SETTINGS)
    path.write_bytes(path.read_bytes()[:100])  # simulate a torn file
    assert read_cache_record(path) is None
    with pytest.raises(ProviderUnavailableError):
        await fetch_tile(tile, cache_dir=tmp_path, settings=OFFLINE)  # offline: refuses
    again = await fetch_tile(tile, cache_dir=tmp_path, settings=SETTINGS)  # online: repairs
    assert again.read_bytes() == bucket.bodies[tile.key]
    assert len(bucket.get_requests) == 2


async def test_a_file_without_a_record_is_not_a_hit(
    bucket: FakeBucket, mocked: respx.MockRouter, tmp_path: Path
) -> None:
    """If we cannot say when it was retrieved we cannot say how old it is."""
    tile = _tile(bucket)
    path = tmp_path / "satellite" / tile.key
    path.parent.mkdir(parents=True)
    path.write_bytes(bucket.bodies[tile.key])
    assert read_cache_record(path) is None
    with pytest.raises(ProviderUnavailableError):
        await fetch_tile(tile, cache_dir=tmp_path, settings=OFFLINE)


async def test_missing_object_is_a_typed_error(
    bucket: FakeBucket, mocked: respx.MockRouter, tmp_path: Path
) -> None:
    tile = _tile(bucket)
    del bucket.bodies[tile.key]
    with pytest.raises(TileNotFoundError):
        await fetch_tile(tile, cache_dir=tmp_path, settings=SETTINGS)
    assert len(bucket.get_requests) == 1  # a 404 is not retried


async def test_unsafe_keys_cannot_escape_the_cache(tmp_path: Path) -> None:
    evil = SatelliteTile(
        satellite="Himawari-9", band="C03", tile_id="T001", slot_time_utc=SLOT,
        key="../../etc/passwd",
    )  # fmt: skip
    with pytest.raises(ValueError, match="unsafe object key"):
        await fetch_tile(evil, cache_dir=tmp_path, settings=SETTINGS)


def test_the_record_is_plain_json_next_to_the_file(
    bucket: FakeBucket, mocked: respx.MockRouter, tmp_path: Path
) -> None:
    tile = _tile(bucket)
    path = asyncio.run(fetch_tile(tile, cache_dir=tmp_path, settings=SETTINGS))
    doc = json.loads(path.with_name(path.name + ".meta.json").read_text(encoding="utf-8"))
    assert doc["key"] == tile.key and doc["schema"] == 1
    assert doc["source_url"] == f"{BUCKET_URL}/{tile.key}"
    assert datetime.fromisoformat(doc["retrieved_at_utc"]).tzinfo is not None


def test_the_attribution_lives_in_source_and_says_it_is_modified_data() -> None:
    assert "JMA" in source.ATTRIBUTION and "NOAA" in source.ATTRIBUTION
    assert "modified" in source.ATTRIBUTION
    assert "endorsement" in source.ATTRIBUTION
    assert "noaa-himawari" in source.LICENSE


# ---------------------------------------------------------------------------
# tiles_covering: geometry picks the tiles, one listing supplies the keys
# ---------------------------------------------------------------------------

ROUTE_BOX = (-34.934, 130.837, -12.425, 138.624)  # bounding box of the bundled route


def test_tiles_covering_returns_three_of_88_for_darwin_to_adelaide(
    bucket: FakeBucket, mocked: respx.MockRouter, tmp_path: Path
) -> None:
    from rwi.satellite.geo import tiles_covering

    bucket.objects = full_slot_keys(SLOT)
    tiles = tiles_covering(ROUTE_BOX, SLOT, band="C03", cache_dir=tmp_path, settings=SETTINGS)
    assert [t.tile_id for t in tiles] == ["T059", "T069", "T078"]
    assert all(t.band == "C03" and t.slot_time_utc == SLOT for t in tiles)
    assert tiles[0].key.endswith("OR_HFD-005-B11-M1C03-T059_GH9_s20263060300000_c20263060309090.nc")
    assert total_size_bytes(tiles) == 2_000_059 + 2_000_069 + 2_000_078
    assert len(bucket.list_requests) == 1  # one narrow listing, no download of anything
    assert bucket.get_requests == []
    # the tile map was materialised on disk for next time
    assert list((tmp_path / "satellite" / "tile_maps").glob("tile_map-*.json"))


def test_tiles_covering_off_the_disk_returns_none_and_makes_no_request(
    bucket: FakeBucket, mocked: respx.MockRouter, tmp_path: Path
) -> None:
    from rwi.satellite.geo import tiles_covering

    bucket.objects = full_slot_keys(SLOT)
    for box in [(50.0, 0.0, 60.0, 10.0), (-10.0, -60.0, 10.0, -20.0)]:
        assert tiles_covering(box, SLOT, cache_dir=tmp_path, settings=SETTINGS) == []
    assert bucket.list_requests == []


def test_tiles_covering_on_a_partly_published_slot_raises(
    bucket: FakeBucket, mocked: respx.MockRouter, tmp_path: Path
) -> None:
    from rwi.satellite.geo import tiles_covering
    from rwi.satellite.tiles import TileMissingError

    objs = full_slot_keys(SLOT)
    for key in [k for k in objs if "M1C03-T069" in k]:
        del objs[key]
    bucket.objects = objs
    with pytest.raises(TileMissingError, match="T069"):
        tiles_covering(ROUTE_BOX, SLOT, cache_dir=tmp_path, settings=SETTINGS)


def test_tiles_covering_in_an_empty_slot_is_empty(
    bucket: FakeBucket, mocked: respx.MockRouter, tmp_path: Path
) -> None:
    from rwi.satellite.geo import tiles_covering

    assert tiles_covering(ROUTE_BOX, SLOT, cache_dir=tmp_path, settings=SETTINGS) == []
