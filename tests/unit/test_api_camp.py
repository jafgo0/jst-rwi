"""/api/camp/forecast: candidate-stop charging outlook, respx-mocked Open-Meteo (ADR 0015)."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from rwi.api.app import create_app
from rwi.config import get_settings

ROUTE = "bwsc-2027-stuart-highway"
# Open-Meteo serves at most 384 h ahead of today, so the stop date follows the real clock.
STOP = datetime.now(UTC).date() + timedelta(days=1)
STOP_DATE = STOP.isoformat()
_ENV = ("RWI_CAMP_GUST_WARNING_M_S", "RWI_CAMP_DEW_MARGIN_C", "RWI_CAMP_HORIZON_OFFSETS_KM")
_UNITS = {
    "time": "iso8601",
    "shortwave_radiation": "W/m²",
    "direct_normal_irradiance": "W/m²",
    "wind_gusts_10m": "km/h",
    "temperature_2m": "°C",
    "dew_point_2m": "°C",
    "wind_speed_10m": "km/h",
    "cloud_cover": "%",
    "cloud_cover_low": "%",
    "cloud_cover_mid": "%",
    "cloud_cover_high": "%",
}
_VALUES = {
    "shortwave_radiation": 150.0,
    "direct_normal_irradiance": 300.0,
    "wind_gusts_10m": 36.0,  # 10 m/s
    "temperature_2m": 9.0,
    "dew_point_2m": 7.0,
    "wind_speed_10m": 3.6,
    "cloud_cover": 20.0,
    "cloud_cover_low": 5.0,
    "cloud_cover_mid": 10.0,
    "cloud_cover_high": 15.0,
}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("RWI_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("RWI_HTTP_RETRIES", "0")
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def _payload() -> dict[str, object]:
    t0 = datetime.combine(STOP, datetime.min.time(), tzinfo=UTC)
    times = [(t0 + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(48)]
    return {
        "latitude": -23.7,
        "longitude": 133.875,
        "utc_offset_seconds": 0,
        "hourly_units": _UNITS,
        "hourly": {"time": times, **{k: [v] * len(times) for k, v in _VALUES.items()}},
    }


def _mock_ok(mock: respx.MockRouter) -> respx.Route:
    mock.get(url__regex=r".*/meta\.json").mock(return_value=httpx.Response(404))
    return mock.get(url__regex=r".*/forecast.*").mock(
        return_value=httpx.Response(200, json=_payload())
    )


def _get(client: TestClient, *distances: float, day: str = STOP_DATE) -> httpx.Response:
    params: list[tuple[str, str | float]] = [("route_id", ROUTE), ("date", day)]
    params += [("distance_km", d) for d in distances]
    return client.get("/api/camp/forecast", params=params)


def test_unconfigured_gives_clear_sky_forecast_and_notes(client: TestClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mock_ok(mock)
        resp = _get(client, 1500.0, 1520.0)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["route_id"] == ROUTE and body["stop_date_event"] == STOP_DATE
    assert len(body["notes"]) == 3
    assert any("RWI_CAMP_HORIZON_OFFSETS_KM" in n for n in body["notes"])
    [c1, c2] = body["candidates"]
    assert c1["distance_km"] == 1500.0 and c2["distance_km"] == 1520.0
    assert c1["forecast_available"] is True and c1["message"] is None
    ev = c1["camp"]["evening"]
    assert ev["clear_sky"]["provenance"]["kind"] == "clear_sky_model"
    assert ev["forecast"]["provenance"]["kind"] == "forecast"
    assert ev["gust_max_m_s"] == pytest.approx(10.0)
    assert ev["gust_warning"] is None
    assert c1["camp"]["horizon_west"] == [] and c1["camp"]["horizon_east"] == []
    dew = c1["camp"]["dew"]
    assert dew["hours"] and dew["risk_at_sunrise"] is None


def test_configured_flags_gusts_dew_and_fetches_horizon(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RWI_CAMP_GUST_WARNING_M_S", "8")
    monkeypatch.setenv("RWI_CAMP_DEW_MARGIN_C", "2")
    monkeypatch.setenv("RWI_CAMP_HORIZON_OFFSETS_KM", "[50, 100]")
    get_settings.cache_clear()
    assert get_settings().camp_horizon_offsets_km == [50.0, 100.0]
    with respx.mock(assert_all_called=False) as mock:
        route = _mock_ok(mock)
        body = _get(client, 1500.0).json()
        assert route.call_count == 1 + 2 * 2  # stop + 2 offsets x (west, east)
    assert body["notes"] == []
    camp = body["candidates"][0]["camp"]
    assert camp["evening"]["gust_warning"] is True
    assert [h["offset_km"] for h in camp["horizon_west"]] == [50.0, 100.0]
    assert [h["side"] for h in camp["horizon_east"]] == ["east", "east"]
    west = camp["horizon_west"][0]
    assert west["cloud_cover_pct"] == 20.0 and west["cloud_cover_high_pct"] == 15.0
    # Date follows the clock, so only the half-sky is fixed: sunset west, sunrise east.
    assert 180.0 < west["azimuth_deg"] < 360.0
    assert 0.0 < camp["horizon_east"][0]["azimuth_deg"] < 180.0
    assert west["provenance"]["kind"] == "forecast"
    assert camp["dew"]["margin_threshold_c"] == 2.0
    assert camp["dew"]["risk_at_sunrise"] is not None


def test_provider_error_falls_back_to_clear_sky(client: TestClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*/meta\.json").mock(return_value=httpx.Response(404))
        mock.get(url__regex=r".*/forecast.*").mock(return_value=httpx.Response(503))
        body = _get(client, 1500.0).json()
    [c] = body["candidates"]
    assert c["forecast_available"] is False
    assert "stop forecast unavailable" in c["message"] and "clear-sky only" in c["message"]
    assert c["camp"]["evening"]["clear_sky"] is not None
    assert c["camp"]["evening"]["forecast"] is None and c["camp"]["dew"] is None


def test_date_beyond_forecast_horizon_is_clear_sky_only(client: TestClient) -> None:
    far = (STOP + timedelta(days=30)).isoformat()
    with respx.mock(assert_all_called=False) as mock:
        route = _mock_ok(mock)
        body = _get(client, 1500.0, day=far).json()
        assert not route.called
    [c] = body["candidates"]
    assert c["forecast_available"] is False and "CapabilityNotSupportedError" in c["message"]
    assert c["camp"]["evening"]["clear_sky"]["provenance"]["kind"] == "clear_sky_model"


@pytest.mark.parametrize("distances", [(-1.0,), (99999.0,), tuple(float(d) for d in range(7))])
def test_bad_distances_are_422(client: TestClient, distances: tuple[float, ...]) -> None:
    with respx.mock(assert_all_called=False) as mock:
        any_call = mock.route().mock(return_value=httpx.Response(500))
        resp = _get(client, *distances)
        assert not any_call.called
    assert resp.status_code == 422


def test_negative_horizon_offset_is_rejected_by_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RWI_CAMP_HORIZON_OFFSETS_KM", "[50, -10]")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="camp_horizon_offsets_km"):
            get_settings()
    finally:
        get_settings.cache_clear()
