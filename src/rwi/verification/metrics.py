"""Deterministic forecast verification scores.

Formulas, bucket choice and every convention are documented in
``docs/science/verification.md``; this module must not drift from that file.

Conventions that matter most:

* error = forecast - observation (positive: forecast too high);
* lead-time buckets are half-open ``[lower, upper)`` in seconds, so a pair
  exactly on a boundary belongs to the UPPER bucket;
* circular variables (wind direction) use differences wrapped to (-180, 180]
  and the vector mean, never the arithmetic one;
* the persistence baseline is the observation at the forecast's initialisation
  time, and it is ``None`` whenever it cannot be built honestly.

All sums use ``math.fsum`` and inputs are put in a canonical order first, so the
result does not depend on the order the archive returned the pairs in.
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise

from rwi.contracts import ForecastObservationPair, VerificationScore
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import TimeWindow, ensure_utc, utc_now
from rwi.core.variables import spec_for

HOUR_S = 3600.0

DEFAULT_LEAD_TIME_BUCKETS_S: tuple[tuple[float, float], ...] = (
    (0.0 * HOUR_S, 3.0 * HOUR_S),
    (3.0 * HOUR_S, 6.0 * HOUR_S),
    (6.0 * HOUR_S, 12.0 * HOUR_S),
    (12.0 * HOUR_S, 24.0 * HOUR_S),
    (24.0 * HOUR_S, 48.0 * HOUR_S),
    (48.0 * HOUR_S, 120.0 * HOUR_S),
)
"""Default lead-time buckets: 0-3, 3-6, 6-12, 12-24, 24-48, 48-120 h, half-open."""

PERSISTENCE_TOLERANCE_S = 1800.0
"""Largest gap between a forecast's init time and the observation used as its persistence."""

MIN_PERSISTENCE_MATCHES = 2
MIN_PERSISTENCE_COVERAGE = 0.5
"""A persistence baseline is reported only if at least this many pairs, and at least this
fraction of the group, have a usable reference observation."""

MIN_RESULTANT_LENGTH = 1e-9
"""Below this mean resultant length a circular mean direction is undefined (e.g. 0 and 180)."""


# ---------------------------------------------------------------------------
# Circular helpers
# ---------------------------------------------------------------------------


def is_circular_variable(variable: str) -> bool:
    """True for registry variables that are angles on a circle.

    Detected from the registry rather than a hardcoded name: unit ``deg`` and a
    description/name that says it is a direction. Today only
    ``wind_direction_10m`` qualifies.
    """
    spec = spec_for(variable)
    if spec.unit != "deg":
        return False
    return "direction" in spec.name or "direction" in spec.description.lower()


def wrap_degrees(angle_deg: float) -> float:
    """Wrap an angle difference to the interval (-180, 180]."""
    wrapped = (angle_deg + 180.0) % 360.0 - 180.0
    return 180.0 if wrapped == -180.0 else wrapped


def circular_difference(forecast_deg: float, observed_deg: float) -> float:
    """Signed shortest-arc forecast minus observation, in (-180, 180] degrees.

    355 forecast vs 5 observed gives -10, not +350.
    """
    return wrap_degrees(forecast_deg - observed_deg)


def circular_mean(angles_deg: Iterable[float]) -> float | None:
    """Mean direction in [0, 360) by the vector (sin/cos) method.

    Returns ``None`` for no data or when the vectors cancel (resultant length
    below ``MIN_RESULTANT_LENGTH``): the mean direction of 0 and 180 degrees
    does not exist, and inventing one would be a false number.
    """
    values = list(angles_deg)
    if not values:
        return None
    sin_mean = math.fsum(math.sin(math.radians(a)) for a in values) / len(values)
    cos_mean = math.fsum(math.cos(math.radians(a)) for a in values) / len(values)
    if math.hypot(sin_mean, cos_mean) < MIN_RESULTANT_LENGTH:
        return None
    mean = math.degrees(math.atan2(sin_mean, cos_mean)) % 360.0
    if round(mean, 9) >= 360.0:
        return 0.0
    return mean


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------


def validate_buckets(
    buckets: Sequence[tuple[float, float]] | None,
) -> tuple[tuple[float, float], ...]:
    """Check and sort lead-time buckets; ``None`` selects the defaults."""
    if buckets is None:
        return DEFAULT_LEAD_TIME_BUCKETS_S
    if not buckets:
        raise ValueError("lead_time_buckets_s must not be empty")
    ordered = tuple(sorted((float(lo), float(hi)) for lo, hi in buckets))
    for lo, hi in ordered:
        if not (math.isfinite(lo) and math.isfinite(hi)) or lo < 0.0 or hi <= lo:
            raise ValueError(f"invalid lead-time bucket ({lo}, {hi}): need 0 <= lower < upper")
    for (_, prev_hi), (next_lo, _) in pairwise(ordered):
        if next_lo < prev_hi:
            raise ValueError("lead-time buckets must not overlap")
    return ordered


def bucket_for(
    lead_time_s: float, buckets: Sequence[tuple[float, float]]
) -> tuple[float, float] | None:
    """The half-open ``[lower, upper)`` bucket containing the lead time, else ``None``."""
    for lo, hi in buckets:
        if lo <= lead_time_s < hi:
            return (lo, hi)
    return None


def bucket_label(bucket: tuple[float, float]) -> str:
    """Human label such as ``6-12h``."""
    return f"{bucket[0] / HOUR_S:g}-{bucket[1] / HOUR_S:g}h"


# ---------------------------------------------------------------------------
# Pair bookkeeping
# ---------------------------------------------------------------------------


def _is_finite_pair(pair: ForecastObservationPair) -> bool:
    return math.isfinite(pair.forecast_value) and math.isfinite(pair.observed_value)


def finite_pairs(pairs: Iterable[ForecastObservationPair]) -> list[ForecastObservationPair]:
    """Drop pairs with NaN/inf values (they cannot be scored)."""
    return [p for p in pairs if _is_finite_pair(p)]


def _canonical_key(p: ForecastObservationPair) -> tuple[object, ...]:
    return (
        p.provider,
        p.model_name or "",
        ensure_utc(p.valid_time_utc),
        ensure_utc(p.forecast_recorded_at_utc),
        p.lead_time_s,
        p.forecast_value,
        p.observed_value,
        p.time_offset_s,
        p.observation_point.latitude_deg,
        p.observation_point.longitude_deg,
    )


def _sorted(pairs: Iterable[ForecastObservationPair]) -> list[ForecastObservationPair]:
    return sorted(pairs, key=_canonical_key)


def _error(pair: ForecastObservationPair, circular: bool) -> float:
    if circular:
        return circular_difference(pair.forecast_value, pair.observed_value)
    return pair.forecast_value - pair.observed_value


def _station_key(pair: ForecastObservationPair) -> tuple[float, float]:
    return (
        round(pair.observation_point.latitude_deg, 3),
        round(pair.observation_point.longitude_deg, 3),
    )


# ---------------------------------------------------------------------------
# Persistence baseline
# ---------------------------------------------------------------------------


class _ObservationPool:
    """Time-ordered observations, per station, for persistence look-ups."""

    def __init__(self, circular: bool) -> None:
        self._circular = circular
        self._raw: dict[object, dict[float, list[float]]] = defaultdict(lambda: defaultdict(list))
        self._times: dict[object, list[float]] = {}
        self._values: dict[object, list[float]] = {}

    def add(self, station: object, when: datetime, value: float) -> None:
        if math.isfinite(value):
            self._raw[station][round(ensure_utc(when).timestamp(), 3)].append(value)

    def _finalise(self) -> None:
        for station, by_time in self._raw.items():
            if station in self._times:
                continue
            times: list[float] = []
            values: list[float] = []
            for t in sorted(by_time):
                vals = sorted(by_time[t])
                merged = circular_mean(vals) if self._circular else math.fsum(vals) / len(vals)
                if merged is not None:
                    times.append(t)
                    values.append(merged)
            self._times[station] = times
            self._values[station] = values

    def lookup(
        self, station: object, target_ts: float, *, before_ts: float, tolerance_s: float
    ) -> float | None:
        """Observation nearest ``target_ts`` (within tolerance), strictly before ``before_ts``."""
        if station not in self._times:
            if station not in self._raw:
                return None
            self._finalise()
        times = self._times[station]
        values = self._values[station]
        idx = bisect.bisect_left(times, target_ts)
        best: tuple[float, float, float] | None = None
        for i in (idx - 1, idx):
            if 0 <= i < len(times) and times[i] < before_ts:
                gap = abs(times[i] - target_ts)
                if gap <= tolerance_s and (best is None or (gap, times[i]) < (best[0], best[1])):
                    best = (gap, times[i], values[i])
        return None if best is None else best[2]


def _pool_from_history(
    history: Sequence[tuple[datetime, float]], circular: bool
) -> _ObservationPool:
    pool = _ObservationPool(circular)
    for when, value in history:
        pool.add(None, when, value)
    return pool


def _pool_from_pairs(pairs: Iterable[ForecastObservationPair], circular: bool) -> _ObservationPool:
    pool = _ObservationPool(circular)
    for p in pairs:
        valid = ensure_utc(p.valid_time_utc)
        obs_time = datetime.fromtimestamp(valid.timestamp() + p.time_offset_s, tz=valid.tzinfo)
        pool.add(_station_key(p), obs_time, p.observed_value)
    return pool


@dataclass(frozen=True)
class _PersistenceResult:
    """Persistence evaluated on the subset of pairs that have a reference observation."""

    matched: int
    total: int
    persistence_mae: float
    model_mae_on_matched: float


def _persistence_eval(
    pairs: Sequence[ForecastObservationPair],
    pool: _ObservationPool,
    *,
    circular: bool,
    use_station: bool,
    tolerance_s: float,
) -> _PersistenceResult | None:
    """Persistence MAE and the model's MAE on the same matched pairs, or ``None``."""
    persistence_abs: list[float] = []
    model_abs: list[float] = []
    for p in pairs:
        valid_ts = ensure_utc(p.valid_time_utc).timestamp()
        obs_ts = valid_ts + p.time_offset_s
        init_ts = valid_ts - p.lead_time_s
        reference = pool.lookup(
            _station_key(p) if use_station else None,
            init_ts,
            before_ts=obs_ts,
            tolerance_s=tolerance_s,
        )
        if reference is None:
            continue
        if circular:
            persistence_abs.append(abs(circular_difference(reference, p.observed_value)))
        else:
            persistence_abs.append(abs(reference - p.observed_value))
        model_abs.append(abs(_error(p, circular)))
    matched = len(persistence_abs)
    total = len(pairs)
    if matched < MIN_PERSISTENCE_MATCHES or matched < MIN_PERSISTENCE_COVERAGE * total:
        return None
    return _PersistenceResult(
        matched=matched,
        total=total,
        persistence_mae=math.fsum(persistence_abs) / matched,
        model_mae_on_matched=math.fsum(model_abs) / matched,
    )


def persistence_baseline(
    pairs: Sequence[ForecastObservationPair],
    *,
    observation_history: Sequence[tuple[datetime, float]] | None = None,
    tolerance_s: float = PERSISTENCE_TOLERANCE_S,
) -> float | None:
    """MAE of the trivial forecast "the value stays what it was at initialisation".

    For each pair the persistence forecast is the observation at the forecast's
    initialisation time ``valid_time - lead_time_s``, taken from

    * ``observation_history`` (ordered or unordered ``(time, value)`` of ONE
      station) when supplied, otherwise
    * the observations carried by the pairs themselves (``observed_value`` at
      ``valid_time + time_offset_s``), matched per station (observation point
      rounded to 0.001 deg).

    The reference must be within ``tolerance_s`` of the init time and strictly
    earlier than the pair's own observation (a lead of zero would otherwise
    "forecast" a value with itself). Pairs with no reference are excluded.

    Returns ``None`` (never a flattering number) when fewer than two pairs, or
    fewer than half of the pairs, have a reference. All pairs must be one
    variable. Circular variables use circular differences.
    """
    if not pairs:
        return None
    variables = {p.variable for p in pairs}
    if len(variables) != 1:
        raise ValueError(f"persistence_baseline needs a single variable, got {sorted(variables)}")
    variable = next(iter(variables))
    circular = is_circular_variable(variable)
    usable = _sorted(finite_pairs(pairs))
    if observation_history is not None:
        pool = _pool_from_history(observation_history, circular)
        result = _persistence_eval(
            usable, pool, circular=circular, use_station=False, tolerance_s=tolerance_s
        )
    else:
        pool = _pool_from_pairs(usable, circular)
        result = _persistence_eval(
            usable, pool, circular=circular, use_station=True, tolerance_s=tolerance_s
        )
    return None if result is None else result.persistence_mae


def skill_score(model_mae: float | None, persistence_mae: float | None) -> float | None:
    """``1 - MAE/persistence_MAE``; positive beats persistence, 0 ties, negative loses.

    ``None`` when either input is missing or the persistence MAE is zero (the
    ratio is undefined; it is not "infinitely bad").
    """
    if model_mae is None or persistence_mae is None or persistence_mae <= 0.0:
        return None
    return 1.0 - model_mae / persistence_mae


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_pairs(
    pairs: Sequence[ForecastObservationPair],
    *,
    variable: str,
    unit: str,
    lead_time_buckets_s: Sequence[tuple[float, float]] | None = None,
    now: datetime | None = None,
    observation_history: Sequence[tuple[datetime, float]] | None = None,
    persistence_tolerance_s: float = PERSISTENCE_TOLERANCE_S,
) -> list[VerificationScore]:
    """Score pairs per (provider, model_name, lead-time bucket).

    Pairs of other variables, with non-finite values, or with a lead time that
    falls in no bucket are ignored (``verify`` reports these in its notes).
    Groups with no pairs are not emitted. Output is sorted by provider, model
    name, then bucket and does not depend on input order.

    ``observation_history`` optionally supplies the observation series (one
    station) used for the persistence baseline; without it the observations of
    the pairs themselves are used (see :func:`persistence_baseline`).
    """
    spec = spec_for(variable)
    if unit != spec.unit:
        raise ValueError(f"{variable} is scored in {spec.unit!r}, not {unit!r}")
    buckets = validate_buckets(lead_time_buckets_s)
    circular = is_circular_variable(variable)
    computed_at = ensure_utc(now) if now is not None else utc_now()

    mine = _sorted(finite_pairs(p for p in pairs if p.variable == variable))
    if not mine:
        return []

    if observation_history is not None:
        pool = _pool_from_history(observation_history, circular)
    else:
        pool = _pool_from_pairs(mine, circular)

    groups: dict[tuple[str, str, float, float], list[ForecastObservationPair]] = defaultdict(list)
    for p in mine:
        bucket = bucket_for(p.lead_time_s, buckets)
        if bucket is not None:
            groups[(p.provider, p.model_name or "", bucket[0], bucket[1])].append(p)

    scores: list[VerificationScore] = []
    for key in sorted(groups):
        provider, model_key, lo, hi = key
        group = groups[key]
        n = len(group)
        errors = [_error(p, circular) for p in group]
        forecasts = [p.forecast_value for p in group]
        observed = [p.observed_value for p in group]

        mae = math.fsum(abs(e) for e in errors) / n
        rmse = math.sqrt(math.fsum(e * e for e in errors) / n)
        bias = math.fsum(errors) / n
        if circular:
            mean_f = circular_mean(forecasts)
            mean_o = circular_mean(observed)
        else:
            mean_f = math.fsum(forecasts) / n
            mean_o = math.fsum(observed) / n

        persistence = _persistence_eval(
            group,
            pool,
            circular=circular,
            use_station=observation_history is None,
            tolerance_s=persistence_tolerance_s,
        )
        persistence_mae = None if persistence is None else persistence.persistence_mae
        skill = (
            None
            if persistence is None
            else skill_score(persistence.model_mae_on_matched, persistence.persistence_mae)
        )

        valid_times = [ensure_utc(p.valid_time_utc) for p in group]
        period = TimeWindow(start_utc=min(valid_times), end_utc=max(valid_times))
        model_name = model_key or None
        persistence_note = (
            "persistence unavailable (too few forecast init-time observations)"
            if persistence is None
            else f"persistence from {persistence.matched}/{persistence.total} pairs"
        )
        provenance = Provenance(
            provider="rwi",
            dataset="verification",
            kind=DataKind.DERIVED,
            retrieved_at_utc=computed_at,
            model_name=model_name,
            processing=(
                f"score_pairs(variable={variable}, bucket={bucket_label((lo, hi))}, "
                f"n={n}, circular={circular})"
            ),
            notes=f"scored provider={provider}; {persistence_note}",
        )
        scores.append(
            VerificationScore(
                variable=variable,
                provider=provider,
                model_name=model_name,
                lead_time_bucket_s=(lo, hi),
                sample_count=n,
                mean_absolute_error=mae,
                root_mean_square_error=rmse,
                bias=bias,
                mean_observed=mean_o,
                mean_forecast=mean_f,
                persistence_mae=persistence_mae,
                skill_vs_persistence=skill,
                unit=unit,
                period=period,
                provenance=provenance,
            )
        )
    return scores


# ---------------------------------------------------------------------------
# CRPS (ADR 0008 §4). See docs/science/verification.md §11.
# ---------------------------------------------------------------------------


def pinball_loss(observation: float, quantile_value: float, level: float) -> float:
    """Quantile (pinball) loss of one quantile forecast, in the variable's unit."""
    if not 0.0 < level < 1.0:
        raise ValueError("quantile level must lie in (0, 1)")
    diff = observation - quantile_value
    return level * diff if diff >= 0.0 else (level - 1.0) * diff


def crps_quantiles(
    observation: float, quantile_values: Sequence[float], levels: Sequence[float]
) -> float:
    """CRPS approximated from quantiles: ``(2/K) * sum(pinball)``, in the variable's unit.

    Exact in the limit of many evenly spaced levels (CRPS = 2 * integral of the
    pinball loss over levels). With three levels (P10/P50/P90) it is a coarse
    approximation, but a consistent one: use it to rank systems scored on the
    same levels, never to compare against an ensemble CRPS.
    """
    if len(quantile_values) != len(levels) or not levels:
        raise ValueError("need one value per quantile level")
    terms = [pinball_loss(observation, q, t) for q, t in zip(quantile_values, levels, strict=True)]
    return 2.0 * math.fsum(terms) / len(levels)


def crps_ensemble(observation: float, members: Sequence[float]) -> float:
    """CRPS of an ensemble, energy form ``E|X-y| - 0.5 E|X-X'|`` (one member: absolute error)."""
    m = sorted(float(x) for x in members)
    n = len(m)
    if n == 0:
        raise ValueError("ensemble has no members")
    term1 = math.fsum(abs(x - observation) for x in m) / n
    # sum_{i,j} |x_i - x_j| = 2 * sum_i (2i - n + 1) x_i for sorted x (0-based i)
    spread = 2.0 * math.fsum((2 * i - n + 1) * x for i, x in enumerate(m))
    return term1 - 0.5 * spread / (n * n)
