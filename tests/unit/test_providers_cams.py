"""CAMS aerosol via Open-Meteo Air Quality: replay of a recorded response (ADR 0014)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from rwi.config import Settings
from rwi.core.errors import ProviderUnavailableError
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, QualityFlag
from rwi.providers.cams import CAMS_DOMAIN, CamsAerosolProvider

FIXTURE = Path(__file__).parents[1] / "fixtures" / "openmeteo_aq_cams_darwin.json"
DARWIN = GeoPoint(latitude_deg=-12.4634, longitude_deg=130.8456)
START = datetime(2026, 11, 27, 0, tzinfo=UTC)
END = datetime(2026, 11, 28, 23, tzinfo=UTC)


def _provider(tmp_path: Path) -> CamsAerosolProvider:
    return CamsAerosolProvider(Settings(_env_file=None, cache_dir=tmp_path, http_retries=0))  # type: ignore[call-arg]


async def test_replay_gives_forecast_series_with_units(tmp_path: Path) -> None:
    p = _provider(tmp_path)
    with respx.mock() as mock:
        route = mock.get(url__regex=r".*/air-quality.*").mock(
            return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text()))
        )
        s = await p.fetch_aerosol(DARWIN, START, END)
        params = route.calls.last.request.url.params
    await p.aclose()
    assert params["domains"] == CAMS_DOMAIN and params["timezone"] == "UTC"
    assert len(s.steps) == 48
    assert s.steps[0].valid_time_utc == START and s.steps[0].aod550 == 0.42
    assert s.steps[0].aod550_quality is QualityFlag.OK
    assert s.units == {"aod550": "1", "dust_ug_m3": "ug/m3"}
    assert s.provenance.kind is DataKind.FORECAST
    assert s.provenance.model_name == CAMS_DOMAIN
    assert s.point.latitude_deg == -12.5  # the grid point Open-Meteo answered for


async def test_out_of_range_and_null_are_flagged_not_clamped(tmp_path: Path) -> None:
    data = json.loads(FIXTURE.read_text())
    data["hourly"]["aerosol_optical_depth"][0] = -0.1
    data["hourly"]["aerosol_optical_depth"][1] = None
    p = _provider(tmp_path)
    with respx.mock() as mock:
        mock.get(url__regex=r".*/air-quality.*").mock(return_value=httpx.Response(200, json=data))
        s = await p.fetch_aerosol(DARWIN, START, END)
    await p.aclose()
    assert (s.steps[0].aod550, s.steps[0].aod550_quality) == (None, QualityFlag.OUT_OF_RANGE)
    assert (s.steps[1].aod550, s.steps[1].aod550_quality) == (None, QualityFlag.MISSING)


async def test_api_error_is_unavailable(tmp_path: Path) -> None:
    p = _provider(tmp_path)
    with respx.mock() as mock:
        mock.get(url__regex=r".*/air-quality.*").mock(
            return_value=httpx.Response(200, json={"error": True, "reason": "bad domain"})
        )
        with pytest.raises(ProviderUnavailableError, match="bad domain"):
            await p.fetch_aerosol(DARWIN, START, END)
    await p.aclose()
