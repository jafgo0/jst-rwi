"""Fetch ERA5 race weeks through the Open-Meteo archive and build the climatology (ADR 0016).

The build is resumable: every point-year series is saved as a gzipped JSON part
as soon as it arrives, and a rerun reuses the parts instead of fetching again.
A point-year that still fails after its retries is recorded and the build goes on;
the climatology itself is only produced once every part exists.
"""

import asyncio
import gzip
import logging
import zlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from rwi.climatology.raceweek import (
    RACE_WEEK_DAYS,
    VARIABLES,
    RaceWeekClimatology,
    SamplePoint,
    control_stop_segments,
    race_week_window_utc,
    sample_points,
    segment_climatology,
)
from rwi.contracts import Route, WeatherSeries
from rwi.core.errors import CapabilityNotSupportedError, ProviderError, ProviderUnavailableError
from rwi.core.timebase import TimeWindow, utc_now
from rwi.providers.openmeteo import OpenMeteoProvider
from rwi.regulations import EVENT_TIMEZONE, SCHEDULE

ERA5_MODEL = "era5"
_POINT_TOLERANCE_DEG = 1e-4
_PROGRESS_EVERY = 20

log = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]


class IncompleteClimatologyError(ProviderUnavailableError):
    """Some point-years are still missing; the parts already fetched are kept for a rerun."""

    def __init__(self, failures: list[str], *, total: int, aborted: bool) -> None:
        self.failures = failures
        self.total = total
        self.aborted = aborted
        head = "; ".join(failures[:5]) + (" ..." if len(failures) > 5 else "")
        why = "stopped after consecutive failures" if aborted else "finished with gaps"
        super().__init__(
            f"climatology build {why}: {len(failures)} of {total} point-years missing ({head}). "
            "Rerun the same command to resume; fetched parts are kept."
        )


def climatology_path(directory: Path, route_id: str) -> Path:
    """Where the built race-week climatology for ``route_id`` lives."""
    return directory / f"{route_id}-race-week.json"


def part_path(parts_dir: Path, route_id: str, year: int, sp: SamplePoint) -> Path:
    """Where the ERA5 race-week series of one sample point and year is checkpointed."""
    lat, lon = sp.point.latitude_deg, sp.point.longitude_deg
    return parts_dir / route_id / ERA5_MODEL / str(year) / f"{lat:+.4f}_{lon:+.4f}.json.gz"


def _load_part(path: Path, sp: SamplePoint, year: int) -> WeatherSeries | None:
    """A saved part if it is readable and matches this point-year, else None (refetch)."""
    if not path.exists():
        return None
    try:
        s = WeatherSeries.model_validate_json(gzip.decompress(path.read_bytes()))
    except (OSError, EOFError, zlib.error, ValidationError, ValueError) as exc:
        log.warning("unreadable part %s (%s); fetching again", path, exc)
        return None
    start, end = race_week_window_utc(year)
    asked = s.requested_point
    problems = [
        s.model_name != ERA5_MODEL and f"model {s.model_name!r}",
        not s.steps and "no steps",
        bool(s.steps) and not set(VARIABLES) <= s.steps[0].values.keys() and "missing variables",
        bool(s.steps)
        and (
            s.steps[0].valid_time_utc > start + timedelta(hours=1)
            or s.steps[-1].valid_time_utc < end - timedelta(hours=1)
        )
        and "does not cover the race week",
        asked is not None
        and (
            abs(asked.latitude_deg - sp.point.latitude_deg) > _POINT_TOLERANCE_DEG
            or abs(asked.longitude_deg - sp.point.longitude_deg) > _POINT_TOLERANCE_DEG
        )
        and "other point",
    ]
    bad = [p for p in problems if p]
    if bad:
        log.warning("part %s does not match (%s); fetching again", path, ", ".join(map(str, bad)))
        return None
    return s


def _save_part(path: Path, series: WeatherSeries) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(gzip.compress(series.model_dump_json().encode("utf-8")))
    tmp.replace(path)


async def _fetch_with_retry(
    provider: OpenMeteoProvider,
    sp: SamplePoint,
    year: int,
    *,
    attempts: int,
    backoff_s: float,
    sleep: Sleep,
) -> WeatherSeries:
    """ERA5 series for one point-year, retrying provider errors with exponential backoff.

    Each attempt already includes the provider's own HTTP retries; this layer rides
    out longer outages and rate limits. A capability error is a configuration bug and
    is never retried.
    """
    start, end = race_week_window_utc(year)
    window = TimeWindow(start_utc=start, end_utc=end)
    for attempt in range(1, attempts + 1):
        try:
            return await provider.fetch_analysis(sp.point, window, VARIABLES, models=ERA5_MODEL)
        except CapabilityNotSupportedError:
            raise
        except ProviderError as exc:
            if attempt == attempts:
                raise
            wait = backoff_s * 2 ** (attempt - 1)
            log.warning(
                "km %.1f %d: attempt %d/%d failed (%s); retrying in %.0f s",
                sp.distance_km,
                year,
                attempt,
                attempts,
                exc,
                wait,
            )
            await sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


async def build_race_week_climatology(
    provider: OpenMeteoProvider,
    route: Route,
    *,
    first_year: int,
    last_year: int,
    spacing_km: float,
    delay_s: float,
    parts_dir: Path,
    attempts: int = 5,
    backoff_s: float = 30.0,
    max_consecutive_failures: int = 3,
    now: datetime | None = None,
    sleep: Sleep = asyncio.sleep,
) -> RaceWeekClimatology:
    """ERA5 22-29 Aug climatology per control-stop segment (``DataKind.CLIMATOLOGY``).

    One archive request per point per year, checkpointed under ``parts_dir``. Raises
    `IncompleteClimatologyError` when any point-year is still missing: a partial
    climatology is never returned. ``max_consecutive_failures`` failed point-years in a
    row stop the run early, since the provider is then down or the quota is spent.
    """
    if provider.radiation_mode != "preceding_hour_mean":
        raise ValueError("climatology daily energy needs preceding-hour mean radiation")
    if first_year > last_year:
        raise ValueError(f"first_year {first_year} is after last_year {last_year}")
    if delay_s < 0.0:
        raise ValueError(f"delay_s must be >= 0, got {delay_s!r}")
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts!r}")
    if backoff_s < 0.0:
        raise ValueError(f"backoff_s must be >= 0, got {backoff_s!r}")
    if max_consecutive_failures < 1:
        raise ValueError(f"max_consecutive_failures must be >= 1, got {max_consecutive_failures!r}")
    built = now or utc_now()
    years = list(range(first_year, last_year + 1))
    if race_week_window_utc(last_year)[1] > built:
        raise ValueError(f"race week {last_year} is not over yet; ERA5 has no data for it")

    segments = control_stop_segments(route.total_distance_m / 1000.0)
    plan = [(segment, sample_points(route, segment, spacing_km)) for segment in segments]
    total = sum(len(points) for _, points in plan) * len(years)
    done = fetched = reused = 0
    consecutive = 0
    failures: list[str] = []
    out = []
    for segment, points in plan:
        series: list[tuple[int, int, WeatherSeries]] = []
        for idx, sp in enumerate(points):
            for year in years:
                done += 1
                path = part_path(parts_dir, route.id, year, sp)
                s = _load_part(path, sp, year)
                if s is not None:
                    reused += 1
                else:
                    try:
                        s = await _fetch_with_retry(
                            provider, sp, year, attempts=attempts, backoff_s=backoff_s, sleep=sleep
                        )
                    except CapabilityNotSupportedError:
                        raise
                    except ProviderError as exc:
                        failures.append(
                            f"{segment.start_name}-{segment.end_name} km {sp.distance_km:.1f} "
                            f"{year}: {exc}"
                        )
                        log.error("km %.1f %d: giving up (%s)", sp.distance_km, year, exc)
                        consecutive += 1
                        if consecutive >= max_consecutive_failures:
                            raise IncompleteClimatologyError(
                                failures, total=total, aborted=True
                            ) from exc
                        continue
                    consecutive = 0
                    fetched += 1
                    _save_part(path, s)
                    if delay_s:
                        await sleep(delay_s)
                series.append((idx, year, s))
                if done % _PROGRESS_EVERY == 0 or done == total:
                    log.info(
                        "%d/%d point-years (%d fetched, %d from parts, %d failed)",
                        done,
                        total,
                        fetched,
                        reused,
                        len(failures),
                    )
        if not failures:
            out.append(segment_climatology(segment, points, series, retrieved_at_utc=built))
    if failures:
        raise IncompleteClimatologyError(failures, total=total, aborted=False)

    first, last = RACE_WEEK_DAYS
    return RaceWeekClimatology(
        route_id=route.id,
        provider="open-meteo",
        dataset="archive",
        model_name=ERA5_MODEL,
        years=years,
        race_week_event_dates=f"Aug {first:02d}-{last:02d} ({EVENT_TIMEZONE.tzname(None)})",
        driving_window_event=(
            f"{SCHEDULE.daily_start:%H:%M}-{SCHEDULE.daily_stop:%H:%M} "
            f"({EVENT_TIMEZONE.tzname(None)})"
        ),
        spacing_km=spacing_km,
        built_at_utc=built,
        notes=[
            "ERA5 reanalysis via Open-Meteo archive: model output, not observations.",
            "Segments use the 2025 control stops; the 2027 stops may differ.",
            "Hourly samples are autocorrelated; n_years is the effective record length.",
            "ERA5 grid is about 0.25 deg; sample spacing below ~28 km repeats grid cells.",
        ],
        segments=out,
    )
