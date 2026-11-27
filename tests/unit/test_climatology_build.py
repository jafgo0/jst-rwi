"""Building the race-week climatology from a respx-mocked ERA5 archive (ADR 0016)."""

import gzip
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from rwi.climatology.build import (
    IncompleteClimatologyError,
    build_race_week_climatology,
    climatology_path,
    part_path,
)
from rwi.climatology.raceweek import RaceWeekClimatology, SamplePoint
from rwi.config import Settings
from rwi.contracts import WeatherSeries
from rwi.core.errors import ProviderUnavailableError
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind
from rwi.providers.openmeteo import OpenMeteoProvider
from rwi.route.bwsc import load_bwsc_route

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
UNITS = {
    "time": "iso8601",
    "cloud_cover": "%",
    "shortwave_radiation": "W/m²",
    "wind_speed_10m": "km/h",
    "wind_direction_10m": "°",
}


async def _no_sleep(_: float) -> None:
    return None


def _provider(tmp_path: Path, **kw: Any) -> OpenMeteoProvider:
    base: dict[str, Any] = {
        "cache_dir": tmp_path / "cache",
        "http_retries": 0,
        "http_backoff_s": 0.01,
        "allow_network": True,
    }
    st = Settings(_env_file=None, **base)
    return OpenMeteoProvider(
        st, clock=lambda: NOW, sleep=_no_sleep, forecast_model="ecmwf_ifs025", **kw
    )


def _archive(request: httpx.Request) -> httpx.Response:
    p = request.url.params
    first, last = date.fromisoformat(p["start_date"]), date.fromisoformat(p["end_date"])
    t0 = datetime.combine(first, datetime.min.time())
    n = ((last - first).days + 1) * 24
    times = [(t0 + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
    values = {
        "cloud_cover": 10.0,
        "shortwave_radiation": 500.0,
        "wind_speed_10m": 18.0,  # 5 m/s
        "wind_direction_10m": 135.0,
    }
    return httpx.Response(
        200,
        json={
            "latitude": float(p["latitude"]),
            "longitude": float(p["longitude"]),
            "utc_offset_seconds": 0,
            "hourly_units": UNITS,
            "hourly": {"time": times, **{k: [v] * n for k, v in values.items()}},
        },
    )


@respx.mock
async def test_builds_every_segment_from_one_call_per_point_year(tmp_path: Path) -> None:
    calls = respx.get(ARCHIVE_URL).mock(side_effect=_archive)
    route = load_bwsc_route()
    async with _provider(tmp_path) as provider:
        clim = await build_race_week_climatology(
            provider,
            route,
            first_year=2023,
            last_year=2024,
            spacing_km=1000.0,
            delay_s=0.0,
            parts_dir=tmp_path / "parts",
            now=NOW,
        )
    # spacing larger than any segment: one point per segment, 10 segments x 2 years.
    assert calls.call_count == 10 * 2
    params = [c.request.url.params for c in calls.calls]
    assert {q["models"] for q in params} == {"era5"}
    assert {q["start_date"] for q in params} == {"2023-08-21", "2024-08-21"}
    assert clim.route_id == route.id and clim.kind is DataKind.CLIMATOLOGY
    assert clim.years == [2023, 2024] and len(clim.segments) == 10
    assert clim.driving_window_event == "08:00-17:00 (ACST)"
    seg = clim.segments[0]
    assert seg.ghi is not None and seg.ghi.p50 == 500.0 and seg.ghi.n_years == 2
    assert seg.wind_speed_10m is not None and seg.wind_speed_10m.p50 == pytest.approx(5.0)
    assert seg.ghi.provenance.spatial_resolution_km == 25.0
    # JSON round trip is what the API serves.
    path = climatology_path(tmp_path, route.id)
    path.write_text(clim.model_dump_json(), encoding="utf-8")
    assert type(clim).model_validate_json(path.read_text(encoding="utf-8")) == clim


@respx.mock
async def test_persistent_outage_stops_early_and_writes_no_climatology(tmp_path: Path) -> None:
    calls = respx.get(ARCHIVE_URL).respond(503)
    async with _provider(tmp_path) as provider:
        with pytest.raises(IncompleteClimatologyError) as info:
            await _build(provider, tmp_path, max_consecutive_failures=2)
    assert isinstance(info.value, ProviderUnavailableError)
    assert info.value.aborted and len(info.value.failures) == 2
    assert calls.call_count == 2 * 3  # two point-years, three attempts each
    assert not (tmp_path / "parts").exists()


async def _build(provider: OpenMeteoProvider, tmp_path: Path, **kw: Any) -> RaceWeekClimatology:
    args: dict[str, Any] = {
        "first_year": 2023,
        "last_year": 2024,
        "spacing_km": 1000.0,
        "delay_s": 0.0,
        "parts_dir": tmp_path / "parts",
        "attempts": 3,
        "backoff_s": 10.0,
        "now": NOW,
        "sleep": _no_sleep,
        **kw,
    }
    return await build_race_week_climatology(provider, load_bwsc_route(), **args)


def _forget_responses(tmp_path: Path) -> None:
    """Drop the provider's HTTP cache so a rerun can only be served by the parts."""
    shutil.rmtree(tmp_path / "cache", ignore_errors=True)


def _parts(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "parts").rglob("*.json.gz"))


@respx.mock
async def test_rerun_reuses_every_part_and_makes_no_calls(tmp_path: Path) -> None:
    calls = respx.get(ARCHIVE_URL).mock(side_effect=_archive)
    async with _provider(tmp_path) as provider:
        first = await _build(provider, tmp_path)
    assert calls.call_count == 20 and len(_parts(tmp_path)) == 20
    calls.reset()
    _forget_responses(tmp_path)
    async with _provider(tmp_path) as provider:
        second = await _build(provider, tmp_path)
    assert calls.call_count == 0
    assert second.segments == first.segments


@respx.mock
async def test_transient_error_is_retried_with_exponential_backoff(tmp_path: Path) -> None:
    replies = iter([httpx.Response(503), httpx.Response(503)])

    def flaky(request: httpx.Request) -> httpx.Response:
        return next(replies, None) or _archive(request)

    calls = respx.get(ARCHIVE_URL).mock(side_effect=flaky)
    waits: list[float] = []

    async def record(s: float) -> None:
        waits.append(s)

    async with _provider(tmp_path) as provider:
        clim = await _build(provider, tmp_path, last_year=2023, sleep=record)
    assert calls.call_count == 10 + 2 and len(clim.segments) == 10
    assert waits == [10.0, 20.0]


@respx.mock
async def test_one_failure_does_not_stop_the_rest(tmp_path: Path) -> None:
    # The first point-year fails all three attempts; every later call succeeds.
    state = {"n": 0}

    def fail_first_point_year(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(503) if state["n"] <= 3 else _archive(request)

    calls = respx.get(ARCHIVE_URL).mock(side_effect=fail_first_point_year)
    async with _provider(tmp_path) as provider:
        with pytest.raises(IncompleteClimatologyError) as info:
            await _build(provider, tmp_path)
    assert not info.value.aborted and info.value.total == 20
    (failure,) = info.value.failures
    assert failure.startswith("Darwin-Katherine") and "2023" in failure
    assert calls.call_count == 3 + 19 and len(_parts(tmp_path)) == 19
    assert not climatology_path(tmp_path, load_bwsc_route().id).exists()

    calls.reset()
    _forget_responses(tmp_path)
    async with _provider(tmp_path) as provider:
        clim = await _build(provider, tmp_path)
    assert calls.call_count == 1 and len(clim.segments) == 10


@respx.mock
async def test_corrupt_or_foreign_part_is_fetched_again(tmp_path: Path) -> None:
    calls = respx.get(ARCHIVE_URL).mock(side_effect=_archive)
    async with _provider(tmp_path) as provider:
        await _build(provider, tmp_path, last_year=2023)
    corrupt, foreign, *_ = _parts(tmp_path)
    corrupt.write_bytes(b"not gzip")
    other = WeatherSeries.model_validate_json(gzip.decompress(foreign.read_bytes()))
    foreign.write_bytes(
        gzip.compress(other.model_copy(update={"model_name": "gfs"}).model_dump_json().encode())
    )
    calls.reset()
    _forget_responses(tmp_path)
    async with _provider(tmp_path) as provider:
        await _build(provider, tmp_path, last_year=2023)
    assert calls.call_count == 2
    reread = WeatherSeries.model_validate_json(gzip.decompress(foreign.read_bytes()))
    assert reread.model_name == "era5"


def test_part_path_is_keyed_by_route_year_and_coordinates(tmp_path: Path) -> None:
    sp = SamplePoint(
        distance_km=12.5, point=GeoPoint(latitude_deg=-12.46, longitude_deg=130.84), heading_deg=0
    )
    assert part_path(tmp_path, "r", 2024, sp) == (
        tmp_path / "r" / "era5" / "2024" / "-12.4600_+130.8400.json.gz"
    )


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"first_year": 2025, "last_year": 2024}, "after last_year"),
        ({"first_year": 2026, "last_year": 2027}, "not over yet"),
        ({"first_year": 2024, "last_year": 2024, "delay_s": -1.0}, "delay_s"),
        ({"first_year": 2024, "last_year": 2024, "attempts": 0}, "attempts"),
        ({"first_year": 2024, "last_year": 2024, "backoff_s": -1.0}, "backoff_s"),
        ({"first_year": 2024, "last_year": 2024, "max_consecutive_failures": 0}, "consecutive"),
    ],
)
async def test_rejects_bad_arguments(tmp_path: Path, kw: dict[str, Any], match: str) -> None:
    args: dict[str, Any] = {
        "spacing_km": 1000.0,
        "delay_s": 0.0,
        "parts_dir": tmp_path / "parts",
        "now": NOW,
        **kw,
    }
    with respx.mock(assert_all_called=False) as mock:
        any_call = mock.route().respond(500)
        async with _provider(tmp_path) as provider:
            with pytest.raises(ValueError, match=match):
                await build_race_week_climatology(provider, load_bwsc_route(), **args)
        assert not any_call.called


async def test_rejects_instantaneous_radiation(tmp_path: Path) -> None:
    async with _provider(tmp_path, radiation_mode="instant") as provider:
        with pytest.raises(ValueError, match="preceding-hour mean"):
            await build_race_week_climatology(
                provider,
                load_bwsc_route(),
                first_year=2024,
                last_year=2024,
                spacing_km=1000.0,
                delay_s=0.0,
                parts_dir=tmp_path / "parts",
                now=NOW,
            )
