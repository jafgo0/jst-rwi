"""Open-Meteo adapter replayed from REAL captured responses (tests/fixtures). No network."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from rwi.config import Settings
from rwi.core.errors import (
    CapabilityNotSupportedError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    UnknownVariableError,
)
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, QualityFlag
from rwi.core.timebase import TimeWindow
from rwi.providers.openmeteo import OpenMeteoProvider

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
META_URL = "https://api.open-meteo.com/data/ecmwf_ifs025/static/meta.json"
NOW = datetime(2026, 11, 1, 22, 30, tzinfo=UTC)
# The point we ASK for; the fixture's grid cell is (-12.5, 130.75) - a different place.
DARWIN = GeoPoint(latitude_deg=-12.4634, longitude_deg=130.8456, elevation_m=30.0)
FORECAST_WINDOW = TimeWindow(
    start_utc=datetime(2026, 11, 2, 2, 0, tzinfo=UTC),
    end_utc=datetime(2026, 11, 2, 4, 0, tzinfo=UTC),
)
VARS = ["temperature_2m", "pressure_msl", "wind_speed_10m", "wind_gusts_10m", "ghi", "dni", "dhi"]


def fx(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def settings(tmp_path: Path, **kw: Any) -> Settings:
    base: dict[str, Any] = {
        "cache_dir": tmp_path / "cache",
        "http_retries": 2,
        "http_backoff_s": 0.01,
        "allow_network": True,
    }
    base.update(kw)
    return Settings(_env_file=None, **base)


def make(tmp_path: Path, clock: Clock | None = None, **kw: Any) -> OpenMeteoProvider:
    st = kw.pop("settings", None) or settings(tmp_path)
    kw.setdefault("forecast_model", "ecmwf_ifs025")
    return OpenMeteoProvider(st, clock=clock or Clock(), sleep=Sleeps(), **kw)


def mock_forecast(payload: Any | None = None, meta: bool = True) -> tuple[respx.Route, respx.Route]:
    f = respx.get(FORECAST_URL).respond(
        200, json=payload or fx("openmeteo_forecast_darwin_ifs025.json")
    )
    m = (
        respx.get(META_URL).respond(200, json=fx("openmeteo_meta_ecmwf_ifs025.json"))
        if meta
        else respx.get(META_URL).respond(500)
    )
    return f, m


# --------------------------------------------------------------------------- units & names


@respx.mock
async def test_pressure_hpa_becomes_pascal_and_kmh_becomes_ms(tmp_path):
    mock_forecast()
    s = await make(tmp_path).fetch_forecast(DARWIN, FORECAST_WINDOW, VARS)
    step = s.nearest_step(datetime(2026, 11, 2, 3, 0, tzinfo=UTC), tolerance_s=0)
    assert step is not None
    # fixture, 03:00Z: pressure_msl 1014.0 hPa, wind 8.6 km/h, gust 26.6 km/h
    assert step.value("pressure_msl") == pytest.approx(101400.0, abs=1e-6)
    assert step.value("wind_speed_10m") == pytest.approx(8.6 / 3.6, rel=1e-9)
    assert step.value("wind_speed_10m") == pytest.approx(2.388889, abs=1e-6)
    assert step.value("wind_gusts_10m") == pytest.approx(26.6 / 3.6, rel=1e-9)
    assert step.values["pressure_msl"].unit == "Pa"
    assert step.values["wind_speed_10m"].unit == "m/s"
    assert step.value("temperature_2m") == pytest.approx(32.4)  # degC unchanged


@respx.mock
async def test_radiation_names_map_to_canonical_and_request_uses_api_names(tmp_path):
    f, _ = mock_forecast()
    s = await make(tmp_path).fetch_forecast(DARWIN, FORECAST_WINDOW, ["ghi", "dni", "dhi"])
    step = s.nearest_step(datetime(2026, 11, 2, 3, 0, tzinfo=UTC), tolerance_s=0)
    assert (step.value("ghi"), step.value("dni"), step.value("dhi")) == (892.0, 576.3, 322.5)
    assert step.values["ghi"].unit == "W/m2"
    q = dict(f.calls.last.request.url.params)
    assert q["hourly"] == "shortwave_radiation,direct_normal_irradiance,diffuse_radiation"


@respx.mock
async def test_instant_radiation_mode_requests_instant_columns(tmp_path):
    payload = fx("openmeteo_forecast_darwin_ifs025.json")
    for src, dst in (("shortwave_radiation", "shortwave_radiation_instant"),):
        payload["hourly"][dst] = payload["hourly"].pop(src)
        payload["hourly_units"][dst] = payload["hourly_units"].pop(src)
    f, _ = mock_forecast(payload)
    p = make(tmp_path, radiation_mode="instant")
    s = await p.fetch_forecast(DARWIN, FORECAST_WINDOW, ["ghi"])
    assert "shortwave_radiation_instant" in f.calls.last.request.url.params["hourly"]
    assert s.steps[1].value("ghi") == 892.0
    assert "instantaneous" in (s.steps[0].values["ghi"].provenance.notes or "")


@respx.mock
async def test_conversion_follows_the_unit_the_api_reports_not_a_hardcoded_one(tmp_path):
    payload = fx("openmeteo_forecast_darwin_ifs025.json")
    payload["hourly_units"]["wind_speed_10m"] = "m/s"  # as if wind_speed_unit=ms had been honoured
    mock_forecast(payload)
    s = await make(tmp_path).fetch_forecast(DARWIN, FORECAST_WINDOW, ["wind_speed_10m"])
    assert s.steps[1].value("wind_speed_10m") == pytest.approx(8.6)  # untouched


@respx.mock
async def test_unknown_unit_is_rejected_instead_of_guessed(tmp_path):
    payload = fx("openmeteo_forecast_darwin_ifs025.json")
    payload["hourly_units"]["wind_speed_10m"] = "mph"
    mock_forecast(payload)
    with pytest.raises(ProviderUnavailableError, match="unexpected unit 'mph'"):
        await make(tmp_path).fetch_forecast(DARWIN, FORECAST_WINDOW, ["wind_speed_10m"])


@respx.mock
async def test_out_of_range_values_are_flagged_not_clipped(tmp_path):
    payload = fx("openmeteo_forecast_darwin_ifs025.json")
    payload["hourly"]["pressure_msl"][2] = 2000.0  # 200000 Pa: impossible
    payload["hourly"]["temperature_2m"][2] = 99.0
    mock_forecast(payload)
    s = await make(tmp_path).fetch_forecast(
        DARWIN, FORECAST_WINDOW, ["pressure_msl", "temperature_2m"]
    )
    bad = s.nearest_step(datetime(2026, 11, 2, 3, 0, tzinfo=UTC), tolerance_s=0)
    assert bad.values["pressure_msl"].quality is QualityFlag.OUT_OF_RANGE
    assert bad.values["pressure_msl"].value == pytest.approx(200000.0)  # kept for inspection
    assert bad.value("pressure_msl") is None  # but not usable
    assert bad.values["temperature_2m"].quality is QualityFlag.OUT_OF_RANGE
    ok = s.nearest_step(datetime(2026, 11, 2, 2, 0, tzinfo=UTC), tolerance_s=0)
    assert ok.values["pressure_msl"].quality is QualityFlag.OK


@respx.mock
async def test_null_values_are_missing_never_zero(tmp_path):
    """Real response: bom_access_global in Open-Meteo is stale and returns only nulls."""
    respx.get(FORECAST_URL).respond(200, json=fx("openmeteo_forecast_all_null_bom_access.json"))
    # Metadata unreachable: the staleness guard cannot fire, so the nulls must stay MISSING.
    respx.get(BOM_META_URL).respond(503)
    window = TimeWindow(
        start_utc=datetime(2026, 11, 1, 22, tzinfo=UTC),
        end_utc=datetime(2026, 11, 1, 23, tzinfo=UTC),
    )
    s = await make(tmp_path).fetch_forecast(
        DARWIN, window, ["temperature_2m", "visibility"], models="bom_access_global"
    )
    assert len(s.steps) == 2
    for step in s.steps:
        for m in step.values.values():
            assert m.value is None and m.quality is QualityFlag.MISSING
            assert not m.is_usable
    assert s.steps[0].value("temperature_2m") is None


BOM_META_URL = "https://api.open-meteo.com/data/bom_access_global/static/meta.json"
BOM_ENS_META_URL = "https://api.open-meteo.com/data/bom_access_global_ensemble/static/meta.json"


@respx.mock
async def test_stale_model_is_refused_not_served_as_nulls(tmp_path):
    """Real metadata (2026-11-27): ACCESS-G data ends 2025-06-30 07Z, run 2025-06-26 18Z."""
    data = respx.get(FORECAST_URL).respond(
        200, json=fx("openmeteo_forecast_all_null_bom_access.json")
    )
    respx.get(BOM_META_URL).respond(200, json=fx("openmeteo_meta_bom_access_global.json"))
    window = TimeWindow(
        start_utc=datetime(2026, 11, 1, 22, tzinfo=UTC),
        end_utc=datetime(2026, 11, 1, 23, tzinfo=UTC),
    )
    p = make(tmp_path)
    with pytest.raises(ProviderUnavailableError, match="stale") as exc:
        await p.fetch_forecast(DARWIN, window, ["temperature_2m"], models="bom_access_global")
    msg = str(exc.value)
    assert "bom_access_global" in msg
    assert "2025-06-26T18:00:00+00:00" in msg and "2025-06-30T07:00:00+00:00" in msg
    # Not cached: a second call asks again instead of replaying the refusal.
    with pytest.raises(ProviderUnavailableError):
        await p.fetch_forecast(DARWIN, window, ["temperature_2m"], models="bom_access_global")
    assert data.call_count == 2


@respx.mock
async def test_stale_ensemble_is_refused(tmp_path):
    """Real metadata (2026-11-27): ACCESS-GE data ends 2025-07-07 03Z."""
    respx.get(ENSEMBLE_URL).respond(200, json=fx("openmeteo_ensemble_darwin_ifs025.json"))
    respx.get(BOM_ENS_META_URL).respond(
        200, json=fx("openmeteo_meta_bom_access_global_ensemble.json")
    )
    window = TimeWindow(
        start_utc=datetime(2026, 11, 2, 3, tzinfo=UTC),
        end_utc=datetime(2026, 11, 2, 5, tzinfo=UTC),
    )
    with pytest.raises(ProviderUnavailableError, match="2025-07-07T03:00:00"):
        await make(tmp_path).fetch_ensemble(
            DARWIN, window, ["temperature_2m"], models="bom_access_global_ensemble"
        )


@respx.mock
async def test_live_model_whose_data_reaches_the_window_is_served(tmp_path):
    """The real ecmwf_ifs025 metadata ends 2026-11-16 15Z, after the window: no refusal."""
    mock_forecast()
    s = await make(tmp_path).fetch_forecast(DARWIN, FORECAST_WINDOW, VARS)
    assert s.steps and s.model_init_time_utc is not None


# --------------------------------------------------------------------------- time handling


@respx.mock
async def test_timestamps_are_aware_utc_and_request_forces_utc(tmp_path):
    f, _ = mock_forecast()
    s = await make(tmp_path).fetch_forecast(DARWIN, FORECAST_WINDOW, VARS)
    assert [t.isoformat() for t in s.times] == [
        "2026-11-02T02:00:00+00:00",
        "2026-11-02T03:00:00+00:00",
        "2026-11-02T04:00:00+00:00",
    ]
    for t in s.times:
        assert t.tzinfo is not None and t.utcoffset() == timedelta(0)
    q = f.calls.last.request.url.params
    assert q["timezone"] == "UTC"
    assert q["start_hour"] == "2026-11-02T02:00" and q["end_hour"] == "2026-11-02T04:00"
    assert s.temporal_resolution_s == 3600.0


@respx.mock
async def test_window_is_widened_to_bracketing_hours(tmp_path):
    f, _ = mock_forecast()
    w = TimeWindow(
        start_utc=datetime(2026, 11, 2, 2, 20, tzinfo=UTC),
        end_utc=datetime(2026, 11, 2, 3, 40, tzinfo=UTC),
    )
    s = await make(tmp_path).fetch_forecast(DARWIN, w, ["temperature_2m"])
    assert f.calls.last.request.url.params["start_hour"] == "2026-11-02T02:00"
    assert f.calls.last.request.url.params["end_hour"] == "2026-11-02T04:00"
    assert s.times[0] <= w.start_utc and s.times[-1] >= w.end_utc


@respx.mock
async def test_non_utc_response_is_refused(tmp_path):
    payload = fx("openmeteo_forecast_darwin_ifs025.json")
    payload["utc_offset_seconds"] = 34200  # ACST: local-time mode must never be consumed
    mock_forecast(payload)
    with pytest.raises(ProviderUnavailableError, match="not in UTC"):
        await make(tmp_path).fetch_forecast(DARWIN, FORECAST_WINDOW, VARS)


# --------------------------------------------------------------------------- provenance


@respx.mock
async def test_requested_and_returned_coordinates_are_both_recorded(tmp_path):
    mock_forecast()
    s = await make(tmp_path).fetch_forecast(DARWIN, FORECAST_WINDOW, VARS)
    assert s.requested_point == DARWIN
    assert (s.point.latitude_deg, s.point.longitude_deg, s.point.elevation_m) == (
        -12.5,
        130.75,
        32.0,
    )
    assert s.point != s.requested_point
    prov = s.steps[0].values["ghi"].provenance
    assert prov.point == s.point and prov.requested_point == DARWIN
    assert "nearest_grid_cell" in (prov.processing or "")


@respx.mock
async def test_series_provenance_fields(tmp_path):
    mock_forecast()
    s = await make(tmp_path).fetch_forecast(DARWIN, FORECAST_WINDOW, VARS)
    assert s.provider == "open-meteo" and s.dataset == "forecast" and s.kind is DataKind.FORECAST
    assert s.model_name == "ecmwf_ifs025" and s.spatial_resolution_km == 25.0
    assert s.retrieved_at_utc == NOW
    assert "CC BY 4.0" in (s.license or "") and "non-commercial" in (s.license or "").lower()
    assert (s.source_url or "").startswith(FORECAST_URL) and "timezone=UTC" in (s.source_url or "")
    prov = s.steps[1].values["temperature_2m"].provenance
    assert prov.kind is DataKind.FORECAST and prov.valid_time_utc == s.times[1]
    assert "PRECEDING hour" in (prov.notes or "")  # radiation / gust time semantics stated


@respx.mock
async def test_model_init_time_comes_from_metadata_api_and_gives_lead_time(tmp_path):
    _, m = mock_forecast()
    s = await make(tmp_path).fetch_forecast(DARWIN, FORECAST_WINDOW, VARS)
    assert m.call_count == 1
    assert s.model_init_time_utc == datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    prov = s.steps[1].values["temperature_2m"].provenance  # valid 03:00Z next day
    assert prov.lead_time_s == 15 * 3600.0
    assert "Metadata API" in (prov.notes or "")
    # native step 10800 s -> hourly values are interpolated; the series says so
    assert "interpolated" in (prov.notes or "")


@respx.mock
async def test_model_init_time_withheld_when_run_just_became_available(tmp_path):
    mock_forecast()
    avail = datetime(2026, 11, 1, 19, 50, 45, tzinfo=UTC)
    clock = Clock(avail + timedelta(seconds=120))  # < 10 min after availability
    s = await make(tmp_path, clock).fetch_forecast(DARWIN, FORECAST_WINDOW, VARS)
    assert s.model_init_time_utc is None
    assert "withheld" in (s.steps[0].values["ghi"].provenance.notes or "")


@respx.mock
async def test_model_init_time_none_when_metadata_unavailable(tmp_path):
    mock_forecast(meta=False)
    s = await make(tmp_path, settings=settings(tmp_path, http_retries=0)).fetch_forecast(
        DARWIN, FORECAST_WINDOW, VARS
    )
    assert s.model_init_time_utc is None
    assert "unknown" in (s.steps[0].values["ghi"].provenance.notes or "")


@respx.mock
async def test_best_match_never_claims_an_init_time(tmp_path):
    f = respx.get(FORECAST_URL).respond(200, json=fx("openmeteo_forecast_darwin_ifs025.json"))
    meta = respx.get(url__regex=r".*/data/.*meta\.json").respond(200, json={})
    s = await make(tmp_path, forecast_model="best_match").fetch_forecast(
        DARWIN, FORECAST_WINDOW, VARS
    )
    assert s.model_name == "best_match" and s.model_init_time_utc is None
    assert s.spatial_resolution_km is None
    assert meta.call_count == 0 and f.call_count == 1
    assert "best_match" in (s.steps[0].values["ghi"].provenance.notes or "")


# --------------------------------------------------------------------------- capabilities


def test_capabilities_are_honest(tmp_path):
    caps = make(tmp_path).capabilities
    assert caps.provider == "open-meteo"
    assert caps.supports_forecast and caps.supports_ensemble and caps.supports_archive
    assert not caps.supports_observations
    assert not caps.requires_api_key
    assert caps.max_forecast_hours == 384.0
    assert {"ghi", "dni", "dhi", "pressure_msl", "wind_speed_10m"} <= caps.variables
    assert "CC BY 4.0" in caps.license and "non-commercial" in caps.license.lower()
    assert "open-meteo.com" in caps.attribution and caps.documentation_url.startswith("https://")


async def test_observations_are_not_supported_and_never_relabelled(tmp_path):
    p = make(tmp_path)
    with pytest.raises(CapabilityNotSupportedError, match="no observations"):
        await p.fetch_observations(DARWIN, FORECAST_WINDOW, ["temperature_2m"])


async def test_unknown_variable_is_a_caller_error(tmp_path):
    with pytest.raises(UnknownVariableError):
        await make(tmp_path).fetch_forecast(DARWIN, FORECAST_WINDOW, ["not_a_variable"])


async def test_forecast_beyond_horizon_is_refused_without_a_request(tmp_path):
    w = TimeWindow(
        start_utc=datetime(2026, 11, 20, tzinfo=UTC), end_utc=datetime(2026, 11, 21, tzinfo=UTC)
    )
    # any request would raise: nothing is mocked
    with respx.mock, pytest.raises(CapabilityNotSupportedError, match="reach at most"):
        await make(tmp_path).fetch_forecast(DARWIN, w, ["temperature_2m"])


async def test_only_one_model_per_request(tmp_path):
    with pytest.raises(CapabilityNotSupportedError, match="one model"):
        await make(tmp_path).fetch_forecast(
            DARWIN, FORECAST_WINDOW, ["temperature_2m"], models="ecmwf_ifs025,gfs_seamless"
        )


@respx.mock
async def test_api_key_is_only_sent_to_customer_hosts(tmp_path):
    f, _ = mock_forecast()
    p = make(tmp_path, settings=settings(tmp_path, openmeteo_api_key="SECRET"))
    await p.fetch_forecast(DARWIN, FORECAST_WINDOW, ["temperature_2m"])
    assert "apikey" not in f.calls.last.request.url.params  # free host: never leak the key


# --------------------------------------------------------------------------- errors, retry, cache


@respx.mock
async def test_http_400_reason_is_surfaced_and_not_retried(tmp_path):
    r = respx.get(FORECAST_URL).respond(400, json=fx("openmeteo_error_400_out_of_range.json"))
    p = make(tmp_path)
    with pytest.raises(ProviderUnavailableError, match="out of allowed range"):
        await p.fetch_forecast(DARWIN, FORECAST_WINDOW, VARS)
    assert r.call_count == 1


@respx.mock
async def test_retry_on_429_then_success(tmp_path):
    payload = fx("openmeteo_forecast_darwin_ifs025.json")
    r = respx.get(FORECAST_URL).mock(
        side_effect=[
            httpx.Response(429, json={"error": True, "reason": "Minutely limit"}),
            httpx.Response(200, json=payload),
        ]
    )
    respx.get(META_URL).respond(200, json=fx("openmeteo_meta_ecmwf_ifs025.json"))
    sleeps = Sleeps()
    p = OpenMeteoProvider(
        settings(tmp_path), forecast_model="ecmwf_ifs025", clock=Clock(), sleep=sleeps
    )
    s = await p.fetch_forecast(DARWIN, FORECAST_WINDOW, VARS)
    assert r.call_count == 2 and len(s.steps) == 3 and sleeps.calls == [0.01]


@respx.mock
async def test_provider_unavailable_after_exhausting_retries(tmp_path):
    r = respx.get(FORECAST_URL).respond(502)
    with pytest.raises(ProviderUnavailableError):
        await make(tmp_path).fetch_forecast(DARWIN, FORECAST_WINDOW, VARS)
    assert r.call_count == 3


@respx.mock
async def test_real_daily_limit_response_raises_rate_limit_error(tmp_path):
    cap = fx("openmeteo_429_daily_limit.json")
    respx.get(FORECAST_URL).respond(cap["status_code"], headers=cap["headers"], json=cap["body"])
    with pytest.raises(ProviderRateLimitError, match="Daily API request limit"):
        await make(tmp_path).fetch_forecast(DARWIN, FORECAST_WINDOW, VARS)


@respx.mock
async def test_cache_hit_returns_series_with_original_retrieved_at(tmp_path):
    f, _ = mock_forecast()
    clock = Clock()
    p = make(tmp_path, clock)
    first = await p.fetch_forecast(DARWIN, FORECAST_WINDOW, VARS)
    clock.now = NOW + timedelta(minutes=10)
    second = await p.fetch_forecast(DARWIN, FORECAST_WINDOW, VARS)
    assert f.call_count == 1
    assert second.retrieved_at_utc == first.retrieved_at_utc == NOW
    assert second.model_init_time_utc == first.model_init_time_utc
    assert "cache" in (second.steps[0].values["ghi"].provenance.notes or "").lower()
    assert second.steps[0].values["ghi"].provenance.age_s(clock.now) == pytest.approx(600.0)
    # another position inside the same ~1 km cell is also served from the cache
    near = GeoPoint(latitude_deg=-12.4631, longitude_deg=130.8459)
    third = await p.fetch_forecast(near, FORECAST_WINDOW, VARS)
    assert f.call_count == 1 and third.requested_point == near  # requested point is NOT cached


@respx.mock
async def test_offline_mode_serves_cache_or_raises_and_never_invents(tmp_path):
    f, _ = mock_forecast()
    clock = Clock()
    online = make(tmp_path, clock)
    cached = await online.fetch_forecast(DARWIN, FORECAST_WINDOW, VARS)
    calls_before = f.call_count

    clock.now = NOW + timedelta(hours=6)
    offline = make(tmp_path, clock, settings=settings(tmp_path, allow_network=False))
    served = await offline.fetch_forecast(DARWIN, FORECAST_WINDOW, VARS)
    assert f.call_count == calls_before  # no request
    assert served.retrieved_at_utc == cached.retrieved_at_utc == NOW
    assert [s.value("ghi") for s in served.steps] == [s.value("ghi") for s in cached.steps]

    other = TimeWindow(
        start_utc=datetime(2026, 11, 5, tzinfo=UTC), end_utc=datetime(2026, 11, 5, 6, tzinfo=UTC)
    )
    with pytest.raises(ProviderUnavailableError, match="no cached data"):
        await offline.fetch_forecast(DARWIN, other, VARS)  # nothing cached for this request
    assert f.call_count == calls_before


# --------------------------------------------------------------------------- archive / analysis


@respx.mock
async def test_archive_is_analysis_kind_with_night_zero_and_no_init_time(tmp_path):
    a = respx.get(ARCHIVE_URL).respond(200, json=fx("openmeteo_archive_darwin_ifs.json"))
    w = TimeWindow(
        start_utc=datetime(2026, 10, 20, 8, tzinfo=UTC),
        end_utc=datetime(2026, 10, 20, 14, tzinfo=UTC),
    )
    s = await make(tmp_path).fetch_analysis(
        DARWIN, w, ["ghi", "dni", "dhi", "is_day", "pressure_msl"]
    )
    assert s.kind is DataKind.ANALYSIS and s.dataset == "archive" and s.model_name == "ecmwf_ifs"
    assert s.model_init_time_utc is None
    q = a.calls.last.request.url.params
    assert (
        q["start_date"] == "2026-10-20"
        and q["end_date"] == "2026-10-20"
        and q["models"] == "ecmwf_ifs"
    )
    assert q["timezone"] == "UTC"
    assert s.times[0] == w.start_utc and s.times[-1] == w.end_utc
    night = s.nearest_step(datetime(2026, 10, 20, 12, tzinfo=UTC), tolerance_s=0)
    assert night.value("ghi") == 0.0 and night.value("is_day") == 0.0  # night zero is a value
    assert s.spatial_resolution_km == 9.0
    assert "Kind=ANALYSIS" in (s.steps[0].values["ghi"].provenance.notes or "")


async def test_archive_refuses_future_windows(tmp_path):
    w = TimeWindow(
        start_utc=datetime(2026, 11, 3, tzinfo=UTC), end_utc=datetime(2026, 11, 4, tzinfo=UTC)
    )
    with respx.mock, pytest.raises(CapabilityNotSupportedError, match="future"):
        await make(tmp_path).fetch_analysis(DARWIN, w, ["ghi"])


# --------------------------------------------------------------------------- health


@respx.mock
async def test_health_reports_reachable_and_unreachable(tmp_path):
    respx.get(FORECAST_URL).respond(200, json=fx("openmeteo_forecast_darwin_ifs025.json"))
    h = await make(tmp_path).health()
    assert h.reachable and h.latency_ms is not None and h.provider == "open-meteo"
    respx.reset()
    respx.get(FORECAST_URL).respond(503)
    h = await make(tmp_path).health()
    assert not h.reachable and "503" in (h.message or "")


def test_fixture_payload_is_not_mutated_between_tests():
    # guards the copy.deepcopy-free style used above: fixtures are re-read per call
    a, b = fx("openmeteo_forecast_darwin_ifs025.json"), fx("openmeteo_forecast_darwin_ifs025.json")
    assert a == b and a is not b
    assert copy.deepcopy(a) == b


# --------------------------------------------------------------------------- ensemble

ENS_META_URL = "https://api.open-meteo.com/data/ecmwf_ifs025_ensemble/static/meta.json"
ENS_WINDOW = TimeWindow(
    start_utc=datetime(2026, 11, 2, 3, 0, tzinfo=UTC),
    end_utc=datetime(2026, 11, 2, 5, 0, tzinfo=UTC),
)
ENS_VARS = ["temperature_2m", "wind_speed_10m", "pressure_msl"]


def ensemble_meta() -> dict[str, Any]:
    """Shape of the ensemble Metadata API response (values from 2026-11-01 22:20 UTC)."""
    return {
        "last_run_availability_time": 1793547127,
        "last_run_initialisation_time": 1793512800,
        "temporal_resolution_seconds": 10800,
        "update_interval_seconds": 21600,
    }


@respx.mock
async def test_ensemble_returns_one_series_per_member_with_converted_units(tmp_path):
    raw = fx("openmeteo_ensemble_darwin_ifs025.json")
    respx.get(ENSEMBLE_URL).respond(200, json=raw)
    meta = respx.get(ENS_META_URL).respond(200, json=ensemble_meta())
    ens = await make(tmp_path).fetch_ensemble(DARWIN, ENS_WINDOW, ENS_VARS)
    assert meta.call_count == 1
    assert ens.provider == "open-meteo" and ens.model_name == "ecmwf_ifs025"
    assert len(ens.members) == 51  # control + 50 perturbed members
    datasets = [m.dataset for m in ens.members]
    assert datasets[0] == "ensemble/control" and datasets[1] == "ensemble/member01"
    assert datasets[-1] == "ensemble/member50" and len(set(datasets)) == 51
    assert all(m.kind is DataKind.FORECAST and m.model_name == "ecmwf_ifs025" for m in ens.members)
    assert all(len(m.steps) == 3 for m in ens.members)
    # member07 against the raw payload, converted
    member = next(m for m in ens.members if m.dataset == "ensemble/member07")
    raw_wind = raw["hourly"]["wind_speed_10m_member07"][1]
    raw_p = raw["hourly"]["pressure_msl_member07"][1]
    assert member.steps[1].value("wind_speed_10m") == pytest.approx(raw_wind / 3.6)
    assert member.steps[1].value("pressure_msl") == pytest.approx(raw_p * 100.0)
    control = ens.members[0]
    assert control.steps[0].value("temperature_2m") == raw["hourly"]["temperature_2m"][0]
    assert ens.point == control.point and ens.point.latitude_deg == raw["latitude"]
    assert control.requested_point == DARWIN


@respx.mock
async def test_ensemble_members_differ_and_member_values_helper_works(tmp_path):
    respx.get(ENSEMBLE_URL).respond(200, json=fx("openmeteo_ensemble_darwin_ifs025.json"))
    respx.get(ENS_META_URL).respond(200, json=ensemble_meta())
    ens = await make(tmp_path).fetch_ensemble(DARWIN, ENS_WINDOW, ENS_VARS)
    vals = ens.member_values("temperature_2m", datetime(2026, 11, 2, 4, 0, tzinfo=UTC))
    assert len(vals) == 51
    assert len(set(vals)) > 1  # genuinely different members, not 51 copies
    assert all(15.0 < v < 50.0 for v in vals)


@respx.mock
async def test_ensemble_provenance_has_init_time_and_native_step_note(tmp_path):
    respx.get(ENSEMBLE_URL).respond(200, json=fx("openmeteo_ensemble_darwin_ifs025.json"))
    respx.get(ENS_META_URL).respond(200, json=ensemble_meta())
    ens = await make(tmp_path).fetch_ensemble(DARWIN, ENS_WINDOW, ENS_VARS)
    m = ens.members[3]
    assert m.model_init_time_utc == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
    prov = m.steps[0].values["temperature_2m"].provenance
    assert prov.lead_time_s == 21 * 3600.0  # 03:00Z next day minus 06:00Z run
    assert "member03" in (prov.notes or "") and "interpolated" in (prov.notes or "")
    assert m.spatial_resolution_km == 25.0 and m.source_url.startswith(ENSEMBLE_URL)


@respx.mock
async def test_ensemble_without_member_columns_is_an_error(tmp_path):
    payload = fx("openmeteo_ensemble_darwin_ifs025.json")
    payload["hourly"] = {k: v for k, v in payload["hourly"].items() if "_member" not in k}
    respx.get(ENSEMBLE_URL).respond(200, json=payload)
    respx.get(ENS_META_URL).respond(200, json=ensemble_meta())
    with pytest.raises(ProviderUnavailableError, match="no member columns"):
        await make(tmp_path).fetch_ensemble(DARWIN, ENS_WINDOW, ENS_VARS)


async def test_ensemble_refuses_history_and_horizon_beyond_the_model(tmp_path):
    p = make(tmp_path)
    old = TimeWindow(
        start_utc=datetime(2026, 10, 20, tzinfo=UTC), end_utc=datetime(2026, 10, 21, tzinfo=UTC)
    )
    far = TimeWindow(
        start_utc=datetime(2026, 11, 20, tzinfo=UTC), end_utc=datetime(2026, 11, 21, tzinfo=UTC)
    )
    with respx.mock:  # nothing mocked: a request would raise
        with pytest.raises(CapabilityNotSupportedError, match="3 days"):
            await p.fetch_ensemble(DARWIN, old, ["temperature_2m"])
        with pytest.raises(CapabilityNotSupportedError, match="ahead"):
            await p.fetch_ensemble(DARWIN, far, ["temperature_2m"])


@respx.mock
async def test_ensemble_rate_limit_is_typed_and_not_retried_for_a_daily_quota(tmp_path):
    cap = fx("openmeteo_429_daily_limit.json")
    route = respx.get(ENSEMBLE_URL).respond(
        cap["status_code"], headers=cap["headers"], json=cap["body"]
    )
    with pytest.raises(ProviderRateLimitError):
        await make(tmp_path).fetch_ensemble(DARWIN, ENS_WINDOW, ENS_VARS)
    assert route.call_count == 1


# --------------------------------------------------------------------------- instability


@respx.mock
@pytest.mark.science
async def test_lifted_index_sign_convention_is_negative_for_unstable(tmp_path):
    """Pin Open-Meteo's lifted_index sign convention.

    Open-Meteo inherits the Galway (1956) definition of Lifted Index:
    LI = T_environment(500 hPa) - T_parcel(500 hPa), so a NEGATIVE value
    marks an unstable column (parcel warmer than environment at 500 hPa).
    This is the same convention used by SPC, the AMS Glossary of
    Meteorology and WMO No. 782. Our convection alert thresholds
    (LI <= 0 / -2 / -4) are only correct under this convention, so the
    adapter must not silently flip the sign at the boundary.

    References:
    - Galway, J. G. (1956). "The Lifted Index as a Predictor of Latent
      Instability", Bull. Amer. Meteor. Soc. 37(10), 528-529.
    - AMS Glossary of Meteorology, s.v. "lifted index".
    - Open-Meteo public forecast API documentation, hourly variable
      `lifted_index` (verified 2026-11-08).

    We inject a value of -5.2 K into the fixture - a magnitude that any
    production Open-Meteo call over a convective afternoon returns as a
    negative number - and assert the adapter preserves the sign. CAPE is
    injected alongside at a strongly-unstable 2800 J/kg to assert its
    pass-through unit as well.
    """
    payload = fx("openmeteo_forecast_darwin_ifs025.json")
    length = len(payload["hourly"]["time"])
    payload["hourly"]["lifted_index"] = [-5.2] * length
    payload["hourly_units"]["lifted_index"] = ""
    payload["hourly"]["cape"] = [2800.0] * length
    payload["hourly_units"]["cape"] = "J/kg"
    mock_forecast(payload)
    series = await make(tmp_path).fetch_forecast(DARWIN, FORECAST_WINDOW, ["lifted_index", "cape"])
    step = series.nearest_step(datetime(2026, 11, 2, 3, 0, tzinfo=UTC), tolerance_s=0)
    assert step is not None
    # Sign is preserved: a negative API value remains negative canonical.
    li = step.value("lifted_index")
    assert li is not None
    assert li < 0.0, f"expected negative LI for unstable column, got {li}"
    assert li == pytest.approx(-5.2)
    assert step.values["lifted_index"].unit == "K"
    assert step.value("cape") == pytest.approx(2800.0)
    assert step.values["cape"].unit == "J/kg"
