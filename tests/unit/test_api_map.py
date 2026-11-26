"""/api/map/grid: AIFS from the GRIB cache, IFS via Open-Meteo, honest when unavailable."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pytest
import respx
from fastapi.testclient import TestClient

from rwi.api.app import create_app
from rwi.api.routers import map as map_router
from rwi.config import get_settings
from rwi.core.provenance import DataKind
from rwi.providers.gridded import BBox, GriddedRun

INIT = datetime(2027, 8, 22, 0, tzinfo=UTC)
# 06Z = 15:30 ACST (Sun up), 12Z = 21:30 ACST (Sun down the whole preceding hour)
TIMES = (INIT + timedelta(hours=6), INIT + timedelta(hours=12))


def _run() -> GriddedRun:
    lats = np.array([-13.0, -12.0, -11.0])
    lons = np.array([130.0, 131.0, 132.0])
    shape = (len(TIMES), lats.size, lons.size)
    ssrd = np.stack([np.full(shape[1:], 1.2e7), np.full(shape[1:], 1.9e7)])
    return GriddedRun(
        provider="ecmwf",
        dataset="aifs-single-open-data",
        kind=DataKind.FORECAST,
        model_name="aifs",
        model_version="aifs-single-0p25",
        init_time_utc=INIT,
        retrieved_at_utc=INIT + timedelta(hours=7),
        valid_times_utc=TIMES,
        latitudes=lats,
        longitudes=lons,
        fields={
            "cloud_cover": np.full(shape, 40.0),
            "temperature_2m": np.full(shape, 30.0),
            "wind_u_10m": np.full(shape, -5.0),
            "wind_v_10m": np.zeros(shape),
            "ssrd_accum": ssrd,
        },
        spatial_resolution_km=28.0,
        license="CC-BY-4.0",
        source_url="https://data.ecmwf.int/forecasts/",
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("RWI_CACHE_DIR", str(tmp_path))
    get_settings.cache_clear()
    map_router._aifs_cache.clear()
    map_router._ifs_cache.clear()
    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def test_grid_axes_snap_to_step() -> None:
    lats, lons = map_router.grid_axes(BBox(-13.4, -11.0, 130.2, 132.9), 1.0)
    assert lats == [-13.0, -12.0, -11.0]
    assert lons == [131.0, 132.0]


@pytest.mark.science
def test_aifs_grid_night_ghi_exactly_zero_and_units() -> None:
    # Sun geometry from pvlib SPA (no mock): at 12Z (21:30 ACST) near Darwin the Sun
    # has been below the horizon for the whole preceding hour -> GHI must be 0.0.
    grid = map_router.aifs_grid(_run(), [-12.0], [131.0], 1.0)
    assert grid.available and grid.valid_times_utc == list(TIMES)
    ghi = [row[0][0] for row in grid.fields["ghi"]]
    assert ghi[1] == 0.0
    assert ghi[0] is not None and ghi[0] > 0.0
    assert grid.fields["wind_speed_kmh"][0][0][0] == 18.0  # 5 m/s
    assert grid.fields["wind_direction_deg"][0][0][0] == 90.0  # from the east
    assert grid.fields["cloud_cover"][0][0][0] == 40.0
    assert grid.provenance is not None
    assert grid.provenance.kind is DataKind.FORECAST
    assert grid.provenance.init_time_utc == INIT


def test_aifs_grid_nodes_outside_the_run_are_null() -> None:
    grid = map_router.aifs_grid(_run(), [-12.0, -20.0], [131.0], 1.0)
    assert grid.fields["temperature_c"][0][1][0] is None
    assert grid.fields["temperature_c"][0][0][0] == 30.0


def test_aifs_unavailable_without_grib(client: TestClient) -> None:
    body = client.get("/api/map/grid", params={"model": "aifs"}).json()
    assert body["available"] is False
    assert "capture" in body["message"]
    assert body["fields"] == {}


def test_aifs_reads_newest_cached_grib(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "aifs-capture").mkdir()
    (tmp_path / "aifs-capture" / "aifs_20270822T00Z_abcd1234.grib2").write_bytes(b"x")
    seen: list[str] = []

    def fake_decode(path: Path, bbox: BBox) -> GriddedRun:
        seen.append(path.name)
        return _run()

    monkeypatch.setattr(map_router, "_decode_aifs", fake_decode)
    body = client.get("/api/map/grid", params={"model": "aifs"}).json()
    assert body["available"] is True
    assert body["provenance"]["model_name"] == "aifs"
    assert seen == ["aifs_20270822T00Z_abcd1234.grib2"]
    client.get("/api/map/grid", params={"model": "aifs"})
    assert len(seen) == 1  # cached by file name + mtime


@pytest.mark.science
def test_ifs_multi_location_and_night_ghi_forced_to_zero(client: TestClient) -> None:
    # 15:00Z = 00:30 ACST: Sun down all hour (pvlib SPA) -> the provider's small value is zeroed.
    hours = ["2027-08-22T15:00", "2027-08-23T03:00"]

    def reply(request: httpx.Request) -> httpx.Response:
        n = len(request.url.params["latitude"].split(","))
        assert n == len(request.url.params["longitude"].split(","))
        assert request.url.params["models"] == "ecmwf_ifs025"
        loc = {
            "hourly": {
                "time": hours,
                "shortwave_radiation": [3.0, 850.0],
                "cloud_cover": [10, None],
                "wind_speed_10m": [12.0, 20.0],
                "wind_direction_10m": [120, 135],
                "temperature_2m": [22.0, 31.0],
            }
        }
        return httpx.Response(200, json=[loc] * n)

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*/forecast.*").mock(side_effect=reply)
        mock.get(url__regex=r".*/meta\.json").mock(return_value=httpx.Response(404))
        body = client.get("/api/map/grid", params={"model": "ifs"}).json()
    assert body["available"] is True, body.get("message")
    ghi = body["fields"]["ghi"]
    assert all(v == 0.0 for row in ghi[0] for v in row)
    assert any(v == 850.0 for row in ghi[1] for v in row)
    assert body["fields"]["cloud_cover"][1][0][0] is None
    assert body["provenance"]["init_time_utc"] is None  # meta unavailable -> unknown, not guessed
    assert body["provenance"]["kind"] == "forecast"


def test_ifs_provider_error_is_reported(client: TestClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*/forecast.*").mock(return_value=httpx.Response(503))
        body = client.get("/api/map/grid", params={"model": "ifs"}).json()
    assert body["available"] is False
    assert "open-meteo" in body["message"]
