"""Live smoke tests against the real APIs. Skipped unless RWI_RUN_NETWORK_TESTS=1.

    RWI_RUN_NETWORK_TESTS=1 .venv/bin/pytest tests/integration/test_provider_live.py -m network

One request per API, polite User-Agent, no retries, throw-away cache directory. These
assert the *shape* of the responses our parsers depend on (they are the canary for an
upstream change), not particular weather values.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from rwi.config import Settings
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind
from rwi.core.timebase import TimeWindow
from rwi.providers.base import USER_AGENT
from rwi.providers.bom import BomProvider
from rwi.providers.openmeteo import OpenMeteoProvider

RUN = os.environ.get("RWI_RUN_NETWORK_TESTS") == "1"

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(not RUN, reason="live network tests: set RWI_RUN_NETWORK_TESTS=1 to run"),
]

DARWIN = GeoPoint(latitude_deg=-12.4634, longitude_deg=130.8456, elevation_m=30.0)


def live_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        cache_dir=tmp_path / "cache",
        http_retries=0,
        http_timeout_s=20.0,
        allow_network=True,
    )


async def test_openmeteo_raw_response_shape():
    """The raw API still has the fields and units our parser relies on."""
    params = {
        "latitude": DARWIN.latitude_deg,
        "longitude": DARWIN.longitude_deg,
        "timezone": "UTC",
        "hourly": "temperature_2m,pressure_msl,wind_speed_10m,shortwave_radiation,"
        "direct_normal_irradiance,diffuse_radiation,is_day",
        "forecast_hours": 3,
        "models": "best_match",
    }
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=20.0) as client:
        r = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
    assert r.status_code == 200
    body = r.json()
    assert body["utc_offset_seconds"] == 0 and body["timezone"] in ("GMT", "UTC")
    for key in ("latitude", "longitude", "elevation", "generationtime_ms", "hourly_units"):
        assert key in body
    units = body["hourly_units"]
    assert units["pressure_msl"] == "hPa" and units["wind_speed_10m"] == "km/h"
    assert units["temperature_2m"] == "°C" and units["shortwave_radiation"] == "W/m²"
    hourly = body["hourly"]
    assert len(hourly["time"]) == 3 and all(
        len(hourly[k]) == 3 for k in params["hourly"].split(",")
    )
    assert len(hourly["time"][0]) == len("2026-11-01T22:00")  # naive ISO, no offset suffix
    # the response does NOT say which model run it came from
    assert not any("init" in k.lower() or "run" in k.lower() for k in body)


async def test_openmeteo_provider_end_to_end(tmp_path):
    now = datetime.now(UTC)
    window = TimeWindow(start_utc=now, end_utc=now + timedelta(hours=6))
    async with OpenMeteoProvider(live_settings(tmp_path)) as p:
        s = await p.fetch_forecast(
            DARWIN,
            window,
            ["temperature_2m", "pressure_msl", "wind_speed_10m", "ghi", "dni", "dhi"],
        )
    assert s.kind is DataKind.FORECAST and len(s.steps) >= 6
    assert s.requested_point == DARWIN and s.point != DARWIN
    assert abs(s.point.latitude_deg - DARWIN.latitude_deg) < 0.5
    assert all(t.tzinfo is not None for t in s.times)
    first = s.steps[0]
    assert 85_000 < (first.value("pressure_msl") or 0) < 110_000  # Pa, not hPa
    assert 0 <= (first.value("wind_speed_10m") or 0) < 60  # m/s, not km/h


async def test_openmeteo_model_metadata_endpoint_shape():
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=20.0) as client:
        r = await client.get("https://api.open-meteo.com/data/ecmwf_ifs025/static/meta.json")
    assert r.status_code == 200
    meta = r.json()
    for key in (
        "last_run_initialisation_time",
        "last_run_availability_time",
        "temporal_resolution_seconds",
        "update_interval_seconds",
    ):
        assert isinstance(meta[key], int)


async def test_bom_station_feed_shape_and_provider(tmp_path):
    now = datetime.now(UTC)
    async with BomProvider(live_settings(tmp_path)) as p:
        s = await p.fetch_observations(
            DARWIN,
            TimeWindow(start_utc=now - timedelta(hours=6), end_utc=now),
            ["temperature_2m", "pressure_msl", "wind_speed_10m"],
        )
    assert s.kind is DataKind.OBSERVATION and len(s.steps) >= 6
    assert "94120" in (s.steps[0].values["temperature_2m"].provenance.notes or "")
    assert 85_000 < (s.steps[-1].value("pressure_msl") or 0) < 110_000
