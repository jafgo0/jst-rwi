"""GET /api/climatology/race-week serves only the built file (ADR 0016)."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient

from rwi.api.app import create_app
from rwi.climatology.build import climatology_path
from rwi.climatology.raceweek import (
    RaceWeekClimatology,
    SamplePoint,
    SegmentClimatology,
    control_stop_segments,
)
from rwi.config import get_settings
from rwi.core.geo import GeoPoint
from rwi.route.bwsc import OFFICIAL_2025_TOTAL_KM, load_bwsc_route

ROUTE = "bwsc-2027-stuart-highway"
URL = "/api/climatology/race-week"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("RWI_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("RWI_CLIMATOLOGY_DIR", str(tmp_path))
    monkeypatch.setenv("RWI_HTTP_RETRIES", "0")
    get_settings.cache_clear()
    with respx.mock(assert_all_called=False) as mock:
        network = mock.route().respond(500)
        with TestClient(create_app()) as c:
            yield c
        assert not network.called  # serving never fetches
    get_settings.cache_clear()


def _clim(route_id: str = ROUTE) -> RaceWeekClimatology:
    total = load_bwsc_route().total_distance_m / 1000.0
    point = GeoPoint(latitude_deg=-23.7, longitude_deg=133.875)
    segments = [
        SegmentClimatology(
            segment=s,
            points=[
                SamplePoint(distance_km=(s.start_km + s.end_km) / 2, point=point, heading_deg=0)
            ],
            cloud_cover=None,
            ghi=None,
            wind_speed_10m=None,
            tailwind_m_s=None,
            daily_ghi_energy=None,
            wind_rose=None,
        )
        for s in control_stop_segments(total)
    ]
    return RaceWeekClimatology(
        route_id=route_id,
        provider="open-meteo",
        dataset="archive",
        model_name="era5",
        years=[2024],
        race_week_event_dates="Aug 22-29 (ACST)",
        driving_window_event="08:00-17:00 (ACST)",
        spacing_km=50.0,
        built_at_utc=datetime(2026, 11, 1, tzinfo=UTC),
        notes=[],
        segments=segments,
    )


def _write(tmp_path: Path, text: str) -> None:
    climatology_path(tmp_path, ROUTE).write_text(text, encoding="utf-8")


def test_serves_every_segment(client: TestClient, tmp_path: Path) -> None:
    _write(tmp_path, _clim().model_dump_json())
    r = client.get(URL, params={"route_id": ROUTE})
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "climatology" and len(body["segments"]) == 10


@pytest.mark.parametrize(
    ("km", "start_name"),
    [(0.0, "Darwin"), (321.9, "Darwin"), (322.0, "Katherine"), (1500.0, "Alice Springs")],
)
def test_distance_picks_one_segment(
    client: TestClient, tmp_path: Path, km: float, start_name: str
) -> None:
    _write(tmp_path, _clim().model_dump_json())
    r = client.get(URL, params={"route_id": ROUTE, "distance_km": km})
    assert r.status_code == 200
    (seg,) = r.json()["segments"]
    assert seg["segment"]["start_name"] == start_name


def test_route_end_belongs_to_last_segment(client: TestClient, tmp_path: Path) -> None:
    clim = _clim()
    _write(tmp_path, clim.model_dump_json())
    end = clim.segments[-1].segment.end_km
    r = client.get(URL, params={"route_id": ROUTE, "distance_km": end})
    assert r.status_code == 200
    assert r.json()["segments"][0]["segment"]["end_name"] == "Adelaide"


def test_not_built_is_404(client: TestClient) -> None:
    r = client.get(URL, params={"route_id": ROUTE})
    assert r.status_code == 404 and "build_climatology" in r.json()["detail"]


def test_unknown_route_is_404(client: TestClient) -> None:
    assert client.get(URL, params={"route_id": "no-such-route"}).status_code == 404


@pytest.mark.parametrize("km", [-1.0, OFFICIAL_2025_TOTAL_KM + 50.0])
def test_distance_outside_route_is_422(client: TestClient, tmp_path: Path, km: float) -> None:
    _write(tmp_path, _clim().model_dump_json())
    assert client.get(URL, params={"route_id": ROUTE, "distance_km": km}).status_code == 422


def test_invalid_file_is_503(client: TestClient, tmp_path: Path) -> None:
    _write(tmp_path, '{"route_id": "x"}')
    r = client.get(URL, params={"route_id": ROUTE})
    assert r.status_code == 503 and "rebuild" in r.json()["detail"]


def test_file_for_other_route_is_503(client: TestClient, tmp_path: Path) -> None:
    _write(tmp_path, _clim(route_id="other-route").model_dump_json())
    assert client.get(URL, params={"route_id": ROUTE}).status_code == 503
