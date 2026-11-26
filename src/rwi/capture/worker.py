"""The job that builds the verification history.

Nothing in phase 2 works without this. Scores, bias corrections, calibrated
uncertainty and evidence-based model choice are all functions of a record of
*what was forecast before the fact* matched against *what was then measured*.
That record does not exist until something writes it down, every hour, for
months. This is that something.

Design rules:
  * one site or model failing must never stop the cycle - partial history is
    worth far more than none, and a provider outage is not an error in us;
  * forecasts are captured per named model, not only from the provider's
    'best_match' blend, because a score for a blend cannot tell the team which
    model to trust in central Australia;
  * what was captured and what failed is reported, never swallowed;
  * nothing here invents data: a failed fetch is a recorded failure.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from rwi.capture.sites import DEFAULT_SITES, CaptureSite
from rwi.config import Settings, get_settings
from rwi.core.provenance import DataKind
from rwi.core.timebase import TimeWindow, utc_now

logger = logging.getLogger(__name__)

CAPTURE_VARIABLES: tuple[str, ...] = (
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "pressure_msl",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "cloud_cover",
    "precipitation",
    "ghi",
    "dni",
    "dhi",
)
"""What is worth a provider call every hour for nine months.

Chosen for what the race actually needs scored: irradiance (energy), wind
(drag and stability), cloud (irradiance driver), temperature (array and battery
efficiency) and precipitation (safety). Pressure and humidity are cheap and
make the thermodynamic picture checkable.
"""

CAPTURE_MODELS: tuple[str, ...] = (
    "ecmwf_ifs025",
    "ecmwf_ifs",
    "ecmwf_aifs025_single",
    "gfs_global",
    "icon_global",
)
"""Named models captured separately. Two purposes, deliberately in one list.

'best_match' is excluded: it is a blend whose composition can change without
notice, so a score computed from it would not describe any model that exists.

VERIFIABLE (the three ECMWF entries). Probed against the live API on
2026-11-02: only ECMWF models report a model initialisation time through this
provider. Without a run time, lead time is undefined and the archive correctly
refuses to build a pair, so a forecast that lacks one can never be scored.

  | model                | reports run time |
  |----------------------|------------------|
  | ecmwf_ifs025         | yes              |
  | ecmwf_ifs            | yes              |
  | ecmwf_aifs025_single | yes              |
  | gfs_global           | NO               |
  | gfs_seamless         | NO               |
  | icon_global          | NO               |
  | icon_seamless        | NO               |

This refuted the earlier hypothesis that the cause was 'seamless' models being
stitched blends: the single-run `gfs_global` and `icon_global` lack it too.

``ecmwf_aifs025_single`` is ECMWF's AI forecast and ``ecmwf_ifs``/``ecmwf_ifs025``
are its physics-based ones, all with run times, so capturing the three together
is what makes an AI-versus-physics benchmark possible on identical lead times.
The master specification forbids claiming that AI forecasting beats physical
models without exactly that evidence.

COMPARISON ONLY (gfs_global, icon_global). These can never be scored, but
`rwi.fusion` compares models at the same valid time and needs no lead time, so
they still earn their place: genuine disagreement between forecasting centres
is the cheapest honest signal that a forecast needs a margin. The single-run
`_global` variants are used rather than `_seamless` because a seamless model is
itself a stitched blend, which is not a meaningful comparison member.
"""


class SiteOutcome(BaseModel):
    """What happened for one site in one cycle."""

    model_config = ConfigDict(frozen=True)

    site_id: str
    forecast_rows: int = 0
    observation_rows: int = 0
    observation_series: int = Field(
        default=0,
        description="observation series successfully FETCHED, which is not the same as rows "
        "written: re-recording a series the archive already holds writes nothing",
    )
    models_captured: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()

    @property
    def captured_anything(self) -> bool:
        """Whether this site contributed to the history this cycle.

        Zero rows written is a success when the archive already held the data:
        the immutability rule makes re-recording an identical series a no-op.
        Only a failure to fetch is a failure.
        """
        return (
            self.forecast_rows > 0
            or self.observation_rows > 0
            or (self.observation_series > 0 and not self.failures)
            or bool(self.models_captured)
        )


class CaptureResult(BaseModel):
    """The outcome of one capture cycle, in full."""

    model_config = ConfigDict(frozen=True)

    started_at_utc: datetime
    finished_at_utc: datetime
    outcomes: list[SiteOutcome] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return (self.finished_at_utc - self.started_at_utc).total_seconds()

    @property
    def forecast_rows(self) -> int:
        return sum(o.forecast_rows for o in self.outcomes)

    @property
    def observation_rows(self) -> int:
        return sum(o.observation_rows for o in self.outcomes)

    @property
    def failures(self) -> list[str]:
        return [f for o in self.outcomes for f in o.failures]

    @property
    def succeeded(self) -> bool:
        """True when at least one site captured something.

        A cycle in which every site failed is a failure; one in which a single
        provider was flaky is not.
        """
        return any(o.captured_anything for o in self.outcomes)

    def summary(self) -> str:
        return (
            f"captured {self.forecast_rows} forecast and {self.observation_rows} "
            f"observation rows from {len(self.outcomes)} site(s) in "
            f"{self.duration_s:.1f} s, {len(self.failures)} failure(s)"
        )


@dataclass
class GriddedCapture:
    """A gridded forecast provider (e.g. AIFS) and the state the worker owns for it.

    Gridded runs are downloaded once per init time and then interpolated to
    every site, so they are captured sequentially after the per-site work:
    concurrent sites would each start the same download. The worker owns the
    run cache and the download directory so that months of capture keep
    neither every run in memory nor every GRIB file on disk.
    """

    name: str
    provider: Any
    run_cache: dict[datetime, Any] = field(default_factory=dict)
    download_dir: Path | None = None
    keep_runs: int = 2

    def tidy(self) -> None:
        """Drop all but the newest runs from memory, and all but the newest GRIB file from disk.

        The newest file is kept so a following ``--once`` process reuses it instead of
        downloading the same run again; file names start with the run time, so the
        lexicographically last one is the newest. Interrupted downloads are removed.
        """
        for init in sorted(self.run_cache)[: -self.keep_runs or None]:
            del self.run_cache[init]
        if self.download_dir is not None and self.download_dir.is_dir():
            stale = sorted(self.download_dir.glob("*.grib2"))[:-1]
            for grib in [*stale, *self.download_dir.glob("*.part")]:
                try:
                    grib.unlink()
                except OSError as exc:
                    logger.warning("could not remove %s: %s", grib, exc)


def aifs_capture(settings: Settings, sites: Sequence[CaptureSite]) -> GriddedCapture:
    """The ECMWF AIFS open-data feed over a bbox around ``sites`` (network on use)."""
    from rwi.providers.ecmwf_aifs import AifsOpenDataSource
    from rwi.providers.gridded import BBox, GriddedProvider

    download_dir = settings.cache_dir / "aifs-capture"
    run_cache: dict[datetime, Any] = {}
    bbox = BBox.around([s.point for s in sites], settings.capture_aifs_margin_deg)
    provider = GriddedProvider(
        AifsOpenDataSource(download_dir=download_dir), bbox, settings, run_cache=run_cache
    )
    return GriddedCapture(
        name="ecmwf-aifs", provider=provider, run_cache=run_cache, download_dir=download_dir
    )


async def _capture_gridded(
    *,
    gridded: GriddedCapture,
    sites: Sequence[CaptureSite],
    archive: Any,
    variables: Sequence[str],
    forecast_hours: float,
    now: datetime,
) -> tuple[dict[str, SiteOutcome], list[str]]:
    """Capture one gridded provider at every site; one run download per cycle at most."""
    window = TimeWindow(start_utc=now, end_utc=now + timedelta(hours=forecast_hours))
    supported = gridded.provider.capabilities.variables
    wanted = [v for v in variables if v in supported]
    per_site: dict[str, SiteOutcome] = {}
    notes: list[str] = []
    any_fetched = False
    for site in sites:
        try:
            series = await gridded.provider.fetch_forecast(site.point, window, wanted)
        except Exception as exc:
            per_site[site.id] = SiteOutcome(
                site_id=site.id,
                failures=(f"{site.id}/{gridded.name}: {type(exc).__name__}: {exc}",),
            )
            if not any_fetched:
                # The run itself is unavailable; retrying per site would only
                # repeat the same download.
                notes.append(
                    f"{gridded.name}: no run this cycle, remaining sites not attempted"
                )
                break
            continue
        any_fetched = True
        label = f"{gridded.name}/{series.model_name or series.dataset}"
        try:
            rows = archive.record_series(series)
        except Exception as exc:
            per_site[site.id] = SiteOutcome(
                site_id=site.id,
                failures=(f"{site.id}/{gridded.name}: archive: {type(exc).__name__}: {exc}",),
            )
            continue
        per_site[site.id] = SiteOutcome(
            site_id=site.id, forecast_rows=rows, models_captured=(label,)
        )
    gridded.tidy()
    return per_site, notes


def _merge(a: SiteOutcome, b: SiteOutcome) -> SiteOutcome:
    return a.model_copy(
        update={
            "forecast_rows": a.forecast_rows + b.forecast_rows,
            "models_captured": a.models_captured + b.models_captured,
            "failures": a.failures + b.failures,
        }
    )


async def _capture_site(
    *,
    site: CaptureSite,
    archive: Any,
    providers: dict[str, Any],
    variables: Sequence[str],
    models: Sequence[str],
    forecast_hours: float,
    now: datetime,
    semaphore: asyncio.Semaphore,
) -> SiteOutcome:
    forecast_rows = 0
    observation_rows = 0
    observation_series = 0
    captured: list[str] = []
    failures: list[str] = []
    window = TimeWindow(start_utc=now, end_utc=now + timedelta(hours=forecast_hours))

    openmeteo = providers.get("open-meteo")
    if openmeteo is not None:
        for model in models:
            async with semaphore:
                try:
                    series = await openmeteo.fetch_forecast(
                        site.point, window, variables, models=model
                    )
                except Exception as exc:
                    failures.append(f"{site.id}/{model}: {type(exc).__name__}: {exc}")
                    continue
            try:
                forecast_rows += archive.record_series(series)
                captured.append(model)
            except Exception as exc:
                failures.append(f"{site.id}/{model}: archive: {type(exc).__name__}: {exc}")

    # Observations: ask each provider only for what it actually measures, so a
    # provider is never blamed for lacking a variable it never claimed.
    observation_window = TimeWindow(start_utc=now - timedelta(hours=6), end_utc=now)
    for name, provider in providers.items():
        capabilities = getattr(provider, "capabilities", None)
        if capabilities is None or not capabilities.supports_observations:
            continue
        measurable = [v for v in variables if v in capabilities.variables]
        if not measurable:
            continue
        async with semaphore:
            try:
                series = await provider.fetch_observations(
                    site.point, observation_window, measurable
                )
            except Exception as exc:
                failures.append(f"{site.id}/{name} observations: {type(exc).__name__}: {exc}")
                continue
        if series.kind is not DataKind.OBSERVATION:
            failures.append(
                f"{site.id}/{name}: provider returned {series.kind.value}, not an observation; "
                "not archived"
            )
            continue
        observation_series += 1
        try:
            observation_rows += archive.record_series(series)
        except Exception as exc:
            failures.append(f"{site.id}/{name} observations: archive: {type(exc).__name__}: {exc}")

    return SiteOutcome(
        site_id=site.id,
        forecast_rows=forecast_rows,
        observation_rows=observation_rows,
        observation_series=observation_series,
        models_captured=tuple(captured),
        failures=tuple(failures),
    )


async def capture_once(
    *,
    archive: Any,
    providers: dict[str, Any],
    sites: Sequence[CaptureSite] = DEFAULT_SITES,
    variables: Sequence[str] = CAPTURE_VARIABLES,
    models: Sequence[str] = CAPTURE_MODELS,
    forecast_hours: float = 48.0,
    now: datetime | None = None,
    max_concurrency: int = 3,
    gridded: Sequence[GriddedCapture] = (),
) -> CaptureResult:
    """Run one capture cycle across every site. Never raises on a partial failure."""
    started = now or utc_now()
    semaphore = asyncio.Semaphore(max_concurrency)
    outcomes = await asyncio.gather(
        *(
            _capture_site(
                site=site,
                archive=archive,
                providers=providers,
                variables=variables,
                models=models,
                forecast_hours=forecast_hours,
                now=started,
                semaphore=semaphore,
            )
            for site in sites
        )
    )
    outcomes = list(outcomes)
    notes: list[str] = []
    for g in gridded:
        per_site, g_notes = await _capture_gridded(
            gridded=g,
            sites=sites,
            archive=archive,
            variables=variables,
            forecast_hours=forecast_hours,
            now=started,
        )
        notes.extend(g_notes)
        outcomes = [_merge(o, per_site[o.site_id]) if o.site_id in per_site else o
                    for o in outcomes]
    if not any(o.observation_series for o in outcomes):
        notes.append(
            "no observation source answered this cycle: without measurements no forecast can "
            "ever be scored"
        )
    elif not any(o.observation_rows for o in outcomes):
        notes.append(
            "observations were fetched but wrote no new rows: the archive already held them "
            "(this is the immutability rule working, not a failure)"
        )
    result = CaptureResult(
        started_at_utc=started,
        finished_at_utc=utc_now(),
        outcomes=outcomes,
        notes=notes,
    )
    logger.info("capture cycle: %s", result.summary())
    for failure in result.failures:
        logger.warning("capture failure: %s", failure)
    return result


def enforce_retention(archive: Any, settings: Settings | None = None) -> int:
    """Apply ADR 0007's retention policy. Returns the number of rows removed.

    The archive implements `prune` but calls it for nobody, so without this the
    size limit in Settings is a number that documents an intention rather than
    enforcing one. Pruning only happens once the archive is actually over
    `archive_max_bytes`: deleting history that costs nothing to keep would
    throw away evidence for no reason.

    Never raises. A retention failure must not end a capture run - the worst
    case is an archive that keeps growing, which is recoverable, while a
    crashed worker loses history that cannot be recovered at all.
    """
    config = settings or get_settings()
    try:
        cutoff = utc_now() - timedelta(days=config.archive_retention_days)
        removed = archive.prune(
            older_than_utc=cutoff, only_above_bytes=config.archive_max_bytes
        )
    except Exception as exc:
        logger.warning("retention pass failed, archive keeps growing: %s", exc)
        return 0
    if removed:
        logger.info(
            "retention: removed %d row(s) older than %s (archive was above %d bytes)",
            removed,
            cutoff.date().isoformat(),
            config.archive_max_bytes,
        )
    return removed


async def run_forever(
    *,
    archive: Any,
    providers: dict[str, Any],
    settings: Settings | None = None,
    sites: Sequence[CaptureSite] = DEFAULT_SITES,
    stop: asyncio.Event | None = None,
    gridded: Sequence[GriddedCapture] = (),
) -> None:
    """Capture on a fixed interval until stopped.

    Jitter spreads load so that several instances do not hit the provider in
    the same second. Consecutive total failures back off exponentially and,
    past a configured limit, the worker stops rather than hammering a provider
    that is clearly down - a capture gap is recoverable, a rate-limit ban is
    not.
    """
    config = settings or get_settings()
    stop_event = stop or asyncio.Event()
    consecutive_failures = 0

    while not stop_event.is_set():
        result = await capture_once(
            archive=archive,
            providers=providers,
            sites=sites,
            forecast_hours=config.capture_forecast_hours,
            gridded=gridded,
        )
        enforce_retention(archive, config)
        consecutive_failures = 0 if result.succeeded else consecutive_failures + 1
        if consecutive_failures >= config.capture_max_failures:
            logger.error(
                "capture worker stopping after %d consecutive failed cycles; "
                "the verification history has a gap from now on",
                consecutive_failures,
            )
            return
        delay = config.capture_interval_s * (2 ** min(consecutive_failures, 5))
        delay += random.uniform(0, config.capture_jitter_s)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except TimeoutError:
            continue
