"""/api/meteorologist/* endpoints.

Fas 1 covers:
* the three fixed-question endpoints return a well-formed Briefing even
  when the platform has no position (every tool reports unavailable);
* the free-form /ask endpoint answers 501 when the LLM adapter is not
  configured rather than inventing prose.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rwi.alerts.synoptic import SYNOPTIC_VARIABLES
from rwi.api.app import create_app
from rwi.api.routers import meteorologist as met_router
from rwi.config import get_settings
from rwi.contracts import WeatherSeries, WeatherStep
from rwi.core.provenance import DataKind, Provenance, measured


@pytest.fixture(autouse=True)
def _offline_synoptic(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Unit tests never touch the network: the synoptic fetch fails by default."""

    async def refuse(*_a: Any, **_k: Any) -> Any:
        raise ConnectionError("offline in unit tests")

    monkeypatch.setattr("rwi.providers.registry.fetch_with_fallback", refuse)
    met_router._reset_synoptic_cache()
    yield
    met_router._reset_synoptic_cache()


def _client() -> TestClient:
    return TestClient(create_app())


def test_risk_ahead_returns_briefing_shape_without_position() -> None:
    with _client() as client:
        response = client.get("/api/meteorologist/risk-ahead")
    assert response.status_code == 200
    body = response.json()
    assert body["question_kind"] == "risk_ahead"
    for key in (
        "assessment",
        "supporting_data",
        "uncertainty",
        "operational_consequences",
        "watch_next",
        "warnings_and_limits",
        "citations",
        "unknowns",
    ):
        assert key in body
    assert body["uncertainty"], "uncertainty section is never empty"
    # Route weather is intentionally not plumbed in Fas 1, so the
    # 'ruttprognos' gap is always present regardless of what state
    # earlier tests left behind in the process-wide StateStore.
    assert any("ruttprognos" in u.lower() for u in body["unknowns"])


def test_forecast_reliability_names_the_variable_in_assessment() -> None:
    with _client() as client:
        response = client.get(
            "/api/meteorologist/forecast-reliability", params={"variable": "ghi"}
        )
    assert response.status_code == 200
    assert "ghi" in response.json()["assessment"]


def test_morning_briefing_returns_a_well_formed_briefing() -> None:
    with _client() as client:
        response = client.get("/api/meteorologist/morning-briefing")
    assert response.status_code == 200
    body = response.json()
    assert body["question_kind"] == "morning_briefing"
    assert body["assessment"]
    assert body["uncertainty"], "uncertainty section is never empty"
    # The convection caveat lines must appear in warnings_and_limits.
    warnings = " ".join(body["warnings_and_limits"])
    assert "GENERISKA" in warnings
    assert "CIN" in warnings


def test_ask_returns_501_when_api_key_is_not_configured() -> None:
    with _client() as client:
        response = client.post(
            "/api/meteorologist/ask", json={"question": "hello"}
        )
    assert response.status_code == 501
    detail = response.json()["detail"]
    assert detail["code"] == "capability_not_configured"


def test_ask_returns_400_on_missing_question() -> None:
    with _client() as client:
        response = client.post("/api/meteorologist/ask", json={"question": "   "})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "missing_question"


def test_ask_with_injected_classifier_returns_a_briefing() -> None:
    """Rewire the LLM dependency through the FastAPI DI graph so the test
    neither hits the network nor needs an API key. The classifier picks
    the kind; the deterministic composer still builds the Briefing."""
    from rwi.api.routers import meteorologist as router_mod
    from rwi.meteorologist.contract import QuestionKind
    from rwi.meteorologist.llm import Classification

    class _StubClassifier:
        def classify(self, _q: str) -> Classification:
            return Classification(
                question_kind=QuestionKind.RISK_AHEAD, variable="ghi", raw="{}"
            )

    original = router_mod.build_classifier
    router_mod.build_classifier = lambda **_: _StubClassifier()  # type: ignore[assignment]
    try:
        with _client() as client:
            response = client.post(
                "/api/meteorologist/ask",
                json={"question": "vad är viktigast framför oss?"},
            )
    finally:
        router_mod.build_classifier = original  # type: ignore[assignment]
    assert response.status_code == 200
    body = response.json()
    assert body["question_kind"] == "risk_ahead"
    # The user's phrasing survives through the composer.
    assert body["question"] == "vad är viktigast framför oss?"
    assert body["uncertainty"], "uncertainty section is never empty"


# get_settings is cached; make sure tests don't leak state via the cache.
get_settings.cache_clear()


# ----------------------------------------------------------------------------- synoptic (#10, #14)


def _synthetic_series(point: Any, window: Any) -> WeatherSeries:
    """Ridge north of -28 deg; south of it a cold front arriving later further south.

    Front hour (from window start, which is now - 24 h) grows 4 h per degree of
    latitude south of -29, so Coober Pedy -> Port Augusta is a southward front.
    """
    lat = point.latitude_deg
    front_h = 30 + round(4 * (-lat - 29.0)) if lat < -28.0 else None
    steps = []
    t0 = window.start_utc.replace(minute=0, second=0, microsecond=0)
    for h in range(int((window.end_utc - t0).total_seconds() // 3600)):
        t = t0 + timedelta(hours=h)
        prov = Provenance(
            provider="openmeteo",
            dataset="forecast",
            kind=DataKind.FORECAST,
            retrieved_at_utc=t0,
            valid_time_utc=t,
            point=point,
        )
        diurnal = 25.0 + 6.0 * math.sin((h - 6) / 24.0 * 2.0 * math.pi)
        if front_h is None:
            vals = {
                "pressure_msl": 102000.0 + 2.0 * h,
                "wind_direction_10m": 120.0,
                "wind_speed_10m": 5.0,
                "temperature_2m": diurnal,
                "dew_point_2m": 5.0,
            }
        else:
            vals = {
                "pressure_msl": 101000.0 + 50.0 * abs(h - front_h),
                "wind_direction_10m": 330.0 if h < front_h else 220.0,
                "wind_speed_10m": 6.0,
                "temperature_2m": diurnal - (6.0 if h > front_h else 0.0),
                "dew_point_2m": 10.0 if h < front_h else 2.0,
            }
        steps.append(
            WeatherStep(
                valid_time_utc=t,
                values={k: measured(k, v, prov) for k, v in vals.items()},
            )
        )
    return WeatherSeries(
        point=point,
        requested_point=point,
        provider="openmeteo",
        dataset="forecast",
        kind=DataKind.FORECAST,
        model_name="ecmwf_ifs025",
        retrieved_at_utc=t0,
        temporal_resolution_s=3600.0,
        steps=steps,
    )


def _fake_fetch(calls: list[Any]) -> Any:
    async def fetch(_providers: Any, point: Any, window: Any, variables: Any, *, kind: Any) -> Any:
        if tuple(variables) != SYNOPTIC_VARIABLES:  # platform-position fetches of other tools
            raise ConnectionError("offline in unit tests")
        calls.append((point, window, tuple(variables), kind))
        if -20.0 < point.latitude_deg < -19.0:  # Tennant Creek
            raise TimeoutError("HTTP 503")
        return _synthetic_series(point, window)

    return fetch


def test_morning_briefing_reports_front_and_failed_point(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr("rwi.providers.registry.fetch_with_fallback", _fake_fetch(calls))
    with _client() as client:
        response = client.get("/api/meteorologist/morning-briefing", params={"hours": 24})
    assert response.status_code == 200
    body = response.json()
    # 11 named waypoints: start, nine control stops, finish.
    assert len(calls) == 11
    _point, window, variables, kind = calls[0]
    assert kind is DataKind.FORECAST
    assert "pressure_msl" in variables and "dew_point_2m" in variables
    assert window.end_utc - window.start_utc == timedelta(hours=72)
    text = response.text
    assert "front" in body["assessment"].lower()
    assert "Coober Pedy" in text
    assert "Tennant Creek" in text  # the failed point is reported, not hidden
    assert "Control Stop" not in text


def test_morning_briefing_synoptic_is_cached_between_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr("rwi.providers.registry.fetch_with_fallback", _fake_fetch(calls))
    with _client() as client:
        client.get("/api/meteorologist/morning-briefing")
        client.get("/api/meteorologist/morning-briefing")
    assert len(calls) == 11


def test_morning_briefing_lists_synoptic_gap_when_offline() -> None:
    with _client() as client:
        response = client.get("/api/meteorologist/morning-briefing")
    assert response.status_code == 200
    assert "synoptisk" in " ".join(response.json()["unknowns"]).lower()
