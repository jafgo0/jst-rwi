"""Ensemble front-passage quantiles, alert rule and /api/v1/fronts (ADR 0012).

Members are the idealised cold fronts of test_alerts_synoptic, whose passage the
detector places at exactly the hour it was built at. The expected quantiles are
numpy's default linear interpolation (Hyndman & Fan 1996, definition 7) of those
hours, so each number below can be checked by hand.
"""

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from test_alerts_synoptic import NOW, _front, _pt, _ridge

from rwi.alerts.fronts import (
    MIN_MEMBERS_FOR_QUANTILES,
    front_alerts,
    stop_front_passage,
)
from rwi.api.app import create_app
from rwi.config import get_settings
from rwi.contracts import AlertSeverity, EnsembleForecast, WeatherSeries
from rwi.core.geo import GeoPoint
from rwi.core.position import ActiveState, PositionMode
from rwi.core.provenance import DataKind
from rwi.core.state import get_state_store

POINT = _pt(-30.0)
#: Series start at NOW; the detector needs 24 h before a passage for the trough detrend.
T0 = NOW + timedelta(hours=24)
HORIZON = timedelta(hours=36)
HOURS = 72


def _ensemble(*members: WeatherSeries) -> EnsembleForecast:
    return EnsembleForecast(
        point=POINT,
        provider="open-meteo",
        model_name="ecmwf_ifs025",
        retrieved_at_utc=NOW,
        members=list(members),
    )


def _empty() -> WeatherSeries:
    return _ridge(POINT, hours=HOURS).model_copy(update={"steps": []})


def _stop(ens: EnsembleForecast | None, **kw: Any) -> Any:
    return stop_front_passage("Glendambo", 2_400_000.0, POINT, ens, now=T0, horizon=HORIZON, **kw)


@pytest.mark.science
def test_probability_and_conditional_linear_quantiles() -> None:
    hours = [30, 36, 42, 48]
    ens = _ensemble(
        *(_front(POINT, at=h, hours=HOURS) for h in hours), _ridge(POINT, HOURS), _empty()
    )
    s = _stop(ens)
    # The ridge member counts in the denominator; the empty one is not evaluated.
    assert (s.members_total, s.members_evaluated, s.members_with_front) == (6, 5, 4)
    assert s.probability == pytest.approx(4 / 5)
    # Linear quantiles of 30/36/42/48 h: 31.8, 39.0, 46.2 h after NOW.
    expected = np.quantile(hours, [0.1, 0.5, 0.9])
    assert list(expected) == pytest.approx([31.8, 39.0, 46.2])
    for got, h in zip((s.p10_utc, s.p50_utc, s.p90_utc), expected, strict=True):
        assert got == NOW + timedelta(hours=float(h))
    assert s.quantiles_conditional_on_front and s.quantile_reason is None
    assert s.member_passages_utc == [NOW + timedelta(hours=h) for h in hours]
    assert s.provenance is not None
    assert s.provenance.kind is DataKind.DERIVED and s.provenance.dataset == "fronts"
    assert s.provenance.valid_time_utc == s.p50_utc
    assert s.provenance.model_name == "ecmwf_ifs025"
    assert "5 members" in (s.provenance.processing or "")


@pytest.mark.science
def test_passages_outside_now_to_horizon_are_ignored() -> None:
    # 30 h is before now (31 h); 70 h is past now + 36 h. Only 40 h counts.
    ens = _ensemble(*(_front(POINT, at=h, hours=80) for h in (30, 40, 70)))
    s = stop_front_passage(
        "Glendambo", 0.0, POINT, ens, now=NOW + timedelta(hours=31), horizon=HORIZON
    )
    assert s.members_with_front == 1 and s.member_passages_utc == [NOW + timedelta(hours=40)]


def test_too_few_fronts_give_no_quantiles_and_a_reason() -> None:
    fronts = [_front(POINT, at=36, hours=HOURS)] * (MIN_MEMBERS_FOR_QUANTILES - 1)
    s = _stop(_ensemble(*fronts, _ridge(POINT, HOURS)))
    assert s.probability == pytest.approx(2 / 3)
    assert s.p10_utc is s.p50_utc is s.p90_utc is None
    assert s.quantile_reason and str(MIN_MEMBERS_FOR_QUANTILES) in s.quantile_reason


def test_all_members_without_data() -> None:
    s = _stop(_ensemble(_empty(), _empty()))
    assert s.members_evaluated == 0 and s.probability is None
    assert s.quantile_reason == "no ensemble member had data"


def test_failed_fetches_are_recorded_and_aifs_is_kept_apart() -> None:
    s = _stop(None, ensemble_error="TimeoutError", deterministic_error="HTTP 500")
    assert s.error == "TimeoutError" and s.probability is None
    assert s.deterministic is not None and s.deterministic.error == "HTTP 500"

    ens = _ensemble(*(_front(POINT, at=h, hours=HOURS) for h in (30, 36, 42)))
    s = _stop(ens, deterministic=_front(POINT, at=50, hours=HOURS))
    assert s.deterministic is not None
    assert s.deterministic.passage_time_utc == NOW + timedelta(hours=50)
    assert s.deterministic.model_name == "ecmwf_aifs025_single"
    # AIFS is not a member: the ensemble quantiles stay those of 30/36/42 h.
    assert s.p90_utc == NOW + timedelta(hours=float(np.quantile([30, 36, 42], 0.9)))
    assert s.members_evaluated == 3


def _outlook_stops() -> list[Any]:
    certain = _ensemble(*(_front(POINT, at=h, hours=HOURS) for h in (34, 36, 38)))
    half = _ensemble(
        *(_front(POINT, at=h, hours=HOURS) for h in (34, 36, 38)),
        *(_ridge(POINT, HOURS) for _ in range(3)),
    )
    none = _ensemble(_ridge(POINT, HOURS))
    return [
        stop_front_passage(name, 0.0, POINT, e, now=T0, horizon=HORIZON)
        for name, e in (("Port Augusta", certain), ("Glendambo", half), ("Coober Pedy", none))
    ]


def test_alert_severity_follows_the_configured_probabilities() -> None:
    alerts = front_alerts(_outlook_stops(), watch=0.4, warning=0.9, now=T0)
    by_id = {a.id: a for a in alerts}
    assert set(by_id) == {"front-port-augusta", "front-glendambo"}
    pa, gl = by_id["front-port-augusta"], by_id["front-glendambo"]
    assert pa.severity is AlertSeverity.WARNING and gl.severity is AlertSeverity.WATCH
    assert pa.valid_window is not None
    assert pa.valid_window.start_utc == NOW + timedelta(hours=34.4)
    assert pa.valid_window.end_utc == NOW + timedelta(hours=37.6)
    assert any("3/3" in e for e in pa.evidence)
    assert "humans decide" in (pa.recommended_action or "")


def test_no_alerts_without_thresholds_and_bad_thresholds_refused() -> None:
    stops = _outlook_stops()
    assert front_alerts(stops, watch=None, warning=0.9, now=T0) == []
    assert front_alerts(stops, watch=0.4, warning=None, now=T0) == []
    with pytest.raises(ValueError, match="watch <= warning"):
        front_alerts(stops, watch=0.9, warning=0.4, now=T0)


# ----------------------------------------------------------------------------- endpoint


class _Caps:
    supports_ensemble = True


class _FakeOpenMeteo:
    capabilities = _Caps()

    def __init__(self, fail_ensemble_at: float | None = None) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.fail_at = fail_ensemble_at

    async def fetch_ensemble(
        self, point: GeoPoint, _w: Any, _v: Any, *, models: str | None = None
    ) -> EnsembleForecast:
        self.calls.append(("ensemble", models))
        if self.fail_at is not None and point.latitude_deg == self.fail_at:
            raise TimeoutError("slow")
        members = [_front(point, at=h, hours=HOURS) for h in (34, 36, 38)]
        return EnsembleForecast(
            point=point,
            provider="open-meteo",
            model_name=models,
            retrieved_at_utc=NOW,
            members=members,
        )

    async def fetch_forecast(
        self, point: GeoPoint, _w: Any, _v: Any, *, models: str | None = None
    ) -> WeatherSeries:
        self.calls.append(("forecast", models))
        return _ridge(point, HOURS)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from rwi.api.routers import v1

    asyncio.run(get_state_store().replace(ActiveState(mode=PositionMode.MANUAL, fix=None)))
    v1._reset_fronts_cache()
    monkeypatch.setattr("rwi.api.routers.v1.utc_now", lambda: T0)

    async def close(_p: Any) -> None:
        return None

    monkeypatch.setattr("rwi.providers.registry.close_providers", close)
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()
    v1._reset_fronts_cache()


def _use(monkeypatch: pytest.MonkeyPatch, fake: _FakeOpenMeteo) -> None:
    monkeypatch.setattr(
        "rwi.providers.registry.available_providers", lambda _s: {"open-meteo": fake}
    )


def test_endpoint_defaults_to_sa_stops_and_is_stable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeOpenMeteo()
    _use(monkeypatch, fake)
    r = client.get("/api/v1/fronts", params={"hours": 36})
    assert r.status_code == 200, r.text
    assert r.headers["X-Rwi-Product-Tier"] == "stable"
    body = r.json()
    assert [s["name"] for s in body["stops"]] == ["Coober Pedy", "Glendambo", "Port Augusta"]
    assert body["alerts_configured"] is False and body["alerts"] == []
    assert body["caveat"]
    assert set(fake.calls) == {("ensemble", "ecmwf_ifs025"), ("forecast", "ecmwf_aifs025_single")}
    for s in body["stops"]:
        assert s["probability"] == 1.0 and s["p50_utc"] is not None
        assert s["deterministic"]["passage_time_utc"] is None  # the AIFS stand-in is a ridge
    # Cached: a second call within 10 min does not fetch again.
    n = len(fake.calls)
    assert client.get("/api/v1/fronts", params={"hours": 36}).status_code == 200
    assert len(fake.calls) == n


def test_endpoint_alerts_when_configured_and_records_failures(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RWI_FRONT_ALERT_WATCH_PROBABILITY", "0.3")
    monkeypatch.setenv("RWI_FRONT_ALERT_WARNING_PROBABILITY", "0.7")
    get_settings.cache_clear()
    from rwi.api.routers.meteorologist import _synoptic_points
    from rwi.route.bwsc import load_bwsc_route

    points = {n: p for n, _, p in _synoptic_points(load_bwsc_route())}
    _use(monkeypatch, _FakeOpenMeteo(fail_ensemble_at=points["Glendambo"].latitude_deg))
    r = client.get("/api/v1/fronts", params={"stops": "Glendambo,Port Augusta", "hours": 36})
    assert r.status_code == 200, r.text
    body = r.json()
    gl, pa = body["stops"]
    assert gl["error"].startswith("TimeoutError") and gl["probability"] is None
    assert pa["error"] is None and pa["probability"] == 1.0
    assert body["alerts_configured"] is True
    assert [(a["id"], a["severity"]) for a in body["alerts"]] == [("front-port-augusta", "warning")]


def test_unknown_stop_is_422(client: TestClient) -> None:
    r = client.get("/api/v1/fronts", params={"stops": "Glendambo,Narnia"})
    assert r.status_code == 422 and "Narnia" in r.text
