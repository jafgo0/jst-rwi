"""BaseProvider: retry/backoff, typed errors, disk cache, offline mode. No network."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from rwi.config import Settings
from rwi.contracts import ProviderCapabilities
from rwi.core.errors import (
    CapabilityNotSupportedError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    UnknownVariableError,
)
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, QualityFlag
from rwi.core.timebase import TimeWindow
from rwi.providers.base import (
    BaseProvider,
    ResponseCache,
    ceil_hour,
    classify_value,
    floor_hour,
    make_cache_key,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
URL = "https://example.test/api"
T0 = datetime(2026, 11, 1, 22, 30, tzinfo=UTC)
POINT = GeoPoint(latitude_deg=-12.4634, longitude_deg=130.8456, elevation_m=30.0)
WINDOW = TimeWindow(start_utc=T0, end_utc=T0 + timedelta(hours=3))


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_settings(tmp_path: Path, **kw: Any) -> Settings:
    base: dict[str, Any] = {
        "cache_dir": tmp_path / "cache",
        "http_retries": 2,
        "http_backoff_s": 0.01,
        "http_timeout_s": 1.0,
        "allow_network": True,
        "cache_ttl_forecast_s": 900.0,
        "cache_ttl_observation_s": 300.0,
    }
    base.update(kw)
    return Settings(_env_file=None, **base)


class Dummy(BaseProvider):
    """Minimal concrete provider: one cached JSON call."""

    name = "dummy"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider="dummy",
            datasets=("d",),
            supports_observations=False,
            supports_forecast=True,
            supports_ensemble=False,
            supports_archive=False,
            variables=frozenset({"temperature_2m"}),
            max_forecast_hours=10.0,
            spatial_resolution_km=None,
            update_interval_s=None,
            license="test",
            attribution="test",
            documentation_url="https://example.test",
        )

    async def get(self, window: TimeWindow = WINDOW, point: GeoPoint = POINT) -> Any:
        key = make_cache_key(
            provider=self.name,
            dataset="d",
            point=point,
            window=window,
            variables=["temperature_2m"],
            extra={"models": "x"},
        )

        async def fetch() -> Any:
            return await self._get_json(URL, {"a": 1})

        return await self._cached(key, self._ttl_s(DataKind.FORECAST), fetch)

    async def _probe(self) -> None:
        await self._get_json(URL, {"a": 1})


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


def provider(tmp_path: Path, clock: Clock, sleeps: Sleeps, **kw: Any) -> Dummy:
    settings = kw.pop("settings", None) or make_settings(tmp_path)
    return Dummy(settings, clock=clock, sleep=sleeps, **kw)


# --------------------------------------------------------------------------- retry


@respx.mock
async def test_retry_on_429_then_success_honours_retry_after(tmp_path, clock, sleeps):
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(
                429, headers={"Retry-After": "2"}, json={"error": True, "reason": "slow"}
            ),
            httpx.Response(200, json={"ok": 1}),
        ]
    )
    result = await provider(tmp_path, clock, sleeps).get()
    assert result.payload == {"ok": 1}
    assert route.call_count == 2
    assert sleeps.calls == [2.0]  # Retry-After (2 s) beats the 0.01 s backoff


@respx.mock
async def test_exponential_backoff_and_unavailable_after_exhausting_retries(
    tmp_path, clock, sleeps
):
    route = respx.get(URL).respond(503, json={"error": True, "reason": "down"})
    with pytest.raises(ProviderUnavailableError, match="HTTP 503"):
        await provider(tmp_path, clock, sleeps).get()
    assert route.call_count == 3  # 1 + http_retries(2)
    assert sleeps.calls == pytest.approx([0.01, 0.02])  # backoff * 2**attempt


@respx.mock
async def test_timeout_is_retried_then_unavailable(tmp_path, clock, sleeps):
    route = respx.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderUnavailableError, match="timeout"):
        await provider(tmp_path, clock, sleeps).get()
    assert route.call_count == 3


@respx.mock
async def test_transport_error_is_retried(tmp_path, clock, sleeps):
    route = respx.get(URL).mock(
        side_effect=[httpx.ConnectError("refused"), httpx.Response(200, json={"ok": 2})]
    )
    assert (await provider(tmp_path, clock, sleeps).get()).payload == {"ok": 2}
    assert route.call_count == 2


@respx.mock
async def test_persistent_429_raises_rate_limit_error(tmp_path, clock, sleeps):
    route = respx.get(URL).respond(429, json={"error": True, "reason": "slow down"})
    with pytest.raises(ProviderRateLimitError):
        await provider(tmp_path, clock, sleeps).get()
    assert route.call_count == 3


@respx.mock
async def test_real_daily_quota_429_fails_fast_without_sleeping(tmp_path, clock, sleeps):
    """Captured 2026-11-01 from ensemble-api.open-meteo.com: Retry-After 5693 s."""
    fx = json.loads((FIXTURES / "openmeteo_429_daily_limit.json").read_text())
    route = respx.get(URL).respond(fx["status_code"], headers=fx["headers"], json=fx["body"])
    with pytest.raises(ProviderRateLimitError, match="Daily API request limit exceeded"):
        await provider(tmp_path, clock, sleeps).get()
    assert route.call_count == 1
    assert sleeps.calls == []


@respx.mock
async def test_non_rate_limit_4xx_is_not_retried(tmp_path, clock, sleeps):
    route = respx.get(URL).respond(400, json={"error": True, "reason": "bad parameter"})
    with pytest.raises(ProviderUnavailableError, match="bad parameter"):
        await provider(tmp_path, clock, sleeps).get()
    assert route.call_count == 1 and sleeps.calls == []


@respx.mock
async def test_rate_limit_text_in_a_400_body_is_classified_as_rate_limit(tmp_path, clock, sleeps):
    respx.get(URL).respond(400, json={"error": True, "reason": "Hourly API request limit exceeded"})
    with pytest.raises(ProviderRateLimitError):
        await provider(tmp_path, clock, sleeps).get()


@respx.mock
async def test_non_json_200_is_unavailable(tmp_path, clock, sleeps):
    respx.get(URL).respond(200, text="<html>captive portal</html>")
    with pytest.raises(ProviderUnavailableError, match="not JSON"):
        await provider(tmp_path, clock, sleeps).get()


@respx.mock
async def test_error_messages_never_contain_query_string(tmp_path, clock, sleeps):
    respx.get(URL).respond(500)
    with pytest.raises(ProviderUnavailableError) as exc:
        await provider(tmp_path, clock, sleeps).get()
    assert "a=1" not in str(exc.value)


# --------------------------------------------------------------------------- cache


@respx.mock
async def test_cache_hit_keeps_original_retrieved_at(tmp_path, clock, sleeps):
    route = respx.get(URL).respond(200, json={"v": 1})
    p = provider(tmp_path, clock, sleeps)
    first = await p.get()
    assert not first.from_cache and first.retrieved_at_utc == T0
    clock.advance(600)  # inside the 900 s forecast TTL
    second = await p.get()
    assert second.from_cache
    assert second.retrieved_at_utc == T0  # NOT refreshed: age stays honest
    assert second.age_s == pytest.approx(600.0)
    assert route.call_count == 1


@respx.mock
async def test_cache_expires_after_ttl_and_refetches(tmp_path, clock, sleeps):
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(200, json={"v": 1}), httpx.Response(200, json={"v": 2})]
    )
    p = provider(tmp_path, clock, sleeps)
    await p.get()
    clock.advance(901)
    again = await p.get()
    assert again.payload == {"v": 2} and again.retrieved_at_utc == T0 + timedelta(seconds=901)
    assert route.call_count == 2


@respx.mock
async def test_cache_persists_on_disk_across_provider_instances(tmp_path, clock, sleeps):
    route = respx.get(URL).respond(200, json={"v": 1})
    await provider(tmp_path, clock, sleeps).get()
    clock.advance(10)
    again = await provider(tmp_path, clock, sleeps).get()  # new instance, same cache_dir
    assert again.from_cache and again.retrieved_at_utc == T0 and route.call_count == 1


@respx.mock
async def test_cache_key_rounds_the_point_but_separates_variables_and_windows(tmp_path):
    w = WINDOW
    k1 = make_cache_key(provider="p", dataset="d", point=POINT, window=w, variables=["b", "a"])
    near = GeoPoint(latitude_deg=-12.4631, longitude_deg=130.8458)
    k2 = make_cache_key(provider="p", dataset="d", point=near, window=w, variables=["a", "b"])
    assert k1 == k2  # same ~1 km cell, variable order irrelevant
    far = GeoPoint(latitude_deg=-12.60, longitude_deg=130.8456)
    assert (
        make_cache_key(provider="p", dataset="d", point=far, window=w, variables=["a", "b"]) != k1
    )
    other_w = TimeWindow(start_utc=T0, end_utc=T0 + timedelta(hours=4))
    assert (
        make_cache_key(provider="p", dataset="d", point=POINT, window=other_w, variables=["a", "b"])
        != k1
    )
    assert make_cache_key(provider="p", dataset="d", point=POINT, window=w, variables=["a"]) != k1
    assert (
        make_cache_key(
            provider="p",
            dataset="d",
            point=POINT,
            window=w,
            variables=["a", "b"],
            extra={"models": "x"},
        )
        != k1
    )
    cache = ResponseCache(tmp_path)
    assert cache.path_for(k1) == cache.path_for(k2)
    assert cache.path_for(k1) != cache.path_for({**k2, "dataset": "e"})


async def test_corrupt_cache_file_is_ignored_not_fatal(tmp_path):
    cache = ResponseCache(tmp_path)
    key = make_cache_key(provider="p", dataset="d", point=POINT, window=WINDOW, variables=["a"])
    path = cache.path_for(key)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert cache.load(key) is None


# --------------------------------------------------------------------------- offline


@respx.mock
async def test_offline_without_cache_raises_and_makes_no_request(tmp_path, clock, sleeps):
    route = respx.get(URL).respond(200, json={"v": 1})
    p = provider(tmp_path, clock, sleeps, settings=make_settings(tmp_path, allow_network=False))
    with pytest.raises(ProviderUnavailableError, match="no cached data"):
        await p.get()
    assert route.call_count == 0


@respx.mock
async def test_offline_serves_cache_past_ttl_with_original_age(tmp_path, clock, sleeps):
    respx.get(URL).respond(200, json={"v": 1})
    await provider(tmp_path, clock, sleeps).get()  # online, fills the cache
    respx.reset()
    route = respx.get(URL).respond(200, json={"v": 999})
    clock.advance(3 * 3600)  # far past TTL
    p = provider(tmp_path, clock, sleeps, settings=make_settings(tmp_path, allow_network=False))
    served = await p.get()
    assert served.payload == {"v": 1}  # never fabricated or refreshed
    assert served.from_cache and served.retrieved_at_utc == T0
    assert served.age_s == pytest.approx(3 * 3600)
    assert route.call_count == 0


@respx.mock
async def test_network_failure_does_not_silently_serve_stale_cache_by_default(
    tmp_path, clock, sleeps
):
    respx.get(URL).respond(200, json={"v": 1})
    await provider(tmp_path, clock, sleeps).get()
    clock.advance(2000)
    respx.reset()
    respx.get(URL).respond(500)
    with pytest.raises(ProviderUnavailableError):
        await provider(tmp_path, clock, sleeps).get()


@respx.mock
async def test_serve_stale_on_error_opt_in_keeps_original_retrieved_at(tmp_path, clock, sleeps):
    respx.get(URL).respond(200, json={"v": 1})
    await provider(tmp_path, clock, sleeps).get()
    clock.advance(2000)
    respx.reset()
    respx.get(URL).respond(500)
    served = await provider(tmp_path, clock, sleeps, serve_stale_on_error=True).get()
    assert served.from_cache and served.retrieved_at_utc == T0 and served.age_s == 2000


# --------------------------------------------------------------------------- capability + health


async def test_default_methods_raise_capability_not_supported(tmp_path, clock, sleeps):
    p = provider(tmp_path, clock, sleeps)
    with pytest.raises(CapabilityNotSupportedError):
        await p.fetch_observations(POINT, WINDOW, ["temperature_2m"])
    with pytest.raises(CapabilityNotSupportedError):
        await p.fetch_ensemble(POINT, WINDOW, ["temperature_2m"])
    with pytest.raises(CapabilityNotSupportedError):
        await p.fetch_analysis(POINT, WINDOW, ["temperature_2m"])


def test_check_variables(tmp_path, clock, sleeps):
    p = provider(tmp_path, clock, sleeps)
    assert p._check_variables(["temperature_2m", "temperature_2m"]) == ["temperature_2m"]
    with pytest.raises(UnknownVariableError):
        p._check_variables(["nonsense"])
    with pytest.raises(CapabilityNotSupportedError, match="wind_speed_10m"):
        p._check_variables(["wind_speed_10m"])  # canonical, but not offered by Dummy
    with pytest.raises(CapabilityNotSupportedError):
        p._check_variables([])


@respx.mock
async def test_health_ok_failed_and_offline(tmp_path, clock, sleeps):
    respx.get(URL).respond(200, json={"ok": 1})
    h = await provider(tmp_path, clock, sleeps).health()
    assert h.reachable and h.latency_ms is not None and h.provider == "dummy"
    respx.reset()
    respx.get(URL).respond(500)
    h = await provider(tmp_path, clock, sleeps).health()
    assert not h.reachable and "500" in (h.message or "")
    off = provider(tmp_path, clock, sleeps, settings=make_settings(tmp_path, allow_network=False))
    h = await off.health()
    assert not h.reachable and "disabled" in (h.message or "")


# --------------------------------------------------------------------------- helpers


def test_classify_value_range_and_missing():
    assert classify_value("temperature_2m", 25.0) == (25.0, QualityFlag.OK)
    assert classify_value("temperature_2m", 75.0) == (75.0, QualityFlag.OUT_OF_RANGE)
    assert classify_value("pressure_msl", 2.0e5)[1] is QualityFlag.OUT_OF_RANGE
    assert classify_value("ghi", None) == (None, QualityFlag.MISSING)
    assert classify_value("ghi", float("nan")) == (None, QualityFlag.MISSING)
    assert classify_value("ghi", 0.0) == (0.0, QualityFlag.OK)  # zero is a value, not missing


def test_floor_and_ceil_hour():
    t = datetime(2026, 11, 1, 22, 30, 5, tzinfo=UTC)
    assert floor_hour(t) == datetime(2026, 11, 1, 22, tzinfo=UTC)
    assert ceil_hour(t) == datetime(2026, 11, 1, 23, tzinfo=UTC)
    on = datetime(2026, 11, 1, 22, tzinfo=UTC)
    assert floor_hour(on) == ceil_hour(on) == on
