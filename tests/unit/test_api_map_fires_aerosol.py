"""/api/map/fires and /api/map/aerosol endpoints, offline replays (ADR 0014)."""

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from rwi.api.app import create_app
from rwi.api.routers import map as map_router
from rwi.config import get_settings

FIXTURES = Path(__file__).parents[1] / "fixtures"
FIRMS_CSV = FIXTURES / "firms" / "j1_viirs_c2_aus_nz_24h_route_trimmed.csv"
CAMS_JSON = FIXTURES / "openmeteo_aq_cams_darwin.json"
ROUTE = "bwsc-2027-stuart-highway"
KEY = "0123456789abcdef0123456789abcdef"  # fake test key
_ENV = (
    "RWI_FIRMS_MAP_KEY",
    "RWI_FIRMS_SOURCE",
    "RWI_FIRMS_DAY_RANGE",
    "RWI_FIRMS_ROUTE_BUFFER_KM",
    "RWI_FIRE_ALERT_RADIUS_KM",
    "RWI_FIRE_ALERT_MIN_CONFIDENCE",
    "RWI_FIRE_ALERT_MIN_FRP_MW",
    "RWI_AOD_ANGSTROM_EXPONENT",
)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("RWI_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("RWI_HTTP_RETRIES", "0")
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    map_router._aifs_cache.clear()
    map_router._ifs_cache.clear()
    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def _configure_firms(monkeypatch: pytest.MonkeyPatch, *, alerts: bool) -> None:
    monkeypatch.setenv("RWI_FIRMS_MAP_KEY", KEY)
    monkeypatch.setenv("RWI_FIRMS_SOURCE", "VIIRS_NOAA21_NRT")
    monkeypatch.setenv("RWI_FIRMS_DAY_RANGE", "1")
    monkeypatch.setenv("RWI_FIRMS_ROUTE_BUFFER_KM", "50")
    if alerts:
        monkeypatch.setenv("RWI_FIRE_ALERT_RADIUS_KM", "20")
        monkeypatch.setenv("RWI_FIRE_ALERT_MIN_CONFIDENCE", "nominal")
        monkeypatch.setenv("RWI_FIRE_ALERT_MIN_FRP_MW", "0")
    get_settings.cache_clear()


def test_fires_unconfigured_makes_no_request(client: TestClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        any_call = mock.route().mock(return_value=httpx.Response(500))
        body = client.get("/api/map/fires", params={"route_id": ROUTE}).json()
        assert not any_call.called
    assert body["configured"] is False and body["features"] == []
    assert "RWI_FIRMS_MAP_KEY" in body["missing_settings"]


def test_fires_configured_returns_features_alerts_and_hides_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_firms(monkeypatch, alerts=True)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*/api/area/csv/.*").mock(
            return_value=httpx.Response(200, text=FIRMS_CSV.read_text())
        )
        resp = client.get("/api/map/fires", params={"route_id": ROUTE})
    assert KEY not in resp.text
    body = resp.json()
    assert body["configured"] is True and body["available"] is True, body["message"]
    assert len(body["features"]) == 48
    f0 = body["features"][0]
    assert f0["geometry"]["type"] == "Point" and len(f0["geometry"]["coordinates"]) == 2
    assert body["units"]["frp_mw"] == "MW"
    assert body["alerts_configured"] is True
    assert [a["id"].rsplit("-", 1)[1] for a in body["alerts"]] == ["349", "512", "804", "2979"]
    assert body["provenance"]["kind"] == "observation"


def test_fires_without_alert_thresholds_gives_layer_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_firms(monkeypatch, alerts=False)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*/api/area/csv/.*").mock(
            return_value=httpx.Response(200, text=FIRMS_CSV.read_text())
        )
        body = client.get("/api/map/fires", params={"route_id": ROUTE}).json()
    assert body["alerts_configured"] is False and body["alerts"] == []
    assert len(body["features"]) == 48


def test_fires_upstream_error_is_reported_without_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_firms(monkeypatch, alerts=False)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*/api/area/csv/.*").mock(return_value=httpx.Response(503))
        resp = client.get("/api/map/fires", params={"route_id": ROUTE})
    body = resp.json()
    assert body["configured"] is True and body["available"] is False
    assert KEY not in resp.text


def _cams(mock: respx.MockRouter) -> None:
    mock.get(url__regex=r".*/air-quality.*").mock(
        return_value=httpx.Response(200, json=json.loads(CAMS_JSON.read_text()))
    )


def test_aerosol_without_alpha_uses_climatology(client: TestClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _cams(mock)
        body = client.get("/api/map/aerosol", params={"lat": -12.46, "lon": 130.85}).json()
    assert body["available"] is True
    assert len(body["linke_turbidity"]) == 48 and not any(body["linke_turbidity_dynamic"])
    assert "RWI_AOD_ANGSTROM_EXPONENT" in body["linke_turbidity_note"]
    assert body["aerosol"]["provenance"]["kind"] == "forecast"


def test_aerosol_with_alpha_is_dynamic(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RWI_AOD_ANGSTROM_EXPONENT", "1.3")
    get_settings.cache_clear()
    hours = json.loads(CAMS_JSON.read_text())["hourly"]["time"]
    met = {
        "latitude": -12.5,
        "longitude": 130.875,
        "utc_offset_seconds": 0,
        "hourly_units": {"time": "iso8601", "temperature_2m": "°C", "relative_humidity_2m": "%"},
        "hourly": {
            "time": hours,
            "temperature_2m": [25.0] * len(hours),
            "relative_humidity_2m": [70.0] * len(hours),
        },
    }
    with respx.mock(assert_all_called=False) as mock:
        _cams(mock)
        mock.get(url__regex=r".*/forecast.*").mock(return_value=httpx.Response(200, json=met))
        mock.get(url__regex=r".*/meta\.json").mock(return_value=httpx.Response(404))
        body = client.get("/api/map/aerosol", params={"lat": -12.46, "lon": 130.85}).json()
    assert body["available"] is True
    assert all(body["linke_turbidity_dynamic"]), body["linke_turbidity_note"]
    assert body["linke_turbidity"][0] == pytest.approx(5.975, abs=0.005)
    assert body["met_provenance"]["kind"] == "forecast"


def test_aerosol_cams_down_is_unavailable(client: TestClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*/air-quality.*").mock(return_value=httpx.Response(503))
        body = client.get("/api/map/aerosol", params={"lat": -12.46, "lon": 130.85}).json()
    assert body["available"] is False and "503" in body["message"]
