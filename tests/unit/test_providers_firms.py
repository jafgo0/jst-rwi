"""NASA FIRMS adapter: CSV parsing, confidence mapping, key masking (ADR 0014)."""

from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from rwi.config import Settings
from rwi.core.errors import ProviderError, ProviderUnavailableError
from rwi.core.provenance import DataKind, QualityFlag
from rwi.providers.firms import FirmsProvider, firms_missing_config, parse_firms_csv
from rwi.providers.gridded import BBox

FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "firms" / "j1_viirs_c2_aus_nz_24h_route_trimmed.csv"
)
# A made-up test key, never a real MAP_KEY.
KEY = "0123456789abcdef0123456789abcdef"
BOX = BBox(west=129.0, south=-36.0, east=140.0, north=-11.0)


def _settings(tmp_path: Path, **kw: object) -> Settings:
    base: dict[str, object] = {
        "cache_dir": tmp_path,
        "http_retries": 0,
        "firms_map_key": SecretStr(KEY),
        "firms_source": "VIIRS_NOAA21_NRT",
        "firms_day_range": 1,
        "firms_route_buffer_km": 50.0,
    }
    base.update(kw)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def test_fixture_parses_every_row_as_viirs_unvalidated() -> None:
    hotspots, rejected = parse_firms_csv(FIXTURE.read_text())
    assert rejected == 0
    assert len(hotspots) == 52
    assert {h.instrument for h in hotspots} == {"VIIRS"}
    assert {h.quality for h in hotspots} == {QualityFlag.UNVALIDATED}
    assert all(h.acquired_utc.tzinfo is not None for h in hotspots)
    assert {h.confidence for h in hotspots} <= {"low", "nominal", "high"}


def test_viirs_letters_and_modis_percent_map_to_classes() -> None:
    viirs = (
        "latitude,longitude,bright_ti4,acq_date,acq_time,satellite,confidence,frp,daynight\n"
        "-20,134,330,2026-11-26,0238,N21,l,1.5,D\n"
        "-20,134,330,2026-11-26,0238,N21,h,1.5,D\n"
    )
    # MODIS C6 classes (Giglio et al., MODIS C6 fire user guide): <30 low, 30-79 nominal, >=80 high
    modis = (
        "latitude,longitude,brightness,acq_date,acq_time,satellite,confidence,frp,daynight\n"
        "-20,134,320,2026-11-26,0100,Aqua,29,4.0,N\n"
        "-20,134,320,2026-11-26,0100,Aqua,30,4.0,N\n"
        "-20,134,320,2026-11-26,0100,Aqua,80,4.0,N\n"
        "-20,134,320,2026-11-26,0100,Aqua,bogus,4.0,N\n"
    )
    v, v_rej = parse_firms_csv(viirs)
    m, m_rej = parse_firms_csv(modis)
    assert [h.confidence for h in v] == ["low", "high"] and v_rej == 0
    assert [h.confidence for h in m] == ["low", "nominal", "high"] and m_rej == 1
    assert {h.instrument for h in m} == {"MODIS"}


def test_missing_columns_raise() -> None:
    with pytest.raises(ProviderUnavailableError, match="lacks columns"):
        parse_firms_csv("latitude,longitude\n1,2\n")


def test_missing_config_lists_names_and_never_defaults(tmp_path: Path) -> None:
    s = Settings(_env_file=None, cache_dir=tmp_path)  # type: ignore[call-arg]
    assert set(firms_missing_config(s)) == {
        "RWI_FIRMS_MAP_KEY",
        "RWI_FIRMS_SOURCE",
        "RWI_FIRMS_DAY_RANGE",
        "RWI_FIRMS_ROUTE_BUFFER_KM",
    }
    assert firms_missing_config(_settings(tmp_path)) == []


async def test_fetch_puts_key_only_in_the_request(tmp_path: Path) -> None:
    provider = FirmsProvider(_settings(tmp_path))
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(url__regex=r".*/api/area/csv/.*").mock(
            return_value=httpx.Response(200, text=FIXTURE.read_text())
        )
        hs = await provider.fetch_hotspots(BOX)
        assert KEY in str(route.calls.last.request.url)
    await provider.aclose()
    assert len(hs.hotspots) == 52
    assert hs.provenance.kind is DataKind.OBSERVATION
    assert hs.provenance.observation_time_utc == max(h.acquired_utc for h in hs.hotspots)
    dumped = hs.model_dump_json()
    assert KEY not in dumped
    assert "/***/VIIRS_NOAA21_NRT/" in (hs.provenance.source_url or "")
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert KEY not in f.read_text(errors="ignore"), f"key leaked into cache {f}"


@pytest.mark.parametrize(
    ("status", "body"),
    [(401, f"Invalid MAP_KEY {KEY}"), (503, f"down for {KEY}"), (200, "Invalid MAP_KEY.")],
)
async def test_errors_never_contain_the_key(tmp_path: Path, status: int, body: str) -> None:
    provider = FirmsProvider(_settings(tmp_path))
    with respx.mock() as mock:
        mock.get(url__regex=r".*/api/area/csv/.*").mock(
            return_value=httpx.Response(status, text=body)
        )
        with pytest.raises(ProviderError) as err:
            await provider.fetch_hotspots(BOX)
    await provider.aclose()
    assert KEY not in str(err.value)
    assert "***" in str(err.value) or status == 200


async def test_day_range_outside_api_limit_is_refused(tmp_path: Path) -> None:
    provider = FirmsProvider(_settings(tmp_path, firms_day_range=6))
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__regex=r".*").mock(return_value=httpx.Response(200))
        with pytest.raises(ValueError, match=r"1\.\.5"):
            await provider.fetch_hotspots(BOX)
        assert not route.called
    await provider.aclose()
