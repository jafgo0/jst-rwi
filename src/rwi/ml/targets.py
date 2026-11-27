"""Training/verification targets: the truth hierarchy and derived wind u/v (ADR 0011 §2-3).

* `truth_pairs` asks the archive for pairs one observation provider at a time,
  in priority order, and keeps the first truth found for each forecast value.
  The returned `TruthPairs` records how many rows each source supplied, so a
  BoM-trained model is never presented as an on-board one.
* `uv_pairs` turns matched speed/direction pairs into u/v pairs with
  `rwi.core.wind.met_to_uv`, for the forecast and the observation alike.
* `upstream_value` gives the post-processor the same u/v at inference.

Airmar wind is sensor-height wind (``wind_*_sensor``), never 10 m truth, so the
on-board provider is not a truth source for any wind variable.
"""

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from rwi.contracts import ForecastObservationPair, WeatherStep
from rwi.core.timebase import ensure_utc
from rwi.core.wind import met_to_uv
from rwi.ml.artifacts import TargetKind

__all__ = [
    "BOM_PROVIDER",
    "ONBOARD_PROVIDER",
    "TRUTH_PRIORITY",
    "UV_VARIABLES",
    "PairSource",
    "TruthPairs",
    "latest_cutoff",
    "target_kind_for",
    "truth_pairs",
    "truth_priority",
    "upstream_value",
    "uv_pairs",
]

#: Observation provider of the on-board sensors (`rwi.telemetry.ingest.ONBOARD_PROVIDER`).
ONBOARD_PROVIDER = "jst-onboard"
#: Observation provider of BoM stations (`rwi.providers.bom.PROVIDER_NAME`).
BOM_PROVIDER = "bom"
UV_VARIABLES: tuple[str, str] = ("wind_u_10m", "wind_v_10m")
_SPEED, _DIRECTION = "wind_speed_10m", "wind_direction_10m"

#: Truth sources per variable, highest priority first (ADR 0011 §3). Anything not
#: listed is verified against BoM only.
TRUTH_PRIORITY: Mapping[str, tuple[str, ...]] = {
    "ghi": (ONBOARD_PROVIDER, BOM_PROVIDER),
    "temperature_2m": (ONBOARD_PROVIDER, BOM_PROVIDER),
}
_DEFAULT_PRIORITY: tuple[str, ...] = (BOM_PROVIDER,)
_KIND_OF_PROVIDER: Mapping[str, str] = {
    ONBOARD_PROVIDER: "telemetry",
    BOM_PROVIDER: "bom_observation",
}


class PairSource(Protocol):
    """The slice of `rwi.archive.store.SqliteArchive` this module needs."""

    def pairs(
        self, *, variable: str, observation_provider: str | None = None
    ) -> list[ForecastObservationPair]:
        """Archive pairs for ``variable``, optionally from one observation provider."""
        ...


@dataclass(frozen=True)
class TruthPairs:
    """Pairs for one variable plus the number of rows each truth source supplied."""

    variable: str
    pairs: tuple[ForecastObservationPair, ...]
    #: observation provider of each pair, parallel to ``pairs``
    pair_sources: tuple[str, ...]

    @property
    def sources(self) -> dict[str, int]:
        """Observation provider -> rows; only sources that supplied at least one."""
        return dict(Counter(self.pair_sources))

    def known_at(self, cutoff_utc: datetime) -> "TruthPairs":
        """The pairs issued and verified at or before ``cutoff_utc`` (the training rows)."""
        cutoff = ensure_utc(cutoff_utc)
        keep = [
            i
            for i, p in enumerate(self.pairs)
            if ensure_utc(p.forecast_recorded_at_utc) <= cutoff
            and ensure_utc(p.valid_time_utc) <= cutoff
        ]
        return TruthPairs(
            self.variable,
            tuple(self.pairs[i] for i in keep),
            tuple(self.pair_sources[i] for i in keep),
        )

    @property
    def target_kind(self) -> TargetKind:
        """`TargetKind` of this mix; raises ValueError when there are no rows."""
        return target_kind_for(self.sources)


def truth_priority(variable: str) -> tuple[str, ...]:
    """Observation providers accepted as truth for ``variable``, best first."""
    if variable in UV_VARIABLES:
        return truth_priority(_SPEED)
    return TRUTH_PRIORITY.get(variable, _DEFAULT_PRIORITY)


def target_kind_for(sources: Mapping[str, int]) -> TargetKind:
    """``telemetry``, ``bom_observation`` or ``telemetry+bom_observation`` for a source mix."""
    kinds = {_KIND_OF_PROVIDER[p] for p, n in sources.items() if n > 0}
    if not kinds:
        raise ValueError("no truth rows: cannot name a target kind")
    if kinds == {"telemetry"}:
        return "telemetry"
    if kinds == {"bom_observation"}:
        return "bom_observation"
    return "telemetry+bom_observation"


def _forecast_key(p: ForecastObservationPair) -> tuple[object, ...]:
    """One forecast value: run (provider, model, recorded, lead), valid time and grid point."""
    fp = p.forecast_point
    return (
        p.provider,
        p.model_name,
        ensure_utc(p.forecast_recorded_at_utc),
        p.lead_time_s,
        ensure_utc(p.valid_time_utc),
        fp.latitude_deg,
        fp.longitude_deg,
    )


def _prioritised(
    by_source: Iterable[tuple[str, Sequence[ForecastObservationPair]]],
) -> list[tuple[str, ForecastObservationPair]]:
    """(source, pair) keeping the first source per forecast value, sorted by valid time."""
    seen: set[tuple[object, ...]] = set()
    kept: list[tuple[str, ForecastObservationPair]] = []
    for source, found in by_source:
        for p in found:
            key = _forecast_key(p)
            if key in seen:
                continue
            seen.add(key)
            kept.append((source, p))
    kept.sort(key=lambda sp: (ensure_utc(sp[1].valid_time_utc), sp[1].provider, sp[1].lead_time_s))
    return kept


def truth_pairs(store: PairSource, variable: str) -> TruthPairs:
    """Pairs for ``variable`` against its truth hierarchy (first source per forecast wins)."""
    priority = truth_priority(variable)
    if variable not in UV_VARIABLES:
        kept = _prioritised(
            (src, store.pairs(variable=variable, observation_provider=src)) for src in priority
        )
        return TruthPairs(variable, tuple(p for _, p in kept), tuple(s for s, _ in kept))
    speed = _prioritised(
        (src, store.pairs(variable=_SPEED, observation_provider=src)) for src in priority
    )
    direction = _prioritised(
        (src, store.pairs(variable=_DIRECTION, observation_provider=src)) for src in priority
    )
    # u/v only join speed and direction observed by the same source.
    sources: list[str] = []
    chosen: list[ForecastObservationPair] = []
    for src in priority:
        u, v = uv_pairs([p for s, p in speed if s == src], [p for s, p in direction if s == src])
        picked = u if variable == UV_VARIABLES[0] else v
        chosen.extend(picked)
        sources.extend(src for _ in picked)
    order = sorted(range(len(chosen)), key=lambda i: ensure_utc(chosen[i].valid_time_utc))
    return TruthPairs(variable, tuple(chosen[i] for i in order), tuple(sources[i] for i in order))


def _match_key(p: ForecastObservationPair) -> tuple[object, ...]:
    op = p.observation_point
    return (*_forecast_key(p), op.latitude_deg, op.longitude_deg, p.time_offset_s)


def uv_pairs(
    speed: Sequence[ForecastObservationPair], direction: Sequence[ForecastObservationPair]
) -> tuple[list[ForecastObservationPair], list[ForecastObservationPair]]:
    """(u pairs, v pairs) from speed and direction pairs of the same forecast and observation.

    A speed pair joins a direction pair only when the forecast run, valid time,
    grid point, observation point and time offset are all identical. Unmatched
    rows are dropped; nothing is interpolated.
    """
    dir_by_key = {_match_key(p): p for p in direction if p.variable == _DIRECTION}
    out_u: list[ForecastObservationPair] = []
    out_v: list[ForecastObservationPair] = []
    for s in speed:
        if s.variable != _SPEED:
            continue
        d = dir_by_key.get(_match_key(s))
        if d is None:
            continue
        values = (s.forecast_value, d.forecast_value, s.observed_value, d.observed_value)
        if not all(math.isfinite(x) for x in values):
            continue
        fu, fv = met_to_uv(s.forecast_value, d.forecast_value)
        ou, ov = met_to_uv(s.observed_value, d.observed_value)
        u_update = {"variable": UV_VARIABLES[0], "forecast_value": fu, "observed_value": ou}
        v_update = {"variable": UV_VARIABLES[1], "forecast_value": fv, "observed_value": ov}
        out_u.append(s.model_copy(update=u_update))
        out_v.append(s.model_copy(update=v_update))
    return out_u, out_v


def upstream_value(step: WeatherStep, variable: str) -> float | None:
    """Value of ``variable`` in an upstream step, deriving u/v from speed and direction."""
    if variable not in UV_VARIABLES:
        return step.value(variable)
    speed, direction = step.value(_SPEED), step.value(_DIRECTION)
    if speed is None or direction is None:
        return None
    u, v = met_to_uv(speed, direction)
    return u if variable == UV_VARIABLES[0] else v


def latest_cutoff(*cutoffs: datetime) -> datetime:
    """The later training cutoff of a combined band (the one that matters for leakage)."""
    return max(ensure_utc(c) for c in cutoffs)
