"""/api/v1 (ADR 0011): stable route bands and verification, honest when there is nothing."""

import asyncio
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from ml_synth import START, ghi_pairs, upstream_ghi

from rwi.api.app import create_app
from rwi.config import get_settings
from rwi.contracts import ForecastObservationPair, WeatherSeries
from rwi.core.position import ActiveState, PositionMode
from rwi.core.provenance import DataKind
from rwi.core.state import get_state_store
from rwi.ml.targets import BOM_PROVIDER

ROUTE = "bwsc-2027-stuart-highway"
#: 01:00Z on the day `upstream_ghi()` is initialised: mid-morning along the route.
NOW = START + timedelta(days=27, hours=1)


def _client(monkeypatch: pytest.MonkeyPatch, models_dir: Path) -> Iterator[TestClient]:
    # The state store is process-wide: clear any selected time or fix another test left.
    asyncio.run(get_state_store().replace(ActiveState(mode=PositionMode.MANUAL, fix=None)))
    monkeypatch.setenv("RWI_MODELS_DIR", str(models_dir))
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


def _fake_fetch(series: WeatherSeries, calls: list[tuple[str, ...]]) -> Any:
    async def fetch(_p: Any, _point: Any, _window: Any, variables: Any, *, kind: Any) -> Any:
        assert kind is DataKind.FORECAST
        calls.append(tuple(variables))
        return series

    return fetch


def _freeze(monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    for module in ("rwi.api.routers.v1", "rwi.api.routers.route"):
        monkeypatch.setattr(f"{module}.utc_now", lambda: now)


@pytest.fixture
def empty_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    yield from _client(monkeypatch, tmp_path / "no-models-here")


@pytest.fixture(scope="module")
def trained_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from ml_synth import require_lightgbm

    require_lightgbm()
    from rwi.ml.__main__ import retrain

    root = tmp_path_factory.mktemp("models")
    retrain(
        ghi_pairs(30),
        variable="ghi",
        cutoff_utc=START + timedelta(days=24),
        target_kind="bom_observation",
        models_dir=root,
        num_boost_round=40,
        model_version="v1",
    )
    return root


@pytest.fixture
def trained_client(monkeypatch: pytest.MonkeyPatch, trained_dir: Path) -> Iterator[TestClient]:
    yield from _client(monkeypatch, trained_dir)


def test_route_without_models_gives_reasons_never_fetches_and_is_stable(
    empty_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "rwi.providers.registry.fetch_with_fallback", _fake_fetch(upstream_ghi(), calls)
    )
    r = empty_client.get("/api/v1/forecast/route", params={"route_id": ROUTE, "hours": 4})
    assert r.status_code == 200
    assert r.headers["X-Rwi-Product-Tier"] == "stable"
    body = r.json()
    assert body["loaded"] == [] and calls == []
    # head/cross need the u/v models; the response says which model variables are missing.
    assert {"ghi", "wind_u_10m", "wind_v_10m"} <= set(body["missing"])
    assert body["points"]
    for p in body["points"]:
        assert p["bands"] == {} and p["upstream_source"] is None
        assert set(p["unavailable"]) == set(body["variables"])
        assert set(p["unavailable"].values()) == {"no_model"}


def test_unknown_variable_is_422(empty_client: TestClient) -> None:
    r = empty_client.get(
        "/api/v1/forecast/route", params={"route_id": ROUTE, "variables": "ghi,banana"}
    )
    assert r.status_code == 422
    # head/crosswind are route-relative: there is no model or archive row to verify them by.
    assert empty_client.get("/api/v1/verification?variables=headwind_10m").status_code == 422


def test_route_bands_come_from_the_model_with_lineage(
    trained_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "rwi.providers.registry.fetch_with_fallback", _fake_fetch(upstream_ghi(), calls)
    )
    _freeze(monkeypatch, NOW)
    r = trained_client.get(
        "/api/v1/forecast/route",
        params={"route_id": ROUTE, "hours": 6, "variables": "ghi,headwind_10m"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [m["variable"] for m in body["loaded"]] == ["ghi"]
    assert calls and all(c == ("ghi",) for c in calls)  # no wind fetched without u/v models
    banded = [p for p in body["points"] if "ghi" in p["bands"]]
    assert banded, [(p["eta_utc"], p["unavailable"]) for p in body["points"]]
    for p in body["points"]:
        assert p["unavailable"]["headwind_10m"] == "no_model"
        assert p["upstream_source"] == "open-meteo/best_match"
    for p in banded:
        band = p["bands"]["ghi"]
        assert band["ml_derived"] is True and band["unit"] == "W/m2"
        assert band["p10"] <= band["p50"] <= band["p90"]
        assert band["provenance"]["model_version"] == "v1"
        assert band["provenance"]["kind"] == DataKind.FORECAST.value
        assert band["provenance"]["training_cutoff_utc"] is not None


class _FakeArchive:
    def __init__(self, ghi: list[ForecastObservationPair]) -> None:
        self._ghi = ghi
        self.closed = False

    def pairs(
        self, *, variable: str, observation_provider: str | None = None
    ) -> list[ForecastObservationPair]:
        if variable == "ghi" and observation_provider == BOM_PROVIDER:
            return self._ghi
        return []

    def close(self) -> None:
        self.closed = True


def test_verification_scores_holdout_and_names_truth_sources(
    trained_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # truth_pairs labels each source by the provider it queried, not by pair content.
    pairs = list(ghi_pairs(30))
    archive = _FakeArchive(pairs)
    monkeypatch.setattr("rwi.archive.open_archive", lambda _s: archive)
    r = trained_client.get("/api/v1/verification", params={"variables": "ghi,dni"})
    assert r.status_code == 200, r.text
    assert r.headers["X-Rwi-Product-Tier"] == "stable"
    ghi, dni = r.json()["variables"]
    assert ghi["status"] == "ok" and ghi["model_version"] == "v1"
    assert ghi["truth_sources"] == {BOM_PROVIDER: len(pairs)}
    assert ghi["target_kind"] == "bom_observation"
    assert ghi["n_holdout"] > 0
    systems = {s["system"] for s in ghi["scores"]}
    assert "ml" in systems and len(systems) >= 2
    assert dni["status"] == "no_truth" and dni["truth_sources"] == {}
    assert archive.closed


def test_verification_without_model_says_so(
    empty_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _FakeArchive(list(ghi_pairs(5)))
    monkeypatch.setattr("rwi.archive.open_archive", lambda _s: archive)
    monkeypatch.setattr("rwi.ml.targets.TRUTH_PRIORITY", {"ghi": (BOM_PROVIDER,)}, raising=True)
    (ghi,) = empty_client.get("/api/v1/verification?variables=ghi").json()["variables"]
    assert ghi["status"] == "no_model" and ghi["reason"]
    assert ghi["truth_sources"] == {BOM_PROVIDER: len(ghi_pairs(5))}
