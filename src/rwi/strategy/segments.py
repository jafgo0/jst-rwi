"""Route segments: the unit of integration for the scenario engine.

A scenario is evaluated as a chain of short, straight segments. Within one
segment the gradient, heading, wind, air density, available array power, speed
limit and power limit are all held constant, and the vehicle holds one speed.
Everything the strategy engine knows about the world arrives through this type,
which keeps the engine independent of :mod:`rwi.route`, :mod:`rwi.solar` and
:mod:`rwi.energy` - those modules (owned by other engineers) can produce
segments, but nothing here imports them at module import time.

Discretisation error is the price of this simplicity: a 25 km segment averages
away every hill and every gust inside it. Shorter segments are strictly more
faithful and cost only CPU time.

Validation status: implemented, not validated.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rwi.contracts import Trajectory
from rwi.core.provenance import Measured

GenerationBand = Literal["p10", "p50", "p90"]

__all__ = [
    "GenerationBand",
    "SegmentConditions",
    "attach_generation",
    "attach_generation_band",
    "generation_band_from_ghi",
    "generation_index",
    "ghi_band_ratios",
    "scale_generation",
    "scale_wind",
    "segments_from_distance",
    "segments_from_trajectory",
    "select_generation_band",
    "total_distance_m",
]


class SegmentConditions(BaseModel):
    """Everything the power model needs for one piece of road.

    Units: metres, dimensionless rise/run, degrees clockwise from true north,
    m/s, Pa, degC, W, Wh, km/h, seconds.
    """

    model_config = ConfigDict(frozen=True)

    distance_m: float = Field(gt=0.0, description="length of the segment, m")
    grade: float = Field(default=0.0, description="rise/run, positive uphill")
    heading_deg: float = Field(
        default=0.0, ge=0.0, le=360.0, description="direction of travel, clockwise from true north"
    )
    wind_speed_mps: float = Field(default=0.0, ge=0.0, description="true wind speed, m/s")
    wind_from_deg: float = Field(
        default=0.0, ge=0.0, le=360.0, description="meteorological direction the wind blows FROM"
    )
    air_density_kg_m3: float | None = Field(default=None, gt=0.0)
    pressure_pa: float | None = Field(default=None, gt=0.0)
    temperature_c: float | None = None
    generation_w: float | None = Field(
        default=None,
        ge=0.0,
        description="mean net array power over the segment, W; converted to Wh with the time "
        "actually spent in the segment, so driving slower collects more energy",
    )
    generation_wh: float | None = Field(
        default=None,
        ge=0.0,
        description="energy generated while traversing the segment, Wh; a fixed figure that does "
        "NOT change with the speed driven - only use it when that is what you mean",
    )
    generation_p10_w: float | None = Field(
        default=None,
        ge=0.0,
        description="P10 of the mean net array power over the segment, W (ADR 0008); only "
        "meaningful next to generation_w, which is then the P50",
    )
    generation_p90_w: float | None = Field(
        default=None,
        ge=0.0,
        description="P90 of the mean net array power over the segment, W (ADR 0008)",
    )
    speed_limit_kmh: float | None = Field(
        default=None, gt=0.0, description="posted legal limit on this segment, km/h"
    )
    power_limit_w: float | None = Field(
        default=None, gt=0.0, description="maximum electrical demand allowed on this segment, W"
    )
    dwell_s: float = Field(
        default=0.0, ge=0.0, description="mandatory stationary time at the end of the segment, s"
    )
    label: str | None = None

    @model_validator(mode="after")
    def _one_generation_convention(self) -> Self:
        if self.generation_w is not None and self.generation_wh is not None:
            raise ValueError(
                "set generation_w (mean power) or generation_wh (fixed energy), not both: "
                "they answer different questions about how generation scales with speed"
            )
        if (self.generation_p10_w is None) != (self.generation_p90_w is None):
            raise ValueError("set both generation_p10_w and generation_p90_w, or neither")
        if self.generation_p10_w is not None and self.generation_p90_w is not None:
            if self.generation_w is None:
                raise ValueError("a generation band needs generation_w as its P50")
            if not self.generation_p10_w <= self.generation_w <= self.generation_p90_w:
                raise ValueError("generation band must satisfy P10 <= generation_w <= P90")
        return self

    @property
    def has_generation_band(self) -> bool:
        """True when P10 and P90 generation are attached to this segment."""
        return self.generation_p10_w is not None and self.generation_p90_w is not None


def total_distance_m(segments: Sequence[SegmentConditions]) -> float:
    """Sum of the segment lengths, m."""
    return float(sum(s.distance_m for s in segments))


def generation_index(segment: SegmentConditions) -> float | None:
    """A speed-independent measure of how sunny a segment is, or None if unknown.

    ``generation_w`` is already speed-independent. ``generation_wh`` is turned
    into Wh per metre so that segments of different lengths can be compared.
    Only used for *relative* comparisons (the weather-aware heuristic), never
    for energy accounting.
    """
    if segment.generation_w is not None:
        return segment.generation_w
    if segment.generation_wh is not None:
        return segment.generation_wh / segment.distance_m
    return None


def segments_from_distance(
    distance_m: float,
    *,
    segment_count: int = 1,
    **shared: Any,
) -> list[SegmentConditions]:
    """``segment_count`` identical segments covering ``distance_m``.

    For the simplest possible scenario ("3000 km of flat, still air"), and for
    tests. Extra keyword arguments are passed to every
    :class:`SegmentConditions`, so ``grade=0.01`` makes the whole leg a 1 %
    climb.
    """
    if distance_m <= 0.0:
        raise ValueError("distance_m must be positive")
    if segment_count < 1:
        raise ValueError("segment_count must be at least 1")
    length = distance_m / segment_count
    return [SegmentConditions(distance_m=length, **shared) for _ in range(segment_count)]


def _align(
    values: Sequence[float] | float | None,
    *,
    n_points: int,
    name: str,
    average_endpoints: bool,
) -> list[float | None]:
    """Normalise a scalar / point-aligned / segment-aligned input to one value per segment.

    ``average_endpoints`` applies the trapezoidal rule to point-aligned values
    (right for a power series). It is False for angles and for anything where
    averaging two numbers is meaningless - a wind *direction* in particular,
    where the arithmetic mean of 350 and 10 degrees is 180, exactly backwards.
    Those take the value at the start of the segment instead.
    """
    n_segments = n_points - 1
    if values is None:
        return [None] * n_segments
    if isinstance(values, int | float):
        return [float(values)] * n_segments
    seq = list(values)
    if len(seq) == n_segments:
        return [float(v) for v in seq]
    if len(seq) == n_points:
        if average_endpoints:
            return [0.5 * (float(seq[i]) + float(seq[i + 1])) for i in range(n_segments)]
        return [float(seq[i]) for i in range(n_segments)]
    raise ValueError(
        f"{name} has {len(seq)} values; expected {n_points} (one per trajectory point) "
        f"or {n_segments} (one per segment), or a single scalar"
    )


def _net_power_w(item: Any) -> float:
    """Net array power from a PowerEstimate-shaped object or a plain number."""
    value = getattr(item, "net_power_w", item)
    return float(value)


def segments_from_trajectory(
    trajectory: Trajectory,
    *,
    generation: Sequence[Any] | float | None = None,
    wind_speed_mps: Sequence[float] | float | None = None,
    wind_from_deg: Sequence[float] | float | None = None,
    pressure_pa: Sequence[float] | float | None = None,
    temperature_c: Sequence[float] | float | None = None,
    speed_limit_kmh: Sequence[float] | float | None = None,
    power_limit_w: Sequence[float] | float | None = None,
    honour_dwell: bool = True,
) -> list[SegmentConditions]:
    """Turn a :class:`~rwi.contracts.Trajectory` into segments.

    One segment per consecutive pair of trajectory points. Its length is the
    difference in ``distance_m``; its gradient and heading are taken from the
    *first* point of the pair (they describe the road ahead of that point);
    mandatory control-stop dwell time found on the *second* point becomes the
    segment's ``dwell_s`` when ``honour_dwell`` is true.

    ``generation`` accepts :class:`~rwi.contracts.PowerEstimate` objects (their
    ``net_power_w`` is used), plain watt numbers, or a single scalar, aligned
    either with the trajectory points (averaged pairwise, trapezoidal) or with
    the segments. Weather inputs follow the same alignment rules; wind
    *direction* is never averaged (see :func:`_align`).

    The trajectory's own ``speed_mps`` is deliberately ignored: the whole point
    of a scenario is to ask what happens at a *different* speed. Only its
    geometry and gradient are used. The consequence is that the arrival times
    inside the trajectory no longer apply, so any time-varying weather or
    irradiance attached to those times is only valid near the speed the
    trajectory was built for - see ``docs/science/vehicle.md``.
    """
    points = trajectory.points
    if len(points) < 2:
        raise ValueError("a trajectory needs at least two points to form a segment")

    n_points = len(points)
    gen = _align(
        [_net_power_w(g) for g in generation] if isinstance(generation, Sequence) else generation,
        n_points=n_points,
        name="generation",
        average_endpoints=True,
    )
    wind_speed = _align(
        wind_speed_mps, n_points=n_points, name="wind_speed_mps", average_endpoints=True
    )
    wind_dir = _align(
        wind_from_deg, n_points=n_points, name="wind_from_deg", average_endpoints=False
    )
    pressure = _align(pressure_pa, n_points=n_points, name="pressure_pa", average_endpoints=True)
    temperature = _align(
        temperature_c, n_points=n_points, name="temperature_c", average_endpoints=True
    )
    speed_limit = _align(
        speed_limit_kmh, n_points=n_points, name="speed_limit_kmh", average_endpoints=False
    )
    power_limit = _align(
        power_limit_w, n_points=n_points, name="power_limit_w", average_endpoints=False
    )

    segments: list[SegmentConditions] = []
    for i in range(n_points - 1):
        start, end = points[i], points[i + 1]
        length = end.distance_m - start.distance_m
        if length <= 0.0:
            raise ValueError(
                f"trajectory points {i} and {i + 1} are {length:.3f} m apart; "
                "distances must increase strictly"
            )
        dwell = 0.0
        if honour_dwell and end.waypoint is not None and end.waypoint.mandatory_dwell_s:
            dwell = float(end.waypoint.mandatory_dwell_s)
        speed_value = wind_speed[i]
        direction_value = wind_dir[i]
        segments.append(
            SegmentConditions(
                distance_m=length,
                grade=start.grade,
                heading_deg=start.heading_deg,
                wind_speed_mps=0.0 if speed_value is None else speed_value,
                wind_from_deg=0.0 if direction_value is None else direction_value,
                pressure_pa=pressure[i],
                temperature_c=temperature[i],
                generation_w=gen[i],
                speed_limit_kmh=speed_limit[i],
                power_limit_w=power_limit[i],
                dwell_s=dwell,
                label=(end.waypoint.name if end.waypoint is not None else None),
            )
        )
    return segments


def attach_generation(
    segments: Sequence[SegmentConditions],
    generation: Sequence[Any] | float | None,
    *,
    as_energy_wh: bool = False,
) -> list[SegmentConditions]:
    """Copy of ``segments`` with a generation series attached.

    ``generation`` may be :class:`~rwi.contracts.PowerEstimate` objects, plain
    numbers, or a scalar, aligned with the segments or with the segment
    boundaries (``len(segments) + 1``, averaged pairwise). With
    ``as_energy_wh`` the numbers are Wh per segment instead of W.
    """
    n_points = len(segments) + 1
    values = _align(
        [_net_power_w(g) for g in generation] if isinstance(generation, Sequence) else generation,
        n_points=n_points,
        name="generation",
        average_endpoints=True,
    )
    field = "generation_wh" if as_energy_wh else "generation_w"
    other = "generation_w" if as_energy_wh else "generation_wh"
    return [
        segment.model_copy(
            update={field: values[i], other: None, "generation_p10_w": None,
                    "generation_p90_w": None}
        )
        for i, segment in enumerate(segments)
    ]


def scale_generation(
    segments: Sequence[SegmentConditions], factor: float
) -> list[SegmentConditions]:
    """Copy of ``segments`` with every generation figure multiplied by ``factor``."""
    if factor < 0.0:
        raise ValueError("factor must be non-negative")
    out: list[SegmentConditions] = []
    for segment in segments:
        update: dict[str, float] = {}
        if segment.generation_w is not None:
            update["generation_w"] = segment.generation_w * factor
        if segment.generation_wh is not None:
            update["generation_wh"] = segment.generation_wh * factor
        for band in ("generation_p10_w", "generation_p90_w"):
            value = getattr(segment, band)
            if value is not None:
                update[band] = value * factor
        out.append(segment.model_copy(update=update) if update else segment)
    return out


def attach_generation_band(
    segments: Sequence[SegmentConditions],
    p10_w: Sequence[float] | float,
    p90_w: Sequence[float] | float,
) -> list[SegmentConditions]:
    """Copy of ``segments`` with P10/P90 mean array power attached, W.

    Aligned like :func:`attach_generation` (per segment or per boundary). Every
    segment must already carry ``generation_w``, which is taken as the P50; the
    band is checked to bracket it.
    """
    n_points = len(segments) + 1
    lo = _align(p10_w, n_points=n_points, name="p10_w", average_endpoints=True)
    hi = _align(p90_w, n_points=n_points, name="p90_w", average_endpoints=True)
    out: list[SegmentConditions] = []
    for i, segment in enumerate(segments):
        data = segment.model_dump()
        data.update(generation_p10_w=lo[i], generation_p90_w=hi[i])
        out.append(SegmentConditions.model_validate(data))  # re-run the band validator
    return out


def ghi_band_ratios(ghi: Sequence[Measured | None]) -> tuple[list[float], list[float]]:
    """Per-point (P10/P50, P90/P50) ratios from post-processed GHI (ADR 0008 §2).

    A point without a quantile band, or with a P50 of zero (night), gets ratio
    1.0 on both sides: no band is invented where the model gave none.
    """
    lower: list[float] = []
    upper: list[float] = []
    for m in ghi:
        u = m.uncertainty if m is not None else None
        p50 = m.value if m is not None else None
        if p50 is None or p50 <= 0.0 or u is None or u.lower is None or u.upper is None:
            lower.append(1.0)
            upper.append(1.0)
            continue
        lower.append(min(max(u.lower, 0.0) / p50, 1.0))
        upper.append(max(u.upper / p50, 1.0))
    return lower, upper


def generation_band_from_ghi(
    segments: Sequence[SegmentConditions], ghi: Sequence[Measured | None]
) -> list[SegmentConditions]:
    """Segments with a P10/P90 generation band scaled from GHI quantiles.

    Approximation, stated: array power is taken as linear in GHI, so the
    P10/P90 of power are ``generation_w`` times the GHI quantile ratios. That
    ignores the temperature and angle-of-incidence non-linearity; it is a band
    width, not a re-run of the PV model. ``ghi`` is aligned like
    :func:`attach_generation`.
    """
    if any(s.generation_w is None for s in segments):
        raise ValueError("generation_band_from_ghi needs generation_w on every segment")
    lower, upper = ghi_band_ratios(ghi)
    n_points = len(segments) + 1
    lo = _align(lower, n_points=n_points, name="ghi", average_endpoints=True)
    hi = _align(upper, n_points=n_points, name="ghi", average_endpoints=True)
    p50 = [float(s.generation_w or 0.0) for s in segments]
    return attach_generation_band(
        segments,
        [p * (1.0 if r is None else r) for p, r in zip(p50, lo, strict=True)],
        [p * (1.0 if r is None else r) for p, r in zip(p50, hi, strict=True)],
    )


def select_generation_band(
    segments: Sequence[SegmentConditions], band: GenerationBand
) -> list[SegmentConditions]:
    """Segments whose ``generation_w`` is the chosen quantile, band fields cleared.

    Segments without a band keep their ``generation_w`` whatever ``band`` says.
    """
    out: list[SegmentConditions] = []
    for segment in segments:
        if not segment.has_generation_band:
            out.append(segment)
            continue
        value = {
            "p10": segment.generation_p10_w,
            "p50": segment.generation_w,
            "p90": segment.generation_p90_w,
        }[band]
        out.append(
            segment.model_copy(
                update={"generation_w": value, "generation_p10_w": None,
                        "generation_p90_w": None}
            )
        )
    return out


def scale_wind(segments: Sequence[SegmentConditions], factor: float) -> list[SegmentConditions]:
    """Copy of ``segments`` with every wind *speed* multiplied by ``factor``.

    Directions are untouched: this perturbs the magnitude of a forecast wind,
    which makes headwind segments worse and tailwind segments better. It is a
    sensitivity perturbation, not a worst case.
    """
    if factor < 0.0:
        raise ValueError("factor must be non-negative")
    return [
        segment.model_copy(update={"wind_speed_mps": segment.wind_speed_mps * factor})
        for segment in segments
    ]
