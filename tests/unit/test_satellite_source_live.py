"""Live checks against the public NOAA bucket. Never run in CI.

    RWI_RUN_NETWORK_TESTS=1 .venv/bin/pytest tests/unit/test_satellite_source_live.py -m network

They download nothing (listings only) and assert the facts the offline tests rely on.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rwi.config import Settings
from rwi.satellite.geo import tiles_covering
from rwi.satellite.source import latest_slot, list_slots, list_tiles, total_size_bytes

RUN = os.environ.get("RWI_RUN_NETWORK_TESTS") == "1"

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(not RUN, reason="live network tests: set RWI_RUN_NETWORK_TESTS=1 to run"),
]

SETTINGS = Settings(allow_network=True)


def test_live_latest_slot_is_complete_and_recent() -> None:
    now = datetime.now(UTC)
    slot = latest_slot(now=now, settings=SETTINGS)
    assert slot is not None
    assert now - slot < timedelta(hours=1)  # the feed is alive
    tiles = list_tiles(slot, settings=SETTINGS)
    assert len(tiles) == 88
    assert 0 < (tiles[0].latency_s or 0) < 1800  # product created within 30 min of the scan


def test_live_day_listing_is_only_full_disk_slots() -> None:
    slots = list_slots(day=(datetime.now(UTC) - timedelta(days=1)).date(), settings=SETTINGS)
    assert len(slots) > 100
    assert all(s.minute % 10 == 0 for s in slots)


def test_live_route_tiles_are_three_and_a_few_tens_of_megabytes_at_most(tmp_path: Path) -> None:
    slot = latest_slot(settings=SETTINGS)
    assert slot is not None
    tiles = tiles_covering(
        (-34.934, 130.837, -12.425, 138.624), slot, cache_dir=tmp_path, settings=SETTINGS
    )
    assert [t.tile_id for t in tiles] == ["T059", "T069", "T078"]
    size = total_size_bytes(tiles)
    assert size is not None and 3e6 < size < 25e6
