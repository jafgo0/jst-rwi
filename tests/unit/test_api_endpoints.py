"""API behaviour, including the states the platform must show honestly."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rwi.api import capability
from rwi.api.app import create_app
from rwi.core.position import ActiveState, PositionMode
from rwi.core.state import get_state_store

NIGHT = "2026-01-15T23:00:00Z"
RACE_NOON = "2027-08-24T03:00:00Z"


@pytest.fixture
def client() -> Iterator[TestClient]:
    store = get_state_store()
    # Each test starts with no position: the platform must not assume one.
    import asyncio

    asyncio.run(store.replace(ActiveState(mode=PositionMode.MANUAL, fix=None)))
    with TestClient(create_app()) as test_client:
        yield test_client


def test_health_reports_what_the_build_can_do(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["status"] in {"ok", "degraded"}
    assert body["version"]
    assert set(body["capabilities"]) >= {"solar", "providers", "route", "energy", "strategy"}
    # Anything unavailable must be explained, not merely absent.
    for module, reason in body["unavailable_modules"].items():
        assert module and reason


def test_without_a_position_the_api_refuses_to_guess(client: TestClient) -> None:
    response = client.get("/api/solar/position")
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PositionUnavailableError"
    assert "no position available" in error["message"]


def test_half_a_coordinate_is_a_client_error(client: TestClient) -> None:
    assert client.get("/api/solar/position", params={"lat": 57.78}).status_code == 400


def test_coordinates_are_range_checked(client: TestClient) -> None:
    response = client.get("/api/solar/position", params={"lat": 100.0, "lon": 14.0})
    assert response.status_code == 422


def test_night_in_jonkoping_has_the_sun_below_the_horizon(client: TestClient) -> None:
    """The master specification's critical night-time test, through the HTTP layer."""
    params = {"lat": 57.7826, "lon": 14.1618, "elev": 227.0, "time": NIGHT}
    position = client.get("/api/solar/position", params=params).json()
    assert position["apparent_elevation_deg"] < 0.0
    assert position["air_mass"] is None

    clearsky = client.get("/api/solar/clearsky", params=params).json()
    assert clearsky["ghi_w_m2"] == 0.0
    assert clearsky["dni_w_m2"] == 0.0
    assert clearsky["dhi_w_m2"] == 0.0
    # And it is labelled a model result, never a measurement.
    assert clearsky["kind"] == "clear_sky_model"
    assert clearsky["provenance"]["provider"]


def test_australian_position_drives_the_calculation(client: TestClient) -> None:
    alice = client.get(
        "/api/solar/position",
        params={"lat": -23.6980, "lon": 133.8807, "elev": 545.0, "time": RACE_NOON},
    ).json()
    jonkoping = client.get(
        "/api/solar/position",
        params={"lat": 57.7826, "lon": 14.1618, "elev": 227.0, "time": RACE_NOON},
    ).json()
    assert alice["apparent_elevation_deg"] > 40.0
    # Southern-hemisphere winter: the midday sun stands to the north.
    assert alice["azimuth_deg"] < 45.0 or alice["azimuth_deg"] > 315.0
    assert abs(alice["apparent_elevation_deg"] - jonkoping["apparent_elevation_deg"]) > 30.0


def test_setting_a_position_resolves_local_time_from_that_position(client: TestClient) -> None:
    response = client.post(
        "/api/state/position",
        json={
            "mode": "live_gps",
            "latitude_deg": -14.4652,
            "longitude_deg": 132.2635,
            "elevation_m": 108.0,
            "heading_deg": 181.0,
            "source": "vehicle_telemetry",
        },
    )
    body = response.json()
    assert body["position_available"] is True
    assert body["is_simulated"] is False
    assert body["timezone_name"] == "Australia/Darwin"
    assert body["utc_offset_hours"] == pytest.approx(9.5)
    assert body["state"]["fix"]["fix_quality"] == "gps"


def test_simulated_position_is_flagged_for_the_ui(client: TestClient) -> None:
    body = client.post(
        "/api/state/position",
        json={
            "mode": "route_sim",
            "latitude_deg": -19.65,
            "longitude_deg": 134.19,
            "source": "route_sim",
        },
    ).json()
    assert body["is_simulated"] is True
    assert body["state"]["mode"] == "route_sim"
    assert body["state"]["fix"]["fix_quality"] == "simulated"


def test_freezing_and_releasing_time(client: TestClient) -> None:
    client.post(
        "/api/state/position",
        json={"mode": "manual", "latitude_deg": 57.7826, "longitude_deg": 14.1618},
    )
    frozen = client.post("/api/state/time", json={"selected_time_utc": NIGHT}).json()
    assert frozen["state"]["selected_time_utc"].startswith("2026-01-15T23:00")
    assert frozen["effective_time_utc"].startswith("2026-01-15T23:00")
    # With time frozen, the active position's solar geometry follows the chosen time.
    position = client.get("/api/solar/position").json()
    assert position["apparent_elevation_deg"] < 0.0

    released = client.post("/api/state/time", json={"selected_time_utc": None}).json()
    assert released["state"]["selected_time_utc"] is None


def test_variable_registry_is_exposed_with_units(client: TestClient) -> None:
    body = client.get("/api/weather/variables").json()
    assert body["temperature_2m"]["unit"] == "degC"
    assert body["pressure_msl"]["unit"] == "Pa"
    assert body["wind_speed_10m"]["unit"] == "m/s"
    assert body["ghi"]["unit"] == "W/m2"


def test_a_missing_module_answers_501_naming_what_is_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Degradation must be explicit. No endpoint may invent a placeholder answer."""

    def _unavailable(module_name: str) -> Any:
        capability._IMPORT_ERRORS[module_name] = "ModuleNotFoundError: simulated absence"
        return None

    monkeypatch.setattr(capability, "_try_import", _unavailable)
    client.post(
        "/api/state/position",
        json={"mode": "manual", "latitude_deg": -23.698, "longitude_deg": 133.8807},
    )
    response = client.get("/api/weather/forecast")
    assert response.status_code == 501
    detail = response.json()["detail"]["error"]
    assert detail["code"] == "CapabilityNotImplemented"
    assert "simulated absence" in detail["detail"]


def test_openapi_schema_is_generated(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    paths = set(schema["paths"])
    assert {"/api/health", "/api/state", "/api/solar/position", "/api/alerts"} <= paths


def test_regulations_endpoint_cites_clauses_and_names_the_gaps(client: TestClient) -> None:
    body = client.get("/api/regulations").json()
    challenger = body["class_limits"]["challenger"]
    assert challenger["max_collector_area_m2"] == 6.0
    assert challenger["max_energy_storage_mj"] == 11.0
    assert "2.4.2" in challenger["clauses"]
    assert body["prohibited_cell_materials"] == ["GaAs", "CdTe", "CuInSe2"]
    assert body["not_yet_published"]
    assert body["silent_in_regulations"]


def test_route_and_strategy_degrade_honestly_without_providers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no weather source, the route and strategy views must say so rather
    than modelling the race against invented weather."""

    def _unavailable(module_name: str) -> Any:
        if module_name.startswith("rwi.providers"):
            capability._IMPORT_ERRORS[module_name] = "ModuleNotFoundError: simulated absence"
            return None
        import importlib

        return importlib.import_module(module_name)

    monkeypatch.setattr(capability, "_try_import", _unavailable)
    client.post(
        "/api/state/position",
        json={"mode": "manual", "latitude_deg": -23.698, "longitude_deg": 133.8807},
    )
    for path in ("/api/energy/forecast", "/api/strategy/scenarios"):
        response = client.get(path)
        assert response.status_code == 501, path
        assert "simulated absence" in response.json()["detail"]["error"]["detail"]


def test_alerts_survive_a_total_provider_outage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alerting is the last thing that may fail: with no weather at all it must
    still report the platform's own blindness."""

    def _unavailable(module_name: str) -> Any:
        if module_name.startswith("rwi.providers"):
            capability._IMPORT_ERRORS[module_name] = "ModuleNotFoundError: simulated absence"
            return None
        import importlib

        return importlib.import_module(module_name)

    monkeypatch.setattr(capability, "_try_import", _unavailable)
    alerts = client.get("/api/alerts").json()
    assert isinstance(alerts, list)
    ids = {a["id"] for a in alerts}
    assert "position-unavailable" in ids
    position_alert = next(a for a in alerts if a["id"] == "position-unavailable")
    assert position_alert["severity"] == "critical"


def test_health_is_degraded_when_a_module_is_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _unavailable(module_name: str) -> Any:
        capability._IMPORT_ERRORS[module_name] = "ModuleNotFoundError: simulated absence"
        return None

    monkeypatch.setattr(capability, "_try_import", _unavailable)
    body = client.get("/api/health").json()
    assert body["status"] == "degraded"
    assert not any(body["capabilities"].values())


def test_health_publishes_the_freshness_limits_the_ui_must_use(client: TestClient) -> None:
    """The dashboard marks data stale; if it hardcoded the limits they would
    drift away from the backend's. So the backend publishes them."""
    limits = client.get("/api/health").json()["freshness_limits_s"]
    assert set(limits) == {"observation", "forecast", "gps"}
    assert all(isinstance(v, (int, float)) and v > 0 for v in limits.values())
    # An observation may never be considered fresh for longer than a forecast.
    assert limits["observation"] <= limits["forecast"]
    assert limits["gps"] < limits["observation"]


def test_observable_variables_are_advertised_rather_than_guessed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client must be able to ask what is MEASURED, instead of requesting a
    variable and discovering by 501 that nobody measures it."""

    class _Capabilities:
        supports_observations = True
        variables = frozenset({"temperature_2m", "wind_speed_10m"})

    class _Measuring:
        capabilities = _Capabilities()

    class _ModelOnly:
        class capabilities:
            supports_observations = False
            variables = frozenset({"ghi"})

    import types

    stub = types.ModuleType("rwi.providers.registry")
    stub.available_providers = lambda _settings: {  # type: ignore[attr-defined]
        "measuring": _Measuring(),
        "model-only": _ModelOnly(),
    }
    real_import = capability._try_import

    def _patched(module_name: str) -> Any:
        return stub if module_name == "rwi.providers.registry" else real_import(module_name)

    monkeypatch.setattr(capability, "_try_import", _patched)
    body = client.get("/api/weather/observable").json()
    # A provider that only models anything is absent, not listed with an empty set.
    assert set(body) == {"measuring"}
    assert body["measuring"] == ["temperature_2m", "wind_speed_10m"]
