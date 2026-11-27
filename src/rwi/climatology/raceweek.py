"""Race-week climatology per route segment from ERA5 (ADR 0016).

Every number here is ``DataKind.CLIMATOLOGY``: a long-term statistic over many
years of 22-29 August, never a forecast for a given day. The source is ERA5
reanalysis through the Open-Meteo archive, which is itself model output, not an
observation.

Segments run control stop to control stop. Samples are pooled over all points
in a segment, all years and the selected hours. Hourly samples are strongly
autocorrelated, so ``n_years`` (not ``n_samples``) is the honest size of the
record.

Open-Meteo radiation at valid time ``t`` is the mean over ``(t - 1 h, t]``;
cloud and wind are instantaneous at ``t``. Hour selection respects that.
"""

import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta, tzinfo

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from rwi.contracts import Route, WeatherSeries
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance
from rwi.core.variables import spec_for
from rwi.regulations import EVENT_TIMEZONE, SCHEDULE
from rwi.route.bwsc import OFFICIAL_2025_CONTROL_STOPS, ControlStopRef
from rwi.route.geometry import point_at_distance

QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90)
VARIABLES = ("cloud_cover", "ghi", "wind_speed_10m", "wind_direction_10m")
RACE_WEEK_MONTH = 8
RACE_WEEK_DAYS = (22, 29)  # inclusive, event-time dates (EVENT_START_DATE..EVENT_END_DATE)
WIND_ROSE_SECTORS = 16
_HOUR = timedelta(hours=1)


class Segment(BaseModel):
    """A stretch of route between two named points, by distance [km]."""

    model_config = ConfigDict(frozen=True)

    index: int
    start_name: str
    end_name: str
    start_km: float
    end_km: float


class SamplePoint(BaseModel):
    """A route position the reanalysis was sampled at."""

    model_config = ConfigDict(frozen=True)

    distance_km: float
    point: GeoPoint
    heading_deg: float = Field(description="travel direction, clockwise from true north")


class Distribution(BaseModel):
    """Empirical distribution of one variable (CLIMATOLOGY)."""

    model_config = ConfigDict(frozen=True)

    variable: str
    unit: str
    n_samples: int
    n_years: int
    n_missing: int = Field(description="selected steps the source returned no usable value for")
    mean: float
    minimum: float
    maximum: float
    p10: float
    p25: float
    p50: float
    p75: float
    p90: float
    selection: str = Field(description="which hours or days were pooled")
    provenance: Provenance


class WindRose(BaseModel):
    """Direction frequencies in equal sectors centred on north (CLIMATOLOGY).

    Wind direction is circular, so it gets sector frequencies, not quantiles.
    """

    model_config = ConfigDict(frozen=True)

    sectors: int
    sector_width_deg: float
    centres_deg: list[float]
    frequency: list[float] = Field(description="fraction of samples per sector, sums to 1")
    mean_speed_m_s: list[float | None]
    n_samples: int
    n_years: int
    n_calm_excluded: int = Field(description="samples with speed exactly 0 (no direction)")
    selection: str
    provenance: Provenance


class SegmentClimatology(BaseModel):
    """All race-week statistics for one segment."""

    model_config = ConfigDict(frozen=True)

    segment: Segment
    points: list[SamplePoint]
    cloud_cover: Distribution | None
    ghi: Distribution | None
    wind_speed_10m: Distribution | None
    tailwind_m_s: Distribution | None = Field(
        description="wind component along the travel heading, positive = tailwind"
    )
    daily_ghi_energy: Distribution | None
    wind_rose: WindRose | None


class RaceWeekClimatology(BaseModel):
    """The built file served by ``/api/climatology/race-week``."""

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    route_id: str
    kind: DataKind = DataKind.CLIMATOLOGY
    provider: str
    dataset: str
    model_name: str
    years: list[int]
    race_week_event_dates: str = Field(description="calendar window in event time, every year")
    driving_window_event: str
    spacing_km: float
    built_at_utc: datetime
    notes: list[str]
    segments: list[SegmentClimatology]


def control_stop_segments(
    route_total_km: float,
    *,
    start_name: str = "Darwin",
    end_name: str = "Adelaide",
    stops: Sequence[ControlStopRef] = OFFICIAL_2025_CONTROL_STOPS,
) -> list[Segment]:
    """Segments start → stop 1 → … → last stop → end, ordered by distance."""
    ordered = sorted(stops, key=lambda s: s.km)
    if any(not 0.0 < s.km < route_total_km for s in ordered):
        raise ValueError("every control stop must lie strictly inside the route")
    names = [start_name, *(s.name for s in ordered), end_name]
    kms = [0.0, *(s.km for s in ordered), route_total_km]
    return [
        Segment(
            index=i, start_name=names[i], end_name=names[i + 1], start_km=kms[i], end_km=kms[i + 1]
        )
        for i in range(len(kms) - 1)
    ]


def sample_distances_km(segment: Segment, spacing_km: float) -> list[float]:
    """Cell-centred sample distances [km]: ``start + spacing·(i + ½)``; at least the midpoint."""
    if not spacing_km > 0.0 or not math.isfinite(spacing_km):
        raise ValueError(f"spacing_km must be positive and finite, got {spacing_km!r}")
    length = segment.end_km - segment.start_km
    n = max(1, math.floor(length / spacing_km))
    step = length / n
    return [segment.start_km + step * (i + 0.5) for i in range(n)]


def sample_points(route: Route, segment: Segment, spacing_km: float) -> list[SamplePoint]:
    """Positions and headings for :func:`sample_distances_km` along ``route``."""
    out = []
    for d in sample_distances_km(segment, spacing_km):
        point, heading = point_at_distance(route, d * 1000.0)
        out.append(SamplePoint(distance_km=d, point=point, heading_deg=heading))
    return out


def race_week_window_utc(year: int, tz: tzinfo = EVENT_TIMEZONE) -> tuple[datetime, datetime]:
    """UTC bounds of 22 Aug 00:00 to 30 Aug 00:00 event time in ``year`` (end exclusive)."""
    first, last = RACE_WEEK_DAYS
    start = datetime.combine(date(year, RACE_WEEK_MONTH, first), time(0), tzinfo=tz)
    end = datetime.combine(date(year, RACE_WEEK_MONTH, last), time(0), tzinfo=tz) + timedelta(1)
    return start.astimezone(UTC), end.astimezone(UTC)


def tailwind_component(speed: float, direction_from_deg: float, heading_deg: float) -> float:
    """Wind component along the travel heading [same unit as speed]; positive = tailwind."""
    return -speed * math.cos(math.radians(direction_from_deg - heading_deg))


def quantile_summary(values: Sequence[float]) -> dict[str, float]:
    """Mean, min, max and P10/P25/P50/P75/P90 (numpy linear, Hyndman-Fan type 7)."""
    arr = np.asarray(values, dtype=float)
    q = np.quantile(arr, QUANTILES)
    return {
        "mean": float(arr.mean()),
        "minimum": float(arr.min()),
        "maximum": float(arr.max()),
        **{f"p{round(p * 100)}": float(v) for p, v in zip(QUANTILES, q, strict=True)},
    }


def wind_rose_counts(
    directions_deg: Sequence[float], speeds: Sequence[float], sectors: int = WIND_ROSE_SECTORS
) -> tuple[list[int], list[float | None], int]:
    """Per-sector counts and mean speeds, sector 0 centred on north; plus the calm count."""
    width = 360.0 / sectors
    counts = [0] * sectors
    sums = [0.0] * sectors
    calm = 0
    for d, s in zip(directions_deg, speeds, strict=True):
        if s == 0.0:
            calm += 1
            continue
        k = int(((d % 360.0) + width / 2.0) // width) % sectors
        counts[k] += 1
        sums[k] += s
    means = [sums[k] / counts[k] if counts[k] else None for k in range(sectors)]
    return counts, means, calm


def _in_driving_window(start: datetime, end: datetime, tz: tzinfo) -> bool:
    """True when the closed local interval [start, end] lies inside one day's driving window."""
    a, b = start.astimezone(tz), end.astimezone(tz)
    if a.date() != b.date() and b.time() != time(0):
        return False
    lo = datetime.combine(a.date(), SCHEDULE.daily_start, tzinfo=tz)
    hi = datetime.combine(a.date(), SCHEDULE.daily_stop, tzinfo=tz)
    return lo <= a and b <= hi


def _in_race_week(when: datetime, tz: tzinfo) -> bool:
    local = when.astimezone(tz)
    first, last = RACE_WEEK_DAYS
    return local.month == RACE_WEEK_MONTH and first <= local.day <= last


class _Pool:
    """Collects samples of one segment over points and years."""

    def __init__(self, tz: tzinfo) -> None:
        self.tz = tz
        self.values: dict[str, list[float]] = defaultdict(list)
        self.missing: dict[str, int] = defaultdict(int)
        self.years: dict[str, set[int]] = defaultdict(set)
        self.dirs: list[float] = []
        self.dir_speeds: list[float] = []
        self.dir_years: set[int] = set()

    def add(self, key: str, value: float | None, year: int) -> None:
        if value is None or not math.isfinite(value):
            self.missing[key] += 1
            return
        self.values[key].append(value)
        self.years[key].add(year)

    def add_series(self, series: WeatherSeries, heading_deg: float, year: int) -> None:
        """Pool one point-year; ``heading_deg`` is the route heading at that point."""
        tz = self.tz
        hourly_ghi: dict[datetime, float | None] = {}
        for step in series.steps:
            t = step.valid_time_utc
            ghi = step.value("ghi")
            hourly_ghi[t] = ghi
            # Radiation: preceding-hour mean over (t - 1 h, t].
            if _in_race_week(t - _HOUR, tz) and _in_driving_window(t - _HOUR, t, tz):
                self.add("ghi", ghi, year)
            # Cloud and wind: instantaneous at t.
            if not (_in_race_week(t, tz) and _in_driving_window(t, t, tz)):
                continue
            self.add("cloud_cover", step.value("cloud_cover"), year)
            speed, direction = step.value("wind_speed_10m"), step.value("wind_direction_10m")
            self.add("wind_speed_10m", speed, year)
            if speed is None or direction is None:
                self.add("tailwind_m_s", None, year)
                continue
            self.add("tailwind_m_s", tailwind_component(speed, direction, heading_deg), year)
            self.dirs.append(direction)
            self.dir_speeds.append(speed)
            self.dir_years.add(year)
        first, last = RACE_WEEK_DAYS
        for day in range(first, last + 1):
            energy = daily_energy_kwh(date(year, RACE_WEEK_MONTH, day), hourly_ghi, tz)
            self.add("daily_ghi_energy", energy, year)


def whole_hour_intervals(day: date, tz: tzinfo) -> list[datetime]:
    """End times ``t`` (whole UTC hours) whose interval ``(t - 1 h, t]`` lies inside local ``day``.

    With a non-whole-hour offset (ACST is UTC+9:30) the half hour either side of
    local midnight falls outside every such interval.
    """
    lo = datetime.combine(day, time(0), tzinfo=tz).astimezone(UTC)
    hi = lo + timedelta(days=1)
    t = lo.replace(minute=0, second=0, microsecond=0)
    if t < lo:
        t += _HOUR
    out = []
    while t <= hi:
        if t - _HOUR >= lo:
            out.append(t)
        t += _HOUR
    return out


def daily_energy_kwh(
    day: date, hourly_mean_ghi: dict[datetime, float | None], tz: tzinfo
) -> float | None:
    """Sum of preceding-hour mean GHI x 1 h inside local ``day`` [kWh/m2]; None on any gap."""
    expected = whole_hour_intervals(day, tz)
    values = [hourly_mean_ghi.get(t) for t in expected]
    if not expected or any(v is None or not math.isfinite(v) for v in values):
        return None
    return sum(v for v in values if v is not None) / 1000.0


def _provenance(
    variable: str,
    *,
    years: Sequence[int],
    segment: Segment,
    point: GeoPoint,
    retrieved: datetime,
    model: str,
    resolution_km: float | None,
) -> Provenance:
    span = f"{min(years)}-{max(years)}" if years else "no years"
    return Provenance(
        provider="open-meteo",
        dataset="archive",
        kind=DataKind.CLIMATOLOGY,
        retrieved_at_utc=retrieved,
        model_name=model,
        point=point,
        spatial_resolution_km=resolution_km,
        temporal_resolution_s=3600.0,
        processing=(
            f"{variable}: pooled over all {segment.start_name}-{segment.end_name} sample points, "
            f"22-29 Aug event time, {span}; point = sample nearest the segment middle"
        ),
        license="CC BY 4.0 (Open-Meteo); ERA5: Copernicus Climate Change Service licence",
        source_url="https://archive-api.open-meteo.com/v1/archive",
        notes="reanalysis climatology: not an observation and not a forecast for any day",
    )


_SELECTION = {
    "ghi": "preceding-hour means wholly inside 08:00-17:00 event time",
    "cloud_cover": "instantaneous steps inside 08:00-17:00 event time",
    "wind_speed_10m": "instantaneous steps inside 08:00-17:00 event time",
    "tailwind_m_s": "instantaneous steps inside 08:00-17:00 event time, route heading per point",
    "daily_ghi_energy": "one value per point per complete event-time day (no gaps)",
}
_UNITS = {"tailwind_m_s": "m/s", "daily_ghi_energy": "kWh/m2"}


def segment_climatology(
    segment: Segment,
    points: Sequence[SamplePoint],
    series: Sequence[tuple[int, int, WeatherSeries]],
    *,
    retrieved_at_utc: datetime,
    model: str = "era5",
    tz: tzinfo = EVENT_TIMEZONE,
) -> SegmentClimatology:
    """Pool ``(point_index, year, series)`` into a segment's distributions (CLIMATOLOGY)."""
    if not points:
        raise ValueError("a segment needs at least one sample point")
    pool = _Pool(tz)
    resolution: float | None = None
    for idx, year, s in series:
        pool.add_series(s, points[idx].heading_deg, year)
        resolution = resolution or s.spatial_resolution_km
    middle = (segment.start_km + segment.end_km) / 2.0
    anchor = min(points, key=lambda p: abs(p.distance_km - middle)).point

    def prov(key: str, years: Sequence[int]) -> Provenance:
        return _provenance(
            key,
            years=years,
            segment=segment,
            point=anchor,
            retrieved=retrieved_at_utc,
            model=model,
            resolution_km=resolution,
        )

    def dist(key: str) -> Distribution | None:
        vals = pool.values.get(key, [])
        if not vals:
            return None
        years = sorted(pool.years[key])
        return Distribution(
            variable=key,
            unit=_UNITS.get(key) or spec_for(key).unit,
            n_samples=len(vals),
            n_years=len(years),
            n_missing=pool.missing.get(key, 0),
            selection=_SELECTION[key],
            provenance=prov(key, years),
            **quantile_summary(vals),
        )

    rose = None
    counts, means, calm = wind_rose_counts(pool.dirs, pool.dir_speeds)
    n = sum(counts)
    if n:
        width = 360.0 / WIND_ROSE_SECTORS
        rose = WindRose(
            sectors=WIND_ROSE_SECTORS,
            sector_width_deg=width,
            centres_deg=[k * width for k in range(WIND_ROSE_SECTORS)],
            frequency=[c / n for c in counts],
            mean_speed_m_s=means,
            n_samples=n,
            n_years=len(pool.dir_years),
            n_calm_excluded=calm,
            selection=_SELECTION["wind_speed_10m"],
            provenance=prov("wind_direction_10m", sorted(pool.dir_years)),
        )
    return SegmentClimatology(
        segment=segment,
        points=list(points),
        cloud_cover=dist("cloud_cover"),
        ghi=dist("ghi"),
        wind_speed_10m=dist("wind_speed_10m"),
        tailwind_m_s=dist("tailwind_m_s"),
        daily_ghi_energy=dist("daily_ghi_energy"),
        wind_rose=rose,
    )
