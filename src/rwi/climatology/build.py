"""Fetch ERA5 race weeks through the Open-Meteo archive and build the climatology (ADR 0016)."""

import asyncio
from datetime import datetime
from pathlib import Path

from rwi.climatology.raceweek import (
    RACE_WEEK_DAYS,
    VARIABLES,
    RaceWeekClimatology,
    control_stop_segments,
    race_week_window_utc,
    sample_points,
    segment_climatology,
)
from rwi.contracts import Route, WeatherSeries
from rwi.core.timebase import TimeWindow, utc_now
from rwi.providers.openmeteo import OpenMeteoProvider
from rwi.regulations import EVENT_TIMEZONE, SCHEDULE

ERA5_MODEL = "era5"


def climatology_path(directory: Path, route_id: str) -> Path:
    """Where the built race-week climatology for ``route_id`` lives."""
    return directory / f"{route_id}-race-week.json"


async def build_race_week_climatology(
    provider: OpenMeteoProvider,
    route: Route,
    *,
    first_year: int,
    last_year: int,
    spacing_km: float,
    delay_s: float,
    now: datetime | None = None,
) -> RaceWeekClimatology:
    """ERA5 22-29 Aug climatology per control-stop segment (``DataKind.CLIMATOLOGY``).

    One archive request per point per year. Any provider error propagates: a
    partial climatology is never written.
    """
    if provider.radiation_mode != "preceding_hour_mean":
        raise ValueError("climatology daily energy needs preceding-hour mean radiation")
    if first_year > last_year:
        raise ValueError(f"first_year {first_year} is after last_year {last_year}")
    if delay_s < 0.0:
        raise ValueError(f"delay_s must be >= 0, got {delay_s!r}")
    built = now or utc_now()
    years = list(range(first_year, last_year + 1))
    if race_week_window_utc(last_year)[1] > built:
        raise ValueError(f"race week {last_year} is not over yet; ERA5 has no data for it")

    segments = control_stop_segments(route.total_distance_m / 1000.0)
    out = []
    for segment in segments:
        points = sample_points(route, segment, spacing_km)
        series: list[tuple[int, int, WeatherSeries]] = []
        for idx, sp in enumerate(points):
            for year in years:
                start, end = race_week_window_utc(year)
                s = await provider.fetch_analysis(
                    sp.point,
                    TimeWindow(start_utc=start, end_utc=end),
                    VARIABLES,
                    models=ERA5_MODEL,
                )
                series.append((idx, year, s))
                if delay_s:
                    await asyncio.sleep(delay_s)
        out.append(segment_climatology(segment, points, series, retrieved_at_utc=built))

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
