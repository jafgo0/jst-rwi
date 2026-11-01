"""BoM station-observation adapter replayed from a REAL captured IDD60910 file. No network."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx

from rwi.config import Settings
from rwi.core.errors import CapabilityNotSupportedError, ProviderUnavailableError
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, QualityFlag
from rwi.core.timebase import TimeWindow
from rwi.providers.bom import BOM_STATIONS, BomProvider

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
HTTP_URL = "http://www.bom.gov.au/fwo/IDD60910/IDD60910.94120.json"  # RWI_BOM_FTP_BASE default
URL = "https://www.bom.gov.au/fwo/IDD60910/IDD60910.94120.json"  # where BoM redirects it
NOW = datetime(2026, 11, 1, 22, 10, tzinfo=UTC)
DARWIN_NTC = GeoPoint(latitude_deg=-12.4719, longitude_deg=130.8458, elevation_m=0.0)
WINDOW = TimeWindow(
    start_utc=datetime(2026, 11, 1, 20, 0, tzinfo=UTC),
    end_utc=datetime(2026, 11, 1, 22, 0, tzinfo=UTC),
)
ALL = [
    "temperature_2m",
    "apparent_temperature",
    "dew_point_2m",
    "relative_humidity_2m",
    "pressure_msl",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "cloud_cover",
    "visibility",
]


def feed() -> Any:
    return json.loads((FIXTURES / "bom_idd60910_94120_darwin_trimmed.json").read_text())


def serve(payload: Any) -> respx.Route:
    """Mimic the real behaviour: the configured http:// URL answers 301 to https://."""
    respx.get(HTTP_URL).respond(301, headers={"Location": URL})
    return respx.get(URL).respond(200, json=payload)


def make(tmp_path: Path, **kw: Any) -> BomProvider:
    st = Settings(
        _env_file=None,
        cache_dir=tmp_path / "c",
        http_retries=1,
        http_backoff_s=0.0,
        allow_network=kw.pop("allow_network", True),
    )

    async def nosleep(_: float) -> None:
        return None

    return BomProvider(st, clock=lambda: NOW, sleep=nosleep, **kw)


@respx.mock
async def test_observations_are_true_observations_with_station_provenance(tmp_path):
    route = serve(feed())
    s = await make(tmp_path).fetch_observations(DARWIN_NTC, WINDOW, ALL)
    assert route.call_count == 1
    assert s.kind is DataKind.OBSERVATION and s.provider == "bom" and s.model_name is None
    assert s.times == sorted(s.times) and len(s.times) == 5  # 20:00 .. 22:00 half-hourly
    assert all(t.tzinfo is not None and t.utcoffset() == timedelta(0) for t in s.times)
    assert s.times[-1] == datetime(2026, 11, 1, 22, 0, tzinfo=UTC)
    # point = the STATION, requested_point = what we asked for (7 km away)
    assert s.requested_point == DARWIN_NTC
    assert (s.point.latitude_deg, s.point.longitude_deg) == (-12.4239, 130.8925)
    prov = s.steps[-1].values["temperature_2m"].provenance
    assert prov.kind is DataKind.OBSERVATION
    assert prov.observation_time_utc == s.times[-1] and prov.valid_time_utc is None
    assert prov.model_name is None and prov.model_init_time_utc is None
    assert "Darwin Airport" in (prov.notes or "") and "wind_src=metar_10" in (prov.notes or "")
    assert "personal" in (s.license or "") and "organisation" in (s.license or "")


@respx.mock
async def test_values_and_unit_conversion_against_the_raw_feed(tmp_path):
    serve(feed())
    s = await make(tmp_path).fetch_observations(DARWIN_NTC, WINDOW, ALL)
    latest = s.steps[-1]  # 2026-11-01 22:00Z, raw: air_temp 28.7, press_msl 1015.1, wind 7 km/h
    assert latest.value("temperature_2m") == pytest.approx(28.7)
    assert latest.value("pressure_msl") == pytest.approx(101510.0)
    assert latest.value("wind_speed_10m") == pytest.approx(7 / 3.6)
    assert latest.value("wind_gusts_10m") == pytest.approx(11 / 3.6)
    assert latest.value("wind_direction_10m") == 38.0
    assert latest.value("relative_humidity_2m") == 83.0
    assert latest.value("cloud_cover") == 0.0  # 0 oktas
    assert latest.value("visibility") == pytest.approx(10000.0)  # "10" km


@respx.mock
async def test_calm_wind_direction_and_dash_values_become_missing_not_zero(tmp_path):
    doc = feed()
    rec = doc["observations"]["data"][0]
    rec.update(
        {
            "wind_dir": "CALM",
            "wind_dir_deg": 0,
            "wind_spd_kmh": 0,
            "vis_km": "-",
            "cloud_oktas": 9,
            "gust_kmh": None,
        }
    )
    serve(doc)
    s = await make(tmp_path).fetch_observations(DARWIN_NTC, WINDOW, ALL)
    m = s.steps[-1].values
    assert (
        m["wind_direction_10m"].value is None
        and m["wind_direction_10m"].quality is QualityFlag.MISSING
    )
    assert m["wind_speed_10m"].value == 0.0  # calm speed IS a measurement
    assert m["visibility"].value is None and m["cloud_cover"].value is None
    assert m["wind_gusts_10m"].value is None


@respx.mock
async def test_window_outside_the_feed_raises_with_coverage_in_message(tmp_path):
    serve(feed())
    w = TimeWindow(start_utc=NOW + timedelta(days=1), end_utc=NOW + timedelta(days=2))
    with pytest.raises(ProviderUnavailableError, match="feed covers"):
        await make(tmp_path).fetch_observations(DARWIN_NTC, w, ["temperature_2m"])


@respx.mock
async def test_nearest_station_and_distance_limit(tmp_path):
    p = make(tmp_path)
    station, dist = p.nearest_station(DARWIN_NTC)
    assert station.wmo == 94120 and dist == pytest.approx(7.0, abs=1.0)
    alice = GeoPoint(latitude_deg=-23.70, longitude_deg=133.88)
    assert p.nearest_station(alice)[0].wmo == 94326
    jonkoping = GeoPoint(latitude_deg=57.78, longitude_deg=14.16)
    with respx.mock, pytest.raises(CapabilityNotSupportedError, match="km away"):
        await p.fetch_observations(jonkoping, WINDOW, ["temperature_2m"])
    # a point on the highway between stations is refused rather than mislabelled
    mid_route = GeoPoint(latitude_deg=-27.0, longitude_deg=134.3)
    with respx.mock, pytest.raises(CapabilityNotSupportedError, match="refusing"):
        await p.fetch_observations(mid_route, WINDOW, ["temperature_2m"])


async def test_forecast_and_irradiance_are_not_supported(tmp_path):
    p = make(tmp_path)
    caps = p.capabilities
    assert caps.supports_observations and not caps.supports_forecast
    assert not caps.supports_ensemble and not caps.supports_archive
    assert not ({"ghi", "dni", "dhi"} & caps.variables)
    assert not caps.requires_api_key
    with pytest.raises(CapabilityNotSupportedError, match="observations only"):
        await p.fetch_forecast(DARWIN_NTC, WINDOW, ["temperature_2m"])
    with pytest.raises(CapabilityNotSupportedError, match="not supported by this dataset"):
        await p.fetch_observations(DARWIN_NTC, WINDOW, ["ghi"])


@respx.mock
async def test_non_bom_json_is_rejected(tmp_path):
    serve({"hello": "world"})
    with pytest.raises(ProviderUnavailableError, match="not a Bureau"):
        await make(tmp_path).fetch_observations(DARWIN_NTC, WINDOW, ["temperature_2m"])


@respx.mock
async def test_cache_keeps_original_retrieval_time_and_offline_serves_it(tmp_path):
    route = serve(feed())
    first = await make(tmp_path).fetch_observations(DARWIN_NTC, WINDOW, ["temperature_2m"])
    offline = make(tmp_path, allow_network=False)
    again = await offline.fetch_observations(DARWIN_NTC, WINDOW, ["temperature_2m"])
    assert route.call_count == 1
    assert again.retrieved_at_utc == first.retrieved_at_utc
    assert "cache" in (again.steps[0].values["temperature_2m"].provenance.notes or "").lower()


async def test_offline_without_cache_raises(tmp_path):
    with respx.mock, pytest.raises(ProviderUnavailableError, match="no cached data"):
        await make(tmp_path, allow_network=False).fetch_observations(
            DARWIN_NTC, WINDOW, ["temperature_2m"]
        )


def test_station_catalogue_is_well_formed():
    assert len({s.wmo for s in BOM_STATIONS}) == len(BOM_STATIONS)
    assert {s.product for s in BOM_STATIONS} == {"IDD60910", "IDS60910"}
