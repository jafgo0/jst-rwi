"""/api/meteorologist/* endpoints.

Fas 1 covers:
* the three fixed-question endpoints return a well-formed Briefing even
  when the platform has no position (every tool reports unavailable);
* the free-form /ask endpoint answers 501 when the LLM adapter is not
  configured rather than inventing prose.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from rwi.api.app import create_app
from rwi.config import get_settings


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
