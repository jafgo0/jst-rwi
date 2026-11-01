"""available_providers() and fetch_with_fallback(): order, skipping, notes, honest failure."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx

from rwi.config import Settings
from rwi.contracts import (
    ProviderCapabilities,
    ProviderHealth,
    WeatherProvider,
    WeatherSeries,
    WeatherStep,
)
from rwi.core.errors import (
    CapabilityNotSupportedError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance, measured
from rwi.core.timebase import TimeWindow
from rwi.providers.registry import (
    available_providers,
    close_providers,
    fetch_with_fallback,
    fetch_with_fallback_detailed,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NOW = datetime(2026, 11, 1, 22, 30, tzinfo=UTC)
POINT = GeoPoint(latitude_deg=-12.4719, longitude_deg=130.8458)
WINDOW = TimeWindow(start_utc=NOW, end_utc=NOW + timedelta(hours=3))
VARS = ["temperature_2m", "wind_speed_10m"]


def caps(
    name: str,
    *,
    forecast: bool = True,
    obs: bool = False,
    archive: bool = False,
    variables: Sequence[str] = ("temperature_2m", "wind_speed_10m", "ghi"),
    max_h: float | None = 384.0,
) -> ProviderCapabilities:
    return ProviderCapabilities(
        provider=name,
        datasets=("d",),
        supports_observations=obs,
        supports_forecast=forecast,
        supports_ensemble=False,
        supports_archive=archive,
        variables=frozenset(variables),
        max_forecast_hours=max_h,
        spatial_resolution_km=None,
        update_interval_s=None,
        license="t",
        attribution="t",
        documentation_url="https://example.test",
    )


def series(provider: str, kind: DataKind, value: float | None = 20.0) -> WeatherSeries:
    t = NOW + timedelta(hours=1)
    kw: dict[str, Any] = (
        {"observation_time_utc": t} if kind is DataKind.OBSERVATION else {"valid_time_utc": t}
    )
    prov = Provenance(provider=provider, dataset="d", kind=kind, retrieved_at_utc=NOW, **kw)
    values = {v: measured(v, value, prov) for v in VARS}
    return WeatherSeries(
        point=POINT,
        requested_point=POINT,
        provider=provider,
        dataset="d",
        kind=kind,
        retrieved_at_utc=NOW,
        steps=[WeatherStep(valid_time_utc=t, values=values)],
    )


class Fake:
    """Scripted provider. `result` is a series, an exception to raise, or a callable."""

    def __init__(self, name: str, capabilities: ProviderCapabilities, result: Any, log: list[str]):
        self.name = name
        self._caps = capabilities
        self._result = result
        self._log = log

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._caps

    async def _go(self, method: str) -> WeatherSeries:
        self._log.append(f"{self.name}.{method}")
        r = self._result
        if isinstance(r, Exception):
            raise r
        return r

    async def fetch_forecast(self, point, window, variables):
        return await self._go("forecast")

    async def fetch_observations(self, point, window, variables):
        return await self._go("observations")

    async def fetch_analysis(self, point, window, variables):
        return await self._go("analysis")

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider=self.name, reachable=True, checked_at_utc=NOW)


async def run(providers, kind=DataKind.FORECAST, variables=VARS, window=WINDOW):
    return await fetch_with_fallback_detailed(
        providers, POINT, window, variables, kind=kind, now=NOW
    )


async def test_first_provider_in_priority_order_wins_and_others_are_not_called():
    log: list[str] = []
    a = Fake("a", caps("a"), series("a", DataKind.FORECAST), log)
    b = Fake("b", caps("b"), series("b", DataKind.FORECAST), log)
    s, attempts = await run({"a": a, "b": b})
    assert s.provider == "a" and log == ["a.forecast"]
    assert [x.outcome for x in attempts] == ["used"]
    # same result when given a plain sequence in the opposite order
    s2, _ = await run([b, a])
    assert s2.provider == "b"


async def test_failed_provider_is_skipped_and_named_in_a_note_on_the_result():
    log: list[str] = []
    a = Fake("a", caps("a"), ProviderUnavailableError("a: HTTP 503 from upstream"), log)
    b = Fake("b", caps("b"), ProviderRateLimitError("b: rate limited"), log)
    c = Fake("c", caps("c"), series("c", DataKind.FORECAST), log)
    s = await fetch_with_fallback([a, b, c], POINT, WINDOW, VARS, kind=DataKind.FORECAST)
    assert log == ["a.forecast", "b.forecast", "c.forecast"]
    assert s.provider == "c" and s.kind is DataKind.FORECAST
    note = s.steps[0].values["temperature_2m"].provenance.notes or ""
    assert "Fallback:" in note and "a: failed" in note and "HTTP 503" in note
    assert "b: failed" in note and "rate limited" in note
    assert "c: used" not in note  # only the failures are listed
    assert s.steps[0].values["wind_speed_10m"].provenance.notes == note
    assert s.steps[0].values["temperature_2m"].value == 20.0  # data untouched


async def test_no_note_when_first_provider_succeeds():
    a = Fake("a", caps("a"), series("a", DataKind.FORECAST), [])
    s = await fetch_with_fallback([a], POINT, WINDOW, VARS, kind=DataKind.FORECAST)
    assert s.steps[0].values["temperature_2m"].provenance.notes is None


async def test_providers_that_cannot_cover_the_request_are_skipped_without_a_call():
    log: list[str] = []
    no_obs = Fake(
        "model", caps("model", forecast=True, obs=False), series("model", DataKind.FORECAST), log
    )
    missing_var = Fake(
        "narrow",
        caps("narrow", variables=("temperature_2m",)),
        series("narrow", DataKind.FORECAST),
        log,
    )
    short = Fake("short", caps("short", max_h=1.0), series("short", DataKind.FORECAST), log)
    ok = Fake("ok", caps("ok"), series("ok", DataKind.FORECAST), log)
    far = TimeWindow(start_utc=NOW, end_utc=NOW + timedelta(days=3))
    s, attempts = await run([missing_var, short, ok], window=far)
    assert log == ["ok.forecast"] and s.provider == "ok"
    reasons = {a.provider: a.reason for a in attempts if a.outcome == "skipped"}
    assert "wind_speed_10m" in reasons["narrow"]
    assert "horizon" in reasons["short"]
    note = s.steps[0].values["temperature_2m"].provenance.notes or ""
    assert "narrow: skipped" in note and "short: skipped" in note
    # forecast-only provider is skipped for observations
    with pytest.raises(CapabilityNotSupportedError):
        await run([no_obs], kind=DataKind.OBSERVATION)
    assert "model.observations" not in log


async def test_observation_request_never_returns_a_forecast():
    log: list[str] = []
    # a (misbehaving) provider that claims observations but returns a forecast series
    liar = Fake("liar", caps("liar", obs=True), series("liar", DataKind.FORECAST), log)
    truth = Fake(
        "truth", caps("truth", forecast=False, obs=True), series("truth", DataKind.OBSERVATION), log
    )
    s, attempts = await run([liar, truth], kind=DataKind.OBSERVATION)
    assert s.provider == "truth" and s.kind is DataKind.OBSERVATION
    assert "returned kind forecast, requested observation" in (attempts[0].reason or "")
    with pytest.raises(ProviderUnavailableError, match="returned kind forecast"):
        await run([liar], kind=DataKind.OBSERVATION)


async def test_series_without_any_usable_value_counts_as_failure():
    empty = Fake("empty", caps("empty"), series("empty", DataKind.FORECAST, value=None), [])
    good = Fake("good", caps("good"), series("good", DataKind.FORECAST), [])
    s, attempts = await run([empty, good])
    assert s.provider == "good" and "no usable value" in (attempts[0].reason or "")


async def test_all_failed_raises_unavailable_listing_every_attempt():
    a = Fake("a", caps("a"), ProviderUnavailableError("a: timeout"), [])
    b = Fake("b", caps("b"), ProviderRateLimitError("b: quota"), [])
    with pytest.raises(ProviderUnavailableError) as exc:
        await run([a, b])
    msg = str(exc.value)
    assert "a: failed (a: timeout)" in msg and "b: failed (b: quota)" in msg


async def test_nothing_eligible_is_a_capability_error_not_an_outage():
    a = Fake("a", caps("a", forecast=True, obs=False), series("a", DataKind.FORECAST), [])
    with pytest.raises(CapabilityNotSupportedError, match="does not provide observations"):
        await run([a], kind=DataKind.OBSERVATION)
    with pytest.raises(CapabilityNotSupportedError, match="no providers configured"):
        await run([])


async def test_unsupported_kinds_are_rejected():
    with pytest.raises(CapabilityNotSupportedError):
        await run([], kind=DataKind.CLEAR_SKY_MODEL)


async def test_analysis_goes_only_to_archive_capable_providers():
    log: list[str] = []
    f = Fake("f", caps("f"), series("f", DataKind.ANALYSIS), log)
    a = Fake("arch", caps("arch", archive=True), series("arch", DataKind.ANALYSIS), log)
    s, _ = await run([f, a], kind=DataKind.ANALYSIS)
    assert s.provider == "arch" and s.kind is DataKind.ANALYSIS and log == ["arch.analysis"]


def test_available_providers_is_ordered_and_protocol_conformant(tmp_path):
    ps = available_providers(Settings(_env_file=None, cache_dir=tmp_path))
    assert list(ps) == ["open-meteo", "bom"]
    for name, p in ps.items():
        assert isinstance(p, WeatherProvider) and p.name == name
    assert ps["open-meteo"].capabilities.supports_forecast
    assert not ps["open-meteo"].capabilities.supports_observations
    assert ps["bom"].capabilities.supports_observations


@respx.mock
async def test_real_adapters_observations_skip_openmeteo_and_use_bom(tmp_path):
    bom = json.loads((FIXTURES / "bom_idd60910_94120_darwin_trimmed.json").read_text())
    respx.get("http://www.bom.gov.au/fwo/IDD60910/IDD60910.94120.json").respond(
        301, headers={"Location": "https://www.bom.gov.au/fwo/IDD60910/IDD60910.94120.json"}
    )
    respx.get("https://www.bom.gov.au/fwo/IDD60910/IDD60910.94120.json").respond(200, json=bom)
    ps = available_providers(Settings(_env_file=None, cache_dir=tmp_path / "c"))
    w = TimeWindow(
        start_utc=datetime(2026, 11, 1, 20, tzinfo=UTC),
        end_utc=datetime(2026, 11, 1, 22, tzinfo=UTC),
    )
    s, attempts = await fetch_with_fallback_detailed(
        ps, POINT, w, ["temperature_2m"], kind=DataKind.OBSERVATION, now=NOW
    )
    assert s.provider == "bom" and s.kind is DataKind.OBSERVATION
    assert attempts[0].provider == "open-meteo" and attempts[0].outcome == "skipped"
    assert "does not provide observations" in (attempts[0].reason or "")
    note = s.steps[0].values["temperature_2m"].provenance.notes or ""
    assert "open-meteo: skipped" in note


@respx.mock
async def test_real_adapters_all_fail_when_offline_and_uncached(tmp_path):
    ps = available_providers(
        Settings(_env_file=None, cache_dir=tmp_path / "c", allow_network=False)
    )
    w = TimeWindow(
        start_utc=datetime(2026, 11, 2, 2, tzinfo=UTC), end_utc=datetime(2026, 11, 2, 4, tzinfo=UTC)
    )
    with pytest.raises(ProviderUnavailableError, match="no cached data"):
        await fetch_with_fallback(ps, POINT, w, ["temperature_2m"], kind=DataKind.FORECAST)


async def test_close_providers_closes_owned_clients(tmp_path):
    ps = available_providers(Settings(_env_file=None, cache_dir=tmp_path))
    client = ps["bom"]._http()  # create the lazy client
    assert not client.is_closed
    await close_providers(ps)
    assert client.is_closed
