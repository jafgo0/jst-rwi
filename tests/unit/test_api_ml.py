"""/api/ml/* (ADR 0008 §6): bands from loaded models, or an honest "no_model"."""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from ml_synth import POINT, START, ghi_pairs, upstream_ghi

from rwi.api.app import create_app
from rwi.config import get_settings
from rwi.contracts import WeatherSeries
from rwi.core.provenance import DataKind

AT = {"lat": POINT.latitude_deg, "lon": POINT.longitude_deg}


def _client(monkeypatch: pytest.MonkeyPatch, models_dir: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("RWI_MODELS_DIR", str(models_dir))
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


@pytest.fixture
def empty_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    yield from _client(monkeypatch, tmp_path / "no-models-here")


def _fake_fetch(series: WeatherSeries, calls: list[tuple[str, ...]]) -> Any:
    async def fetch(_p: Any, _point: Any, _window: Any, variables: Any, *, kind: Any) -> Any:
        assert kind is DataKind.FORECAST
        calls.append(tuple(variables))
        return series

    return fetch


def test_without_models_bands_say_no_model_and_never_fetch(
    empty_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "rwi.providers.registry.fetch_with_fallback", _fake_fetch(upstream_ghi(), calls)
    )
    assert empty_client.get("/api/ml/models").json() == []
    assert empty_client.get("/api/ml/scores").json() == []
    r = empty_client.get("/api/ml/bands", params=AT)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "no_model"
    assert body["series"] is None and body["loaded"] == []
    assert set(body["missing"]) == {"ghi", "wind_speed_10m"}
    assert calls == []
    assert r.headers["X-Rwi-Product-Tier"] == "experimental"


@pytest.fixture(scope="module")
def trained_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from ml_synth import require_lightgbm

    require_lightgbm()
    from rwi.ml.__main__ import retrain

    root = tmp_path_factory.mktemp("models")
    retrain(
        ghi_pairs(30), variable="ghi", cutoff_utc=START + timedelta(days=24),
        target_kind="bom_observation", models_dir=root, num_boost_round=40, model_version="v1",
    )
    return root


@pytest.fixture
def trained_client(monkeypatch: pytest.MonkeyPatch, trained_dir: Path) -> Iterator[TestClient]:
    yield from _client(monkeypatch, trained_dir)


def test_scores_and_models_list_the_saved_version(trained_client: TestClient) -> None:
    (score,) = trained_client.get("/api/ml/scores").json()
    assert (score["model_id"], score["model_version"]) == ("pp-ghi", "v1")
    assert score["metrics"]["crps"] is not None and score["n_holdout"] > 0
    (manifest,) = trained_client.get("/api/ml/models").json()
    assert manifest["feature_schema_hash"]


def test_bands_are_a_labelled_forecast_from_the_loaded_model(
    trained_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "rwi.providers.registry.fetch_with_fallback", _fake_fetch(upstream_ghi(), calls)
    )
    body = trained_client.get("/api/ml/bands", params=AT).json()
    assert body["status"] == "ok"
    assert calls == [("ghi",)]  # only variables a model exists for are fetched
    assert [m["model_version"] for m in body["loaded"]] == ["v1"]
    assert "wind_speed_10m" in body["missing"]
    series = WeatherSeries.model_validate(body["series"])
    assert series.provider == "rwi" and series.kind is DataKind.FORECAST
    for step in series.steps:
        m = step.values["ghi"]
        assert m.provenance.kind is DataKind.FORECAST
        assert m.uncertainty is not None and m.value is not None
        assert m.uncertainty.lower <= m.value <= m.uncertainty.upper


def test_bands_from_an_unknown_upstream_are_not_applicable(
    trained_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = upstream_ghi().model_copy(update={"provider": "met-norway"})
    monkeypatch.setattr("rwi.providers.registry.fetch_with_fallback", _fake_fetch(other, []))
    body = trained_client.get("/api/ml/bands", params=AT).json()
    assert body["status"] == "not_applicable" and body["series"] is None
    assert "met-norway" in body["reason"]
