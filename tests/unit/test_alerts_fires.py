"""Fire hotspots near the BWSC route and the smoke/fire WATCH rule (ADR 0014)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from rwi.alerts.fires import fire_alerts, fires_near_route, route_bbox_margin_deg
from rwi.contracts import AlertCategory, AlertSeverity
from rwi.providers.firms import parse_firms_csv
from rwi.route.bwsc import load_bwsc_route

FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "firms" / "j1_viirs_c2_aus_nz_24h_route_trimmed.csv"
)
NOW = datetime(2026, 11, 27, 6, tzinfo=UTC)


@pytest.fixture(scope="module")
def fires():  # type: ignore[no-untyped-def]
    route = load_bwsc_route()
    hotspots, _ = parse_firms_csv(FIXTURE.read_text())
    return fires_near_route(route, hotspots, buffer_km=50.0)


def test_buffer_keeps_hotspots_ordered_along_route(fires) -> None:  # type: ignore[no-untyped-def]
    assert len(fires) == 48
    along = [f.distance_along_m for f in fires]
    assert along == sorted(along)
    assert all(f.offset_m <= 50_000.0 for f in fires)


def test_clusters_match_recorded_fixture(fires) -> None:  # type: ignore[no-untyped-def]
    alerts = fire_alerts(fires, radius_km=20.0, min_confidence="nominal", min_frp_mw=0.0, now=NOW)
    starts = [a.id.rsplit("-", 1)[1] for a in alerts]
    assert starts == ["349", "512", "804", "2979"]
    counts = [int(a.evidence[0].split(":")[1].split("(")[0]) for a in alerts]
    assert counts == [1, 3, 9, 1]
    for a in alerts:
        assert a.severity is AlertSeverity.WATCH
        assert a.category is AlertCategory.VISIBILITY
        assert "humans decide." in a.recommended_action
        assert "not fire perimeters or road closures" in a.detail


def test_unconfigured_gives_no_alerts_and_bad_thresholds_raise(fires) -> None:  # type: ignore[no-untyped-def]
    assert (
        fire_alerts(fires, radius_km=None, min_confidence="nominal", min_frp_mw=0.0, now=NOW) == []
    )
    with pytest.raises(ValueError, match="radius"):
        fire_alerts(fires, radius_km=0.0, min_confidence="nominal", min_frp_mw=0.0, now=NOW)
    with pytest.raises(ValueError, match="confidence"):
        fire_alerts(fires, radius_km=20.0, min_confidence="medium", min_frp_mw=0.0, now=NOW)
    with pytest.raises(ValueError, match="frp"):
        fire_alerts(fires, radius_km=20.0, min_confidence="low", min_frp_mw=-1.0, now=NOW)


def test_hotspot_without_frp_never_qualifies(fires) -> None:  # type: ignore[no-untyped-def]
    stripped = [
        f.model_copy(update={"hotspot": f.hotspot.model_copy(update={"frp_mw": None})})
        for f in fires
    ]
    assert (
        fire_alerts(stripped, radius_km=20.0, min_confidence="low", min_frp_mw=0.0, now=NOW) == []
    )


def test_bbox_margin_widens_with_latitude() -> None:
    route = load_bwsc_route()
    m = route_bbox_margin_deg(route, 50.0)
    assert 50.0 / 111.32 < m < 1.0  # cos(~35 S) widens the longitude margin
    with pytest.raises(ValueError):
        route_bbox_margin_deg(route, -1.0)
