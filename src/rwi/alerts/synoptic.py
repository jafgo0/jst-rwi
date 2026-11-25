"""Surface synoptic analysis along the route: pressure tendencies, meridional
pressure profile and cold-front passage signatures (competencies #10, #14).

What this engine does (step A, surface fields only):

* **Pressure tendency** per route point: forward change of mean-sea-level
  pressure over 3 h and 24 h from the first forecast step at or after now (the
  window may include a model look-back for the front rules). Forward and
  model-derived; it is NOT the observed barometric tendency of a METAR/SYNOP.
* **Meridional profile**: MSLP difference between neighbouring route points at
  the same valid time, as Pa per 100 km of great-circle distance. The route runs
  roughly north-south, so this is a crude stand-in for the north-south
  pressure gradient. It is not a gradient-wind or geostrophic analysis.
* **Front passage signatures** per point, from four surface criteria taken
  from textbook cold-front structure (southern hemisphere):

  1. ``pressure_trough``: a local minimum followed by a rise of at least
     ``front_dp_rise_pa_3h`` within ``half_window_h``. By default
     (``pressure_detrend_24h``) the test runs on the 24 h difference
     ``p(t) - p(t-24h)``, which cancels the diurnal and semidiurnal
     atmospheric tide. On raw MSLP, ERA5 detections locked to two local-time
     bands 12 h apart (Rayleigh test on the 12 h harmonic, p = 5e-40; after
     detrending p = 0.36, see docs/validation/synoptic-calibration.md).
     Without 24 h of history over the whole window the criterion is *not
     evaluated*. ``dp_rise_pa`` is always the raw MSLP rise.
  2. ``wind_shift``: 10 m wind goes from the N-NW sector to the S-SW sector
     backing (turning counter-clockwise, through W), as a cold front does in
     the southern hemisphere, by at least ``front_wind_shift_deg``. A veering
     turn through E between the same sectors is rejected. Directions in light
     winds (below ``min_wind_m_s``) are not used.
  3. ``temperature_drop``: the 24 h temperature change at the post-passage
     time is at most ``-front_dt_drop_k_24h``. A 24 h difference removes the
     diurnal cycle, so an evening cooling is not mistaken for a front. Without
     a step 24 h earlier the criterion is *not evaluated*, never "not met".
  4. ``dewpoint_drop``: dewpoint falls by at least ``front_dtd_drop_k`` across
     the window.

  If 3 or 4 criteria are met the result is a ``signature``. If 2 are met it is
  ``possible``. Every match must include at least one kinematic criterion
  (pressure trough or wind shift). Otherwise a dry, cooling afternoon would
  count as a front.
* **Front speed along the route**: only when the same feature is seen at two or
  more route points. Fronts at neighbouring points are paired when their
  passage times are less than ``max_pairing_h`` apart. A pair is dropped when
  its implied speed is above ``max_front_speed_m_s``, because that is two
  different features, not one fast front.

Known limits (also printed by the briefing):

* The thresholds are GENERIC textbook values. They are NOT calibrated against
  Stuart Highway observations (``calibrated=False`` on every result).
* There are no upper-air fields (500 hPa trough/ridge, jet). A surface
  signature without upper support may be a shallow feature or a model artefact.
* An inland **heat trough** also shows a pressure minimum and a N to S wind
  shift. The 24 h temperature criterion is what separates it from a cold
  front, so a ``possible`` without ``temperature_drop`` may be a trough line.
* The engine reads model output, so a "front" here is a front *in the model*.
* In the tropical north (Darwin, Katherine) the cold-front criteria have
  little physical meaning. The engine still evaluates them and does not
  suppress anything.

References:

* Bjerknes, J., Solberg, H. (1922). "Life cycle of cyclones and the polar
  front theory of atmospheric circulation." Geofys. Publ. 3(1).
* Sturman, A., Tapper, N. (2006). *The Weather and Climate of Australia and
  New Zealand*, 2nd ed., Oxford University Press, ch. 7 (cold fronts over
  southern Australia; inland heat troughs).
* AMS Glossary of Meteorology, s.v. "cold front", "pressure tendency".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from statistics import median
from typing import Literal

from rwi.contracts import WeatherSeries, WeatherStep
from rwi.core.geo import GeoPoint, geodesic_distance_m
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import ensure_utc, utc_now

SYNOPTIC_VARIABLES: tuple[str, ...] = (
    "pressure_msl",
    "temperature_2m",
    "dew_point_2m",
    "wind_speed_10m",
    "wind_direction_10m",
)
"""Variables the engine reads; the API fetches exactly these per route point."""

MIN_POINTS = 3
"""Below this many usable points there is no route-scale analysis: unknown, not calm."""

CRITERIA: tuple[str, ...] = ("pressure_trough", "wind_shift", "temperature_drop", "dewpoint_drop")
_KINEMATIC = frozenset({"pressure_trough", "wind_shift"})

# Southern-hemisphere cold front: prefrontal N-NW, postfrontal S-SW (W allowed both sides).
_PRE_SECTOR = ((280.0, 360.0), (0.0, 30.0))
_POST_SECTOR = ((150.0, 260.0),)


@dataclass(frozen=True)
class SynopticThresholds:
    """Generic, uncalibrated thresholds. Changing them is a calibration decision."""

    front_dp_rise_pa_3h: float = 100.0
    front_wind_shift_deg: float = 60.0
    front_dt_drop_k_24h: float = 3.0
    front_dtd_drop_k: float = 3.0
    min_wind_m_s: float = 2.0
    half_window_h: int = 3
    pressure_detrend_24h: bool = True
    """Run the trough test on ``p(t) - p(t-24h)`` so the semidiurnal tide cancels."""
    dedupe_h: int = 6
    max_pairing_h: int = 24
    max_front_speed_m_s: float = 30.0


DEFAULT_THRESHOLDS = SynopticThresholds()


@dataclass(frozen=True)
class RoutePointSeries:
    """One route point and its forecast series (``None`` when the fetch failed)."""

    name: str
    route_distance_m: float
    point: GeoPoint
    series: WeatherSeries | None = None
    error: str | None = None


@dataclass(frozen=True)
class PressureTendency:
    name: str
    position: GeoPoint
    valid_time_utc: datetime
    pressure_pa: float
    dp_3h_pa: float | None
    dp_24h_pa: float | None
    provenance: Provenance


@dataclass(frozen=True)
class MeridionalSegment:
    north: str
    south: str
    valid_time_utc: datetime
    dp_pa: float
    """``p(south) - p(north)``; positive = higher pressure to the south."""
    distance_km: float
    gradient_pa_per_100km: float


@dataclass(frozen=True)
class FrontSignature:
    name: str
    position: GeoPoint
    route_distance_m: float
    passage_time_utc: datetime
    criteria_met: tuple[str, ...]
    criteria_evaluated: tuple[str, ...]
    strength: Literal["signature", "possible"]
    dp_rise_pa: float | None
    wind_shift_deg: float | None
    dt_24h_k: float | None
    dtd_k: float | None
    provenance: Provenance


@dataclass(frozen=True)
class SynopticAnalysis:
    issued_at_utc: datetime
    points: tuple[str, ...]
    """Usable points, ordered along the route (north to south)."""
    missing: tuple[str, ...]
    tendencies: tuple[PressureTendency, ...]
    meridional_profile: tuple[MeridionalSegment, ...]
    fronts: tuple[FrontSignature, ...]
    front_speed_m_s: float | None
    front_direction: Literal["northward", "southward"] | None
    thresholds: SynopticThresholds
    provider: str | None = None
    model_name: str | None = None
    model_init_time_utc: datetime | None = None
    calibrated: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def available(self) -> bool:
        return len(self.points) >= MIN_POINTS


# ----------------------------------------------------------------------------- helpers


class _Index:
    """O(1) lookup of a variable at an exact valid time."""

    __slots__ = ("by_time",)

    def __init__(self, series: WeatherSeries) -> None:
        self.by_time: dict[datetime, WeatherStep] = {s.valid_time_utc: s for s in series.steps}

    def get(self, t: datetime, var: str) -> float | None:
        step = self.by_time.get(t)
        return None if step is None else step.value(var)


def _in_sector(direction: float, sector: Sequence[tuple[float, float]]) -> bool:
    d = direction % 360.0
    return any(lo <= d <= hi for lo, hi in sector)


def _angular_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return 360.0 - d if d > 180.0 else d


def _signed_turn(a: float, b: float) -> float:
    """Shortest turn from ``a`` to ``b`` in degrees, in [-180, 180).

    Positive is clockwise (veering), negative counter-clockwise (backing).
    """
    return (b - a + 540.0) % 360.0 - 180.0


def _provenance(series: WeatherSeries, now: datetime, t: datetime, processing: str) -> Provenance:
    return Provenance(
        provider="rwi",
        dataset="synoptic",
        kind=DataKind.DERIVED,
        retrieved_at_utc=now,
        valid_time_utc=t,
        model_name=series.model_name,
        model_init_time_utc=series.model_init_time_utc,
        point=series.point,
        processing=f"{processing} (input: {series.provider}/{series.dataset})",
    )


# ----------------------------------------------------------------------------- tendencies


def pressure_tendency(
    name: str, series: WeatherSeries, *, now: datetime | None = None
) -> PressureTendency | None:
    """Forward 3 h / 24 h MSLP change from the first step at or after ``now``.

    The series may start earlier (a look-back window lets the front rules see
    the day before); steps before the hour containing ``now`` are skipped so the
    tendency is always forward from the present, never from yesterday.
    """
    now = ensure_utc(now) if now is not None else utc_now()
    anchor = now.replace(minute=0, second=0, microsecond=0)
    idx = _Index(series)
    for step in series.steps:
        if step.valid_time_utc < anchor:
            continue
        p0 = step.value("pressure_msl")
        if p0 is None:
            continue
        t0 = step.valid_time_utc
        p3 = idx.get(t0 + timedelta(hours=3), "pressure_msl")
        p24 = idx.get(t0 + timedelta(hours=24), "pressure_msl")
        return PressureTendency(
            name=name,
            position=series.point,
            valid_time_utc=t0,
            pressure_pa=p0,
            dp_3h_pa=None if p3 is None else p3 - p0,
            dp_24h_pa=None if p24 is None else p24 - p0,
            provenance=_provenance(series, now, t0, "synoptic_tendency"),
        )
    return None


def meridional_profile(tendencies: Sequence[PressureTendency]) -> list[MeridionalSegment]:
    """Neighbour-to-neighbour MSLP difference at a common valid time.

    ``tendencies`` must be in route order (north to south). Neighbours whose
    first valid times differ are skipped, because comparing pressures at
    different times is not a gradient.
    """
    out: list[MeridionalSegment] = []
    for north, south in pairwise(tendencies):
        if north.valid_time_utc != south.valid_time_utc:
            continue
        dist_km = geodesic_distance_m(north.position, south.position) / 1000.0
        if dist_km <= 0.0:
            continue
        dp = south.pressure_pa - north.pressure_pa
        out.append(
            MeridionalSegment(
                north=north.name,
                south=south.name,
                valid_time_utc=north.valid_time_utc,
                dp_pa=dp,
                distance_km=dist_km,
                gradient_pa_per_100km=dp / dist_km * 100.0,
            )
        )
    return out


# ----------------------------------------------------------------------------- fronts


def detect_front_passages(
    rp: RoutePointSeries,
    thr: SynopticThresholds = DEFAULT_THRESHOLDS,
    *,
    now: datetime | None = None,
) -> list[FrontSignature]:
    """Candidate cold-front passages at one point (see module doc for criteria)."""
    series = rp.series
    if series is None or not series.steps:
        return []
    now = ensure_utc(now) if now is not None else utc_now()
    idx = _Index(series)
    h = timedelta(hours=thr.half_window_h)
    day = timedelta(hours=24)
    hourly = [timedelta(hours=k) for k in range(-thr.half_window_h, thr.half_window_h + 1)]

    def _p(tt: datetime) -> float | None:
        v = idx.get(tt, "pressure_msl")
        if v is None or not thr.pressure_detrend_24h:
            return v
        prev = idx.get(tt - day, "pressure_msl")
        return None if prev is None else v - prev

    candidates: list[FrontSignature] = []
    for step in series.steps:
        t = step.valid_time_utc
        before, after = t - h, t + h
        if before not in idx.by_time or after not in idx.by_time:
            continue
        met: list[str] = []
        evaluated: list[str] = []

        # 1. pressure trough then rise. With detrending the test runs on the 24 h
        # difference, which removes the (24 h and 12 h periodic) atmospheric tide
        # that otherwise puts a "trough" at the same local hour every afternoon.
        # The test needs the whole window: a truncated one makes its first hour a
        # spurious "minimum". dp_rise_pa reports the raw MSLP rise, not the
        # rise of the difference.
        dp_rise: float | None = None
        window = [_p(t + k) for k in hourly]
        raw_t, raw_after = idx.get(t, "pressure_msl"), idx.get(after, "pressure_msl")
        if raw_t is not None and raw_after is not None and None not in window:
            evaluated.append("pressure_trough")
            dp_rise = raw_after - raw_t
            vals = [v for v in window if v is not None]
            d_t, d_rise = vals[thr.half_window_h], vals[-1] - vals[thr.half_window_h]
            if d_t <= min(vals) and d_rise >= thr.front_dp_rise_pa_3h:
                met.append("pressure_trough")

        # 2. wind shift N-NW -> S-SW, backing through W (SH cold front)
        shift: float | None = None
        d0, d1 = idx.get(before, "wind_direction_10m"), idx.get(after, "wind_direction_10m")
        s0, s1 = idx.get(before, "wind_speed_10m"), idx.get(after, "wind_speed_10m")
        if (
            d0 is not None
            and d1 is not None
            and s0 is not None
            and s1 is not None
            and min(s0, s1) >= thr.min_wind_m_s
        ):
            evaluated.append("wind_shift")
            shift = _angular_diff(d0, d1)
            if (
                _in_sector(d0, _PRE_SECTOR)
                and _in_sector(d1, _POST_SECTOR)
                and shift >= thr.front_wind_shift_deg
                and _signed_turn(d0, d1) < 0.0
            ):
                met.append("wind_shift")

        # 3. 24 h temperature change at the post-passage time (removes diurnal cycle)
        dt24: float | None = None
        ta, ta_prev = idx.get(after, "temperature_2m"), idx.get(after - day, "temperature_2m")
        if ta is not None and ta_prev is not None:
            evaluated.append("temperature_drop")
            dt24 = ta - ta_prev
            if dt24 <= -thr.front_dt_drop_k_24h:
                met.append("temperature_drop")

        # 4. dewpoint drop across the window
        dtd: float | None = None
        td0, td1 = idx.get(before, "dew_point_2m"), idx.get(after, "dew_point_2m")
        if td0 is not None and td1 is not None:
            evaluated.append("dewpoint_drop")
            dtd = td1 - td0
            if -dtd >= thr.front_dtd_drop_k:
                met.append("dewpoint_drop")

        if len(met) < 2 or not _KINEMATIC.intersection(met):
            continue
        candidates.append(
            FrontSignature(
                name=rp.name,
                position=series.point,
                route_distance_m=rp.route_distance_m,
                passage_time_utc=t,
                criteria_met=tuple(met),
                criteria_evaluated=tuple(evaluated),
                strength="signature" if len(met) >= 3 else "possible",
                dp_rise_pa=dp_rise,
                wind_shift_deg=shift,
                dt_24h_k=dt24,
                dtd_k=dtd,
                provenance=_provenance(series, now, t, "synoptic_front_rules"),
            )
        )
    return _dedupe(candidates, timedelta(hours=thr.dedupe_h))


def _dedupe(cands: list[FrontSignature], gap: timedelta) -> list[FrontSignature]:
    """One front per cluster of candidates closer than ``gap``: most criteria, then largest rise."""
    out: list[FrontSignature] = []
    for c in cands:
        if out and c.passage_time_utc - out[-1].passage_time_utc < gap:
            if _rank(c) > _rank(out[-1]):
                out[-1] = c
            continue
        out.append(c)
    return out


def _rank(f: FrontSignature) -> tuple[int, float]:
    return (len(f.criteria_met), f.dp_rise_pa or 0.0)


def front_speed(
    fronts_by_point: Sequence[Sequence[FrontSignature]],
    thr: SynopticThresholds = DEFAULT_THRESHOLDS,
) -> tuple[float | None, Literal["northward", "southward"] | None]:
    """Median along-route speed from fronts paired between neighbouring detecting points.

    ``fronts_by_point`` is in route order. Returns ``(None, None)`` unless at
    least one physically plausible pair exists.
    """
    detecting = [fs for fs in fronts_by_point if fs]
    max_dt = timedelta(hours=thr.max_pairing_h)
    pairs: list[tuple[float, int]] = []  # (speed m/s, +1 southward / -1 northward)
    for a_list, b_list in pairwise(detecting):
        for a in a_list:
            t_a = a.passage_time_utc
            best = min(b_list, key=lambda b: abs(b.passage_time_utc - t_a))
            dt = (best.passage_time_utc - a.passage_time_utc).total_seconds()
            dist = best.route_distance_m - a.route_distance_m
            if dt == 0.0 or abs(dt) > max_dt.total_seconds() or dist == 0.0:
                continue
            speed = abs(dist) / abs(dt)
            if speed > thr.max_front_speed_m_s:
                continue
            # b is further south (dist > 0); reached later => moving southward.
            pairs.append((speed, 1 if (dt > 0) == (dist > 0) else -1))
    if not pairs:
        return None, None
    south = sum(1 for _, d in pairs if d > 0)
    sign = 1 if south * 2 >= len(pairs) else -1
    speeds = [s for s, d in pairs if d == sign]
    return median(speeds), ("southward" if sign > 0 else "northward")


# ----------------------------------------------------------------------------- entry point


def analyse_route(
    points: Sequence[RoutePointSeries],
    thr: SynopticThresholds = DEFAULT_THRESHOLDS,
    *,
    now: datetime | None = None,
) -> SynopticAnalysis:
    """Route-scale surface analysis. Fewer than :data:`MIN_POINTS` usable points → not available."""
    now = ensure_utc(now) if now is not None else utc_now()
    ordered = sorted(points, key=lambda p: p.route_distance_m)
    missing: list[str] = []
    tendencies: list[PressureTendency] = []
    fronts_by_point: list[list[FrontSignature]] = []
    usable: list[RoutePointSeries] = []
    for rp in ordered:
        tend = (
            pressure_tendency(rp.name, rp.series, now=now)
            if rp.series is not None and rp.series.steps
            else None
        )
        if tend is None:
            missing.append(rp.name)
            continue
        usable.append(rp)
        tendencies.append(tend)
        fronts_by_point.append(detect_front_passages(rp, thr, now=now))

    first = usable[0].series if usable else None
    notes: list[str] = []
    if usable and len(usable) < MIN_POINTS:
        notes.append(f"only {len(usable)} usable route points; need {MIN_POINTS}")
    if len(usable) < MIN_POINTS:
        return SynopticAnalysis(
            issued_at_utc=now,
            points=tuple(p.name for p in usable),
            missing=tuple(missing),
            tendencies=(),
            meridional_profile=(),
            fronts=(),
            front_speed_m_s=None,
            front_direction=None,
            thresholds=thr,
            notes=tuple(notes),
        )
    speed, direction = front_speed(fronts_by_point, thr)
    fronts = sorted((f for fs in fronts_by_point for f in fs), key=lambda f: f.passage_time_utc)
    return SynopticAnalysis(
        issued_at_utc=now,
        points=tuple(p.name for p in usable),
        missing=tuple(missing),
        tendencies=tuple(tendencies),
        meridional_profile=tuple(meridional_profile(tendencies)),
        fronts=tuple(fronts),
        front_speed_m_s=speed,
        front_direction=direction,
        thresholds=thr,
        provider=first.provider if first else None,
        model_name=first.model_name if first else None,
        model_init_time_utc=first.model_init_time_utc if first else None,
        notes=tuple(notes),
    )


__all__ = [
    "CRITERIA",
    "DEFAULT_THRESHOLDS",
    "MIN_POINTS",
    "SYNOPTIC_VARIABLES",
    "FrontSignature",
    "MeridionalSegment",
    "PressureTendency",
    "RoutePointSeries",
    "SynopticAnalysis",
    "SynopticThresholds",
    "analyse_route",
    "detect_front_passages",
    "front_speed",
    "meridional_profile",
    "pressure_tendency",
]
