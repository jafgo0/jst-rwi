"""API behaviour, including the states the platform must show honestly."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rwi.api import capability
from rwi.api.app import create_app
from rwi.config import get_settings
from rwi.core.position import ActiveState, PositionMode
from rwi.core.state import get_state_store

NIGHT = "2026-01-15T23:00:00Z"
RACE_NOON = "2027-08-24T03:00:00Z"
#: Shared secret used by tests that exercise live_gps over HTTP. Any string
#: works - production uses a deployment-specific value in RWI_TELEMETRY_SHARED_SECRET.
TEST_TELEMETRY_SECRET = "test-secret"
TELEMETRY_HEADERS = {"X-Telemetry-Secret": TEST_TELEMETRY_SECRET}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    store = get_state_store()
    # Each test starts with no position: the platform must not assume one.
    import asyncio

    # Enable the live_gps HTTP path for the tests that need it. The cache on
    # get_settings() is why we clear it rather than patching the instance.
    monkeypatch.setenv("RWI_TELEMETRY_SHARED_SECRET", TEST_TELEMETRY_SECRET)
    get_settings.cache_clear()

    asyncio.run(store.replace(ActiveState(mode=PositionMode.MANUAL, fix=None)))
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


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
        headers=TELEMETRY_HEADERS,
    )
    body = response.json()
    assert body["position_available"] is True
    assert body["is_simulated"] is False
    assert body["timezone_name"] == "Australia/Darwin"
    assert body["utc_offset_hours"] == pytest.approx(9.5)
    assert body["state"]["fix"]["fix_quality"] == "gps"


def test_live_gps_requires_the_shared_secret_header(client: TestClient) -> None:
    """Without a valid X-Telemetry-Secret header, mode=live_gps must be refused:
    live_gps is the one mode downstream consumers treat as 'real car on real
    road', and letting any caller claim it would let a browser lie about being
    the car."""
    response = client.post(
        "/api/state/position",
        json={
            "mode": "live_gps",
            "latitude_deg": -14.4652,
            "longitude_deg": 132.2635,
            "source": "attacker",
        },
    )
    assert response.status_code == 403
    assert "X-Telemetry-Secret" in response.json()["detail"]


def test_live_gps_with_a_wrong_secret_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/state/position",
        json={
            "mode": "live_gps",
            "latitude_deg": -14.4652,
            "longitude_deg": 132.2635,
            "source": "attacker",
        },
        headers={"X-Telemetry-Secret": "wrong-value"},
    )
    assert response.status_code == 403


def test_manual_and_route_sim_modes_do_not_require_the_secret(client: TestClient) -> None:
    """Non-authoritative modes stay open: operator and sim entry must not be
    gated by the telemetry secret."""
    for mode in ("manual", "route_sim"):
        response = client.post(
            "/api/state/position",
            json={
                "mode": mode,
                "latitude_deg": -14.4652,
                "longitude_deg": 132.2635,
                "source": "operator",
            },
        )
        assert response.status_code == 200, mode


def test_a_gps_timestamp_in_the_future_is_rejected(client: TestClient) -> None:
    """Clock-skewed senders could otherwise poison the state with a sample
    stamped well ahead of real time. Grace is a couple of seconds; anything
    further is a clock bug."""
    from datetime import UTC, datetime, timedelta

    ahead = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    response = client.post(
        "/api/state/position",
        json={
            "mode": "live_gps",
            "latitude_deg": -14.4652,
            "longitude_deg": 132.2635,
            "gps_time_utc": ahead,
            "source": "vehicle_telemetry",
        },
        headers=TELEMETRY_HEADERS,
    )
    assert response.status_code == 400
    assert "future" in response.json()["detail"].lower()


def test_an_implausible_live_gps_jump_is_parked_as_suspect_not_promoted(
    client: TestClient,
) -> None:
    """A second live_gps fix that implies ground speed over the threshold must
    NOT become the active position. The last trusted fix stays authoritative
    for weather/route/strategy; the suspect fix is reported alongside so the
    operator can see the discrepancy."""
    first = client.post(
        "/api/state/position",
        json={
            "mode": "live_gps",
            "latitude_deg": -14.4652,
            "longitude_deg": 132.2635,
            "source": "vehicle_telemetry",
        },
        headers=TELEMETRY_HEADERS,
    )
    assert first.status_code == 200
    # ~111 km north at any plausible cadence - implied speed well over 110 m/s.
    second = client.post(
        "/api/state/position",
        json={
            "mode": "live_gps",
            "latitude_deg": -13.4652,
            "longitude_deg": 132.2635,
            "source": "vehicle_telemetry",
        },
        headers=TELEMETRY_HEADERS,
    )
    assert second.status_code == 200
    body = second.json()
    # The trusted fix must still be the first one, not the suspect jump.
    assert body["state"]["fix"]["point"]["latitude_deg"] == pytest.approx(-14.4652)
    assert body["state"]["fix"]["fix_quality"] == "gps"
    # The suspect fix is visible to the operator.
    assert body["suspect_fix"] is not None
    assert body["suspect_fix"]["latitude_deg"] == pytest.approx(-13.4652)
    assert body["suspect_fix"]["jump_m"] > 100_000.0


def test_an_operator_fix_clears_a_previously_parked_suspect(client: TestClient) -> None:
    """When the operator posts a confirming fix (manual or a fresh trusted
    live_gps), any earlier suspect fix must be cleared: it no longer applies
    to the current trusted state."""
    client.post(
        "/api/state/position",
        json={
            "mode": "live_gps",
            "latitude_deg": -14.4652,
            "longitude_deg": 132.2635,
            "source": "vehicle_telemetry",
        },
        headers=TELEMETRY_HEADERS,
    )
    client.post(
        "/api/state/position",
        json={
            "mode": "live_gps",
            "latitude_deg": -13.4652,
            "longitude_deg": 132.2635,
            "source": "vehicle_telemetry",
        },
        headers=TELEMETRY_HEADERS,
    )
    cleared = client.post(
        "/api/state/position",
        json={
            "mode": "manual",
            "latitude_deg": -13.4652,
            "longitude_deg": 132.2635,
            "source": "operator",
        },
    ).json()
    assert cleared["suspect_fix"] is None


def test_a_naive_gps_timestamp_is_a_client_error_not_a_server_crash(
    client: TestClient,
) -> None:
    """A client that sent a bare local timestamp (no offset) used to make the
    subtraction against the server clock raise TypeError and return 500.
    The contract is simple: supply a UTC offset or get a 400."""
    response = client.post(
        "/api/state/position",
        json={
            "mode": "live_gps",
            "latitude_deg": -14.4652,
            "longitude_deg": 132.2635,
            "gps_time_utc": "2026-11-07T15:30:00",  # no timezone
            "source": "vehicle_telemetry",
        },
        headers=TELEMETRY_HEADERS,
    )
    assert response.status_code == 400
    assert "UTC offset" in response.json()["detail"]


def test_two_live_gps_fixes_sharing_a_timestamp_park_the_second_as_suspect(
    client: TestClient,
) -> None:
    """A stream that emits two different positions with the same gps_time_utc
    cannot compute an implied speed, but two different places "at the same
    instant" is itself a bug. Park, do not promote."""
    from datetime import UTC, datetime

    stamp = (datetime.now(UTC).replace(microsecond=0)).isoformat().replace("+00:00", "Z")
    first = client.post(
        "/api/state/position",
        json={
            "mode": "live_gps",
            "latitude_deg": -14.4652,
            "longitude_deg": 132.2635,
            "gps_time_utc": stamp,
            "source": "vehicle_telemetry",
        },
        headers=TELEMETRY_HEADERS,
    )
    assert first.status_code == 200
    second = client.post(
        "/api/state/position",
        json={
            "mode": "live_gps",
            "latitude_deg": -14.5000,  # different position
            "longitude_deg": 132.2635,
            "gps_time_utc": stamp,  # same instant
            "source": "vehicle_telemetry",
        },
        headers=TELEMETRY_HEADERS,
    )
    assert second.status_code == 200
    body = second.json()
    assert body["state"]["fix"]["point"]["latitude_deg"] == pytest.approx(-14.4652)
    assert body["suspect_fix"] is not None
    assert body["suspect_fix"]["latitude_deg"] == pytest.approx(-14.5000)


def test_a_duplicate_live_gps_retry_is_rejected_quietly(
    client: TestClient,
) -> None:
    """A retry sending the identical fix (same timestamp, same place) is a
    duplicate, not a new report. The second POST returns 400 so a confused
    caller sees the problem, and the state is untouched."""
    from datetime import UTC, datetime

    stamp = (datetime.now(UTC).replace(microsecond=0)).isoformat().replace("+00:00", "Z")
    payload = {
        "mode": "live_gps",
        "latitude_deg": -14.4652,
        "longitude_deg": 132.2635,
        "gps_time_utc": stamp,
        "source": "vehicle_telemetry",
    }
    first = client.post("/api/state/position", json=payload, headers=TELEMETRY_HEADERS)
    assert first.status_code == 200
    duplicate = client.post("/api/state/position", json=payload, headers=TELEMETRY_HEADERS)
    assert duplicate.status_code == 400
    assert "duplicate" in duplicate.json()["detail"].lower()


def test_a_live_gps_fix_dated_earlier_than_the_previous_one_is_rejected(
    client: TestClient,
) -> None:
    """Monotonicity: live_gps samples that arrive out of order must not
    rewind the trusted state. Rejected 400 rather than silently parked:
    this is a sender bug the operator wants to see."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC).replace(microsecond=0)
    recent = now.isoformat().replace("+00:00", "Z")
    older = (now - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    client.post(
        "/api/state/position",
        json={
            "mode": "live_gps",
            "latitude_deg": -14.4652,
            "longitude_deg": 132.2635,
            "gps_time_utc": recent,
            "source": "vehicle_telemetry",
        },
        headers=TELEMETRY_HEADERS,
    )
    rewind = client.post(
        "/api/state/position",
        json={
            "mode": "live_gps",
            "latitude_deg": -14.4700,
            "longitude_deg": 132.2635,
            "gps_time_utc": older,  # dated earlier than the previous accepted fix
            "source": "vehicle_telemetry",
        },
        headers=TELEMETRY_HEADERS,
    )
    assert rewind.status_code == 400
    assert "monotonicity" in rewind.json()["detail"].lower()


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


def test_an_unreadable_archive_answers_honestly_instead_of_crashing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening an archive can succeed while reading it fails - an older file, a
    corrupt page. That must read as "unavailable, here is why", not as a 500."""

    class BrokenArchive:
        def coverage(self) -> dict[str, object]:
            raise RuntimeError("no such column: v.retrieved_at_utc")

        def close(self) -> None:
            return None

    import types

    stub = types.ModuleType("rwi.archive")
    stub.open_archive = lambda _settings: BrokenArchive()  # type: ignore[attr-defined]
    real_import = capability._try_import

    def _patched(module_name: str) -> Any:
        return stub if module_name == "rwi.archive" else real_import(module_name)

    monkeypatch.setattr(capability, "_try_import", _patched)
    response = client.get("/api/forecast/archive")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert "no such column" in body["message"]
    assert "recreate it" in body["message"]


def test_all_models_failing_returns_503_with_the_reasons(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When every model is down, the per-model reasons are the only useful
    thing we have. A bare 500 would throw them away and blame ourselves."""

    class DeadProvider:
        async def fetch_forecast(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("HTTP 503 from open-meteo: The service is overloaded")

    import types

    registry = types.ModuleType("rwi.providers.registry")
    registry.available_providers = lambda _s: {"open-meteo": DeadProvider()}  # type: ignore[attr-defined]
    real_import = capability._try_import

    def _patched(module_name: str) -> Any:
        return registry if module_name == "rwi.providers.registry" else real_import(module_name)

    monkeypatch.setattr(capability, "_try_import", _patched)
    client.post(
        "/api/state/position",
        json={"mode": "manual", "latitude_deg": -23.698, "longitude_deg": 133.8807},
    )
    response = client.get("/api/forecast/models", params={"variable": "cloud_cover"})
    assert response.status_code == 503
    error = response.json()["detail"]["error"]
    assert error["code"] == "ProviderUnavailableError"
    assert "all failed" in error["message"]
    assert "overloaded" in error["detail"]


def test_capture_requires_the_shared_secret(client: TestClient) -> None:
    response = client.post("/api/forecast/capture")
    assert response.status_code == 403


def test_capture_is_refused_while_capture_is_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RWI_CAPTURE_ENABLED", "false")
    get_settings.cache_clear()
    response = client.post("/api/forecast/capture", headers=TELEMETRY_HEADERS)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "capture_disabled"


@pytest.mark.parametrize("step", ["search", "download"])
def test_satcorps_network_failure_is_a_typed_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    # earthaccess raises requests exceptions (OSError subclasses) when NASA
    # Earthdata is unreachable; the endpoint must answer 503, not crash with 500.
    import requests

    from rwi.api.routers import satellite as satellite_router
    from rwi.satellite import satcorps

    def boom(*_a: Any, **_k: Any) -> Any:
        raise requests.ConnectionError("earthdata down")

    if step == "search":
        monkeypatch.setattr(satellite_router, "_cached_latest_scene", boom)
    else:
        monkeypatch.setattr(satellite_router, "_cached_latest_scene", lambda _h: object())
        monkeypatch.setattr(satcorps, "download_scene", boom)

    response = client.get(
        "/api/satellite/satcorps-crosscheck", params={"lat": -12.46, "lon": 130.84}
    )
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "ProviderUnavailableError"
    assert "ConnectionError" in error["message"]


def test_route_weather_window_follows_the_selected_time(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the trajectory starts at the selected time but the horizon was
    anchored to the wall clock, so a selected time more than `hours` ahead gave 0
    samples. The window must follow the effective time."""
    import types
    from datetime import UTC, datetime, timedelta

    from rwi.contracts import WeatherSeries, WeatherStep
    from rwi.core.provenance import DataKind, Provenance, measured

    selected = (datetime.now(UTC) + timedelta(hours=30)).replace(
        minute=0, second=0, microsecond=0
    )

    async def fetch(_providers: Any, point: Any, window: Any, _vars: Any, *, kind: Any) -> Any:
        steps = []
        t = window.start_utc.replace(minute=0, second=0, microsecond=0)
        while t <= window.end_utc + timedelta(hours=1):
            prov = Provenance(
                provider="stub", dataset="forecast", kind=DataKind.FORECAST,
                retrieved_at_utc=datetime.now(UTC), valid_time_utc=t, point=point,
            )
            steps.append(WeatherStep(
                valid_time_utc=t, values={"temperature_2m": measured("temperature_2m", 30.0, prov)}
            ))
            t += timedelta(hours=1)
        return WeatherSeries(
            point=point, requested_point=point, provider="stub", dataset="forecast",
            kind=DataKind.FORECAST, retrieved_at_utc=datetime.now(UTC),
            temporal_resolution_s=3600.0, steps=steps,
        )

    stub = types.ModuleType("rwi.providers.registry")
    stub.available_providers = lambda _s: {"stub": object()}  # type: ignore[attr-defined]
    stub.fetch_with_fallback = fetch  # type: ignore[attr-defined]
    real_import = capability._try_import
    monkeypatch.setattr(
        capability, "_try_import",
        lambda name: stub if name == "rwi.providers.registry" else real_import(name),
    )
    client.post("/api/state/time", json={"selected_time_utc": selected.isoformat()})

    route_id = client.get("/api/route").json()[0]["id"]
    response = client.get(
        f"/api/route/{route_id}/weather", params={"hours": 10, "start_distance_m": 0}
    )
    assert response.status_code == 200, response.text
    samples = response.json()["forecast"]["samples"]
    assert len(samples) > 0
    first_eta = datetime.fromisoformat(samples[0]["trajectory_point"]["eta_utc"])
    assert first_eta >= selected
