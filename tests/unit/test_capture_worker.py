"""The capture worker: partial failure must never cost a whole cycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from rwi.capture import CAPTURE_VARIABLES, capture_once, sites_from_ids
from rwi.capture.sites import DEFAULT_SITES, SITES_BY_ID
from rwi.contracts import WeatherSeries, WeatherStep
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance, measured

NOW = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
POINT = GeoPoint(latitude_deg=-23.698, longitude_deg=133.8807, elevation_m=545.0)


def _series(
    kind: DataKind, *, provider: str, model: str | None = None, steps: int = 3
) -> WeatherSeries:
    out = []
    for i in range(steps):
        valid = NOW + timedelta(hours=i)
        prov = Provenance(
            provider=provider,
            dataset="forecast" if kind is DataKind.FORECAST else "obs",
            kind=kind,
            retrieved_at_utc=NOW,
            valid_time_utc=valid if kind is DataKind.FORECAST else None,
            observation_time_utc=valid if kind is DataKind.OBSERVATION else None,
            model_name=model,
        )
        out.append(
            WeatherStep(
                valid_time_utc=valid,
                values={"temperature_2m": measured("temperature_2m", 20.0 + i, prov)},
            )
        )
    return WeatherSeries(
        point=POINT,
        provider=provider,
        dataset="forecast" if kind is DataKind.FORECAST else "obs",
        kind=kind,
        model_name=model,
        retrieved_at_utc=NOW,
        steps=out,
    )


class FakeArchive:
    """Counts rows the way the real archive does, without a database."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.recorded: list[WeatherSeries] = []
        self.fail_on = fail_on

    def record_series(self, series: WeatherSeries) -> int:
        if self.fail_on and series.model_name == self.fail_on:
            raise RuntimeError("archive is broken for this one")
        self.recorded.append(series)
        return sum(len(step.values) for step in series.steps)


class FakeCapabilities:
    def __init__(self, *, observations: bool, variables: frozenset[str]) -> None:
        self.supports_observations = observations
        self.variables = variables


class FakeForecastProvider:
    """Stands in for Open-Meteo: forecasts per named model, no observations."""

    def __init__(self, *, fail_models: set[str] | None = None) -> None:
        self.capabilities = FakeCapabilities(
            observations=False, variables=frozenset(CAPTURE_VARIABLES)
        )
        self.fail_models = fail_models or set()
        self.calls: list[tuple[str, str | None]] = []

    async def fetch_forecast(
        self, point: GeoPoint, window: Any, variables: Any, *, models: str | None = None
    ) -> WeatherSeries:
        self.calls.append((f"{point.latitude_deg:.3f}", models))
        if models in self.fail_models:
            raise ConnectionError(f"{models} unavailable")
        return _series(DataKind.FORECAST, provider="open-meteo", model=models)


class FakeObservationProvider:
    """Stands in for BoM: measures a few variables, no forecasts."""

    def __init__(self, *, kind: DataKind = DataKind.OBSERVATION, fail: bool = False) -> None:
        self.capabilities = FakeCapabilities(
            observations=True, variables=frozenset({"temperature_2m", "wind_speed_10m"})
        )
        self.kind = kind
        self.fail = fail
        self.requested: list[tuple[str, ...]] = []

    async def fetch_observations(
        self, point: GeoPoint, window: Any, variables: Any
    ) -> WeatherSeries:
        self.requested.append(tuple(variables))
        if self.fail:
            raise TimeoutError("station unreachable")
        return _series(self.kind, provider="bom")


async def test_a_cycle_captures_every_model_and_the_observations() -> None:
    archive = FakeArchive()
    forecasts = FakeForecastProvider()
    observations = FakeObservationProvider()
    result = await capture_once(
        archive=archive,
        providers={"open-meteo": forecasts, "bom": observations},
        sites=sites_from_ids(["alice-springs"]),
        models=("ecmwf_ifs025", "gfs_seamless"),
        now=NOW,
    )
    assert result.succeeded
    assert result.forecast_rows == 6  # 2 models x 3 steps x 1 variable
    assert result.observation_rows == 3
    assert result.outcomes[0].models_captured == ("ecmwf_ifs025", "gfs_seamless")
    assert result.failures == []
    assert "captured 6 forecast and 3 observation rows" in result.summary()


async def test_one_failing_model_does_not_cost_the_cycle() -> None:
    """A provider outage is not an error in us, and partial history beats none."""
    archive = FakeArchive()
    forecasts = FakeForecastProvider(fail_models={"gfs_seamless"})
    result = await capture_once(
        archive=archive,
        providers={"open-meteo": forecasts},
        sites=sites_from_ids(["darwin"]),
        models=("ecmwf_ifs025", "gfs_seamless", "icon_seamless"),
        now=NOW,
    )
    assert result.succeeded
    assert result.outcomes[0].models_captured == ("ecmwf_ifs025", "icon_seamless")
    assert len(result.failures) == 1
    assert "gfs_seamless" in result.failures[0]
    assert "ConnectionError" in result.failures[0]


async def test_an_archive_failure_is_reported_not_raised() -> None:
    archive = FakeArchive(fail_on="ecmwf_ifs025")
    result = await capture_once(
        archive=archive,
        providers={"open-meteo": FakeForecastProvider()},
        sites=sites_from_ids(["darwin"]),
        models=("ecmwf_ifs025", "gfs_seamless"),
        now=NOW,
    )
    assert "archive" in result.failures[0]
    assert result.outcomes[0].models_captured == ("gfs_seamless",)


async def test_providers_are_asked_only_for_what_they_measure() -> None:
    """BoM measures neither irradiance nor precipitation; asking would produce a
    failure that is our fault, not the provider's."""
    observations = FakeObservationProvider()
    await capture_once(
        archive=FakeArchive(),
        providers={"bom": observations},
        sites=sites_from_ids(["darwin"]),
        now=NOW,
    )
    assert observations.requested == [("temperature_2m", "wind_speed_10m")]


async def test_a_provider_returning_model_output_for_observations_is_refused() -> None:
    """The platform's central rule, enforced at the archive boundary: an
    analysis is not a measurement and must never enter the verification history
    as one."""
    archive = FakeArchive()
    liar = FakeObservationProvider(kind=DataKind.ANALYSIS)
    result = await capture_once(
        archive=archive,
        providers={"bom": liar},
        sites=sites_from_ids(["darwin"]),
        now=NOW,
    )
    assert result.observation_rows == 0
    assert archive.recorded == []
    assert any("not an observation" in f for f in result.failures)


async def test_a_cycle_with_no_observation_source_says_so() -> None:
    """Forecasts alone can never be scored, so a cycle where no measurement
    source answered must say so."""
    result = await capture_once(
        archive=FakeArchive(),
        providers={"open-meteo": FakeForecastProvider()},
        sites=sites_from_ids(["darwin"]),
        models=("ecmwf_ifs025",),
        now=NOW,
    )
    assert any("no observation source answered" in n for n in result.notes)


async def test_observations_already_held_are_not_reported_as_missing() -> None:
    """The archive refuses to duplicate a series it already holds, so a cycle
    can legitimately write zero rows. Calling that "no observations" would tell
    an operator the history has a gap when it does not."""

    class AlreadyHeld(FakeArchive):
        def record_series(self, series: WeatherSeries) -> int:
            return 0

    result = await capture_once(
        archive=AlreadyHeld(),
        providers={"bom": FakeObservationProvider()},
        sites=sites_from_ids(["darwin"]),
        now=NOW,
    )
    assert result.observation_rows == 0
    assert result.outcomes[0].observation_series == 1
    assert result.succeeded
    assert any("already held them" in n for n in result.notes)
    assert not any("no observation source" in n for n in result.notes)


async def test_a_cycle_where_everything_failed_is_not_a_success() -> None:
    result = await capture_once(
        archive=FakeArchive(),
        providers={
            "open-meteo": FakeForecastProvider(fail_models={"ecmwf_ifs025"}),
            "bom": FakeObservationProvider(fail=True),
        },
        sites=sites_from_ids(["darwin"]),
        models=("ecmwf_ifs025",),
        now=NOW,
    )
    assert not result.succeeded
    assert len(result.failures) == 2


async def test_every_site_is_attempted_even_when_one_fails() -> None:
    forecasts = FakeForecastProvider()
    result = await capture_once(
        archive=FakeArchive(fail_on="ecmwf_ifs025"),
        providers={"open-meteo": forecasts},
        sites=DEFAULT_SITES,
        models=("ecmwf_ifs025",),
        now=NOW,
    )
    assert len(result.outcomes) == len(DEFAULT_SITES)
    assert {o.site_id for o in result.outcomes} == set(SITES_BY_ID)


def test_sites_are_on_the_route_and_ordered_from_darwin() -> None:
    distances = [s.route_distance_m for s in DEFAULT_SITES]
    assert all(d is not None for d in distances)
    assert distances == sorted(distances)  # type: ignore[type-var]
    assert DEFAULT_SITES[0].id == "darwin"
    assert DEFAULT_SITES[-1].id == "adelaide"
    assert all(s.rationale for s in DEFAULT_SITES)


def test_an_unknown_site_id_fails_loudly() -> None:
    with pytest.raises(ValueError, match="unknown capture site"):
        sites_from_ids(["atlantis"])
    assert sites_from_ids(None) == DEFAULT_SITES


def test_the_blended_model_is_not_captured() -> None:
    """'best_match' is a blend whose composition can change without notice, so a
    score computed from it would not mean anything."""
    from rwi.capture.worker import CAPTURE_MODELS

    assert "best_match" not in CAPTURE_MODELS
    assert len(CAPTURE_MODELS) >= 2


class PruningArchive(FakeArchive):
    """Records how retention was asked for, without implementing a database."""

    def __init__(self, *, removed: int = 0, fail: bool = False) -> None:
        super().__init__()
        self.removed = removed
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def prune(self, *, older_than_utc: Any, only_above_bytes: Any = None) -> int:
        self.calls.append(
            {"older_than_utc": older_than_utc, "only_above_bytes": only_above_bytes}
        )
        if self.fail:
            raise RuntimeError("disk is unhappy")
        return self.removed


def test_retention_passes_the_configured_limits() -> None:
    """ADR 0007's size limit is only a policy if something enforces it."""
    from rwi.capture import enforce_retention
    from rwi.config import get_settings

    archive = PruningArchive(removed=17)
    settings = get_settings()
    assert enforce_retention(archive, settings) == 17
    call = archive.calls[0]
    assert call["only_above_bytes"] == settings.archive_max_bytes
    age_days = (datetime.now(UTC) - call["older_than_utc"]).days
    assert age_days == pytest.approx(settings.archive_retention_days, abs=1)


def test_a_retention_failure_never_ends_a_capture_run() -> None:
    """An archive that keeps growing is recoverable; a crashed worker loses
    history that cannot be recovered at all."""
    from rwi.capture import enforce_retention

    assert enforce_retention(PruningArchive(fail=True)) == 0


def test_retention_is_not_requested_below_the_size_limit() -> None:
    """Deleting history that costs nothing to keep would discard evidence for
    no reason, so the size gate is always passed down."""
    from rwi.capture import enforce_retention

    archive = PruningArchive()
    enforce_retention(archive)
    assert archive.calls[0]["only_above_bytes"] is not None


# -- gridded (AIFS) capture --------------------------------------------------


class FakeGriddedSource:
    """A GriddedSource that builds a synthetic run instead of downloading one."""

    provider = "ecmwf"
    dataset = "fake-aifs"
    fields = frozenset({"temperature_2m"})
    max_forecast_hours = 60.0
    spatial_resolution_km = 28.0
    license = "test"
    documentation_url = "https://example.invalid"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.downloads = 0

    def latest_init_time(self, now: datetime) -> datetime:
        return now - timedelta(hours=6)

    async def fetch_run(
        self, init_time: datetime, steps_h: Any, fields: Any, bbox: Any
    ) -> Any:
        import numpy as np

        from rwi.providers.gridded import GriddedRun

        self.downloads += 1
        if self.fail:
            raise ConnectionError("open-data server unreachable")
        lats = np.arange(-36.0, -9.0, 1.0)
        lons = np.arange(128.0, 141.0, 1.0)
        times = tuple(init_time + timedelta(hours=h) for h in range(0, 61, 6))
        return GriddedRun(
            provider=self.provider,
            dataset=self.dataset,
            kind=DataKind.SIMULATION,
            model_name="fake-aifs",
            model_version="1",
            init_time_utc=init_time,
            retrieved_at_utc=NOW,
            valid_times_utc=times,
            latitudes=lats,
            longitudes=lons,
            fields={"temperature_2m": np.full((len(times), lats.size, lons.size), 25.0)},
            spatial_resolution_km=self.spatial_resolution_km,
            license=self.license,
            source_url=None,
        )


def _gridded(source: FakeGriddedSource, tmp_path: Any) -> Any:
    from rwi.capture.worker import GriddedCapture
    from rwi.config import Settings
    from rwi.providers.gridded import BBox, GriddedProvider

    cache: dict[datetime, Any] = {}
    provider = GriddedProvider(
        source,
        BBox(south=-35.0, north=-10.0, west=129.0, east=139.0),
        Settings(_env_file=None, cache_dir=tmp_path),
        run_cache=cache,
        clock=lambda: NOW,
    )
    return GriddedCapture(name="ecmwf-aifs", provider=provider, run_cache=cache)


async def test_a_gridded_run_is_downloaded_once_and_archived_at_every_site(tmp_path: Any) -> None:
    archive = FakeArchive()
    source = FakeGriddedSource()
    sites = sites_from_ids(["tennant-creek", "alice-springs"])
    result = await capture_once(
        archive=archive,
        providers={"open-meteo": FakeForecastProvider()},
        sites=sites,
        models=("ecmwf_ifs025",),
        now=NOW,
        gridded=[_gridded(source, tmp_path)],
    )
    assert source.downloads == 1
    assert result.failures == []
    for outcome in result.outcomes:
        assert outcome.models_captured == ("ecmwf_ifs025", "ecmwf-aifs/fake-aifs")
    aifs = [s for s in archive.recorded if s.model_name == "fake-aifs"]
    assert len(aifs) == 2
    # Only what the source carries is asked for; the rest of CAPTURE_VARIABLES
    # is not a failure of the gridded provider.
    assert all(set(step.values) == {"temperature_2m"} for s in aifs for step in s.steps)
    assert all(s.kind is not DataKind.OBSERVATION for s in aifs)


async def test_an_unavailable_gridded_run_is_one_failure_not_a_lost_cycle(tmp_path: Any) -> None:
    source = FakeGriddedSource(fail=True)
    result = await capture_once(
        archive=FakeArchive(),
        providers={"open-meteo": FakeForecastProvider()},
        sites=sites_from_ids(["tennant-creek", "alice-springs"]),
        models=("ecmwf_ifs025",),
        now=NOW,
        gridded=[_gridded(source, tmp_path)],
    )
    assert source.downloads == 1
    assert result.succeeded
    assert len(result.failures) == 1 and "ecmwf-aifs" in result.failures[0]
    assert any("no run this cycle" in n for n in result.notes)


def test_tidy_keeps_only_the_newest_runs_and_drops_grib_files(tmp_path: Any) -> None:
    from rwi.capture.worker import GriddedCapture

    (tmp_path / "aifs_20270823T00Z.grib2").write_bytes(b"x")
    cache = {NOW - timedelta(hours=6 * i): object() for i in range(4)}
    g = GriddedCapture(name="x", provider=None, run_cache=cache, download_dir=tmp_path)
    g.tidy()
    assert sorted(cache) == [NOW - timedelta(hours=6), NOW]
    assert not list(tmp_path.glob("*.grib2"))


def test_aifs_capture_covers_every_site_without_touching_the_network(tmp_path: Any) -> None:
    from rwi.capture.worker import aifs_capture
    from rwi.config import Settings

    settings = Settings(_env_file=None, cache_dir=tmp_path)
    assert settings.capture_aifs is False
    g = aifs_capture(settings, DEFAULT_SITES)
    box = g.provider.bbox
    for s in DEFAULT_SITES:
        assert box.south < s.point.latitude_deg < box.north
        assert box.west < s.point.longitude_deg < box.east
    assert g.download_dir == tmp_path / "aifs-capture"
