"""Ensemble cold-front passage time per control stop and its alert rule (ADR 0012).

The deterministic surface detector `rwi.alerts.synoptic.detect_front_passages`
runs unchanged on every member of an ensemble at one stop. Each member's front
is its earliest passage in ``[now, now + horizon]``:

* ``probability`` = members with a front / members evaluated (a member without
  a front stays in the denominator);
* P10/P50/P90 of the passage time are linear quantiles of the passage epochs of
  the members *with* a front, i.e. conditional on a front, and only when at
  least `MIN_MEMBERS_FOR_QUANTILES` members have one.

A deterministic run (ECMWF AIFS) goes through the same detector and is reported
beside the ensemble, never mixed into its quantiles.

The detector is uncalibrated (REQUIREMENTS.md #14): the probability is that of a
*model* front under these rules, not a verified frequency of observed fronts.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Final

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from rwi.alerts.rules import _Builder
from rwi.alerts.synoptic import (
    DEFAULT_THRESHOLDS,
    FrontSignature,
    RoutePointSeries,
    SynopticThresholds,
    detect_front_passages,
)
from rwi.contracts import Alert, AlertCategory, AlertSeverity, EnsembleForecast, WeatherSeries
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import TimeWindow, ensure_utc

__all__ = [
    "DEFAULT_FRONT_STOPS",
    "FRONT_CAVEAT",
    "FRONT_LOOKBACK_H",
    "MIN_MEMBERS_FOR_QUANTILES",
    "DeterministicPassage",
    "FrontOutlook",
    "StopFrontPassage",
    "earliest_passage",
    "front_alerts",
    "stop_front_passage",
]

#: Smallest member count for which P10 < P50 < P90 can differ: a statistical floor.
MIN_MEMBERS_FOR_QUANTILES = 3
QUANTILE_METHOD: Final = "linear"
#: 24 h trough detrend plus the +-3 h detector half-window (SynopticThresholds).
FRONT_LOOKBACK_H = 30.0
#: South Australian stops where mid-latitude fronts reach the route (ADR 0012 section 2).
DEFAULT_FRONT_STOPS: tuple[str, ...] = ("Coober Pedy", "Glendambo", "Port Augusta")

FRONT_CAVEAT = (
    "Model-front probability from uncalibrated surface rules (trough, NW->SW wind "
    "shift, 24 h cooling, dewpoint drop) applied per ensemble member; not a verified "
    "frequency of observed fronts. Ensemble values are interpolated hourly from 3-6 h "
    "native steps. Passage quantiles are conditional on a front. Not meaningful for "
    "the tropical north of the route."
)


class DeterministicPassage(BaseModel):
    """First passage in a deterministic run, or why there is none."""

    model_config = ConfigDict(frozen=True)

    model_name: str
    passage_time_utc: datetime | None = None
    strength: str | None = None
    criteria_met: tuple[str, ...] = ()
    provenance: Provenance | None = None
    error: str | None = None


class StopFrontPassage(BaseModel):
    """Ensemble front probability and conditional passage quantiles at one stop."""

    model_config = ConfigDict(frozen=True)

    name: str
    route_distance_m: float
    point: GeoPoint
    model_name: str | None = None
    members_total: int = 0
    members_evaluated: int = 0
    members_with_front: int = 0
    probability: float | None = Field(
        default=None, description="members with a front / members evaluated"
    )
    p10_utc: datetime | None = None
    p50_utc: datetime | None = None
    p90_utc: datetime | None = None
    quantiles_conditional_on_front: bool = True
    quantile_reason: str | None = None
    member_passages_utc: list[datetime] = Field(default_factory=list)
    deterministic: DeterministicPassage | None = None
    provenance: Provenance | None = None
    error: str | None = None


class FrontOutlook(BaseModel):
    """Front passages for the requested stops, and the alerts they raise."""

    model_config = ConfigDict(frozen=True)

    generated_at_utc: datetime
    window: TimeWindow
    horizon_h: float
    ensemble_model: str
    deterministic_model: str
    stops: list[StopFrontPassage]
    alerts: list[Alert] = Field(default_factory=list)
    alerts_configured: bool = False
    caveat: str = FRONT_CAVEAT


def earliest_passage(
    rp: RoutePointSeries,
    *,
    now: datetime,
    horizon: timedelta,
    thresholds: SynopticThresholds = DEFAULT_THRESHOLDS,
) -> FrontSignature | None:
    """Earliest detected front passage in ``[now, now + horizon]``, or None."""
    now = ensure_utc(now)
    end = now + horizon
    hits = [
        f
        for f in detect_front_passages(rp, thresholds, now=now)
        if now <= ensure_utc(f.passage_time_utc) <= end
    ]
    return min(hits, key=lambda f: f.passage_time_utc) if hits else None


def _quantiles(times: Sequence[datetime]) -> tuple[datetime, datetime, datetime]:
    epochs = np.array([ensure_utc(t).timestamp() for t in times], dtype=float)
    q = np.quantile(epochs, [0.1, 0.5, 0.9], method=QUANTILE_METHOD)
    p10, p50, p90 = (datetime.fromtimestamp(float(x), tz=UTC) for x in q)
    return p10, p50, p90


def _deterministic(
    name: str,
    dist: float,
    point: GeoPoint,
    series: WeatherSeries | None,
    *,
    model_name: str,
    error: str | None,
    now: datetime,
    horizon: timedelta,
    thresholds: SynopticThresholds,
) -> DeterministicPassage:
    if series is None:
        return DeterministicPassage(model_name=model_name, error=error or "no data")
    rp = RoutePointSeries(name, dist, point, series)
    f = earliest_passage(rp, now=now, horizon=horizon, thresholds=thresholds)
    if f is None:
        return DeterministicPassage(model_name=model_name)
    return DeterministicPassage(
        model_name=model_name,
        passage_time_utc=f.passage_time_utc,
        strength=f.strength,
        criteria_met=f.criteria_met,
        provenance=f.provenance,
    )


def stop_front_passage(
    name: str,
    route_distance_m: float,
    point: GeoPoint,
    ensemble: EnsembleForecast | None,
    *,
    now: datetime,
    horizon: timedelta,
    ensemble_error: str | None = None,
    deterministic: WeatherSeries | None = None,
    deterministic_model: str = "ecmwf_aifs025_single",
    deterministic_error: str | None = None,
    thresholds: SynopticThresholds = DEFAULT_THRESHOLDS,
) -> StopFrontPassage:
    """Front probability and conditional P10/P50/P90 passage at one stop (rwi/fronts, DERIVED)."""
    now = ensure_utc(now)
    det = _deterministic(
        name,
        route_distance_m,
        point,
        deterministic,
        model_name=deterministic_model,
        error=deterministic_error,
        now=now,
        horizon=horizon,
        thresholds=thresholds,
    )
    if ensemble is None:
        return StopFrontPassage(
            name=name,
            route_distance_m=route_distance_m,
            point=point,
            deterministic=det,
            quantile_reason="no ensemble",
            error=ensemble_error or "no ensemble",
        )
    evaluated = [m for m in ensemble.members if m.steps]
    passages: list[datetime] = []
    for member in evaluated:
        f = earliest_passage(
            RoutePointSeries(name, route_distance_m, point, member),
            now=now,
            horizon=horizon,
            thresholds=thresholds,
        )
        if f is not None:
            passages.append(ensure_utc(f.passage_time_utc))
    passages.sort()
    probability = len(passages) / len(evaluated) if evaluated else None
    p10 = p50 = p90 = None
    reason: str | None = None
    if not evaluated:
        reason = "no ensemble member had data"
    elif len(passages) < MIN_MEMBERS_FOR_QUANTILES:
        reason = (
            f"{len(passages)} of {len(evaluated)} members have a front; "
            f"quantiles need at least {MIN_MEMBERS_FOR_QUANTILES}"
        )
    else:
        p10, p50, p90 = _quantiles(passages)
    init = evaluated[0].model_init_time_utc if evaluated else None
    provenance = Provenance(
        provider="rwi",
        dataset="fronts",
        kind=DataKind.DERIVED,
        retrieved_at_utc=ensure_utc(ensemble.retrieved_at_utc),
        valid_time_utc=p50,
        model_name=ensemble.model_name,
        model_init_time_utc=init,
        point=point,
        processing=(
            "ensemble_front_passage: synoptic_front_rules per member, earliest passage in "
            f"[now, now+{horizon.total_seconds() / 3600:g} h], {QUANTILE_METHOD} quantiles "
            f"(input: {ensemble.provider}/ensemble, {len(evaluated)} members)"
        ),
    )
    return StopFrontPassage(
        name=name,
        route_distance_m=route_distance_m,
        point=point,
        model_name=ensemble.model_name,
        members_total=len(ensemble.members),
        members_evaluated=len(evaluated),
        members_with_front=len(passages),
        probability=probability,
        p10_utc=p10,
        p50_utc=p50,
        p90_utc=p90,
        quantile_reason=reason,
        member_passages_utc=passages,
        deterministic=det,
        provenance=provenance,
    )


def _fmt(t: datetime | None) -> str:
    return "n/a" if t is None else ensure_utc(t).strftime("%Y-%m-%d %H:%MZ")


def front_alerts(
    stops: Sequence[StopFrontPassage],
    *,
    watch: float | None,
    warning: float | None,
    now: datetime,
) -> list[Alert]:
    """WATCH/WARNING per stop from the front probability; none unless both thresholds are set."""
    if watch is None or warning is None:
        return []
    if not 0.0 < watch <= warning <= 1.0:
        raise ValueError(
            f"front alert thresholds need 0 < watch <= warning <= 1, got {watch}, {warning}"
        )
    builder = _Builder(ensure_utc(now))
    for s in stops:
        if s.probability is None:
            continue
        if s.probability >= warning:
            severity = AlertSeverity.WARNING
        elif s.probability >= watch:
            severity = AlertSeverity.WATCH
        else:
            continue
        window = (
            TimeWindow(start_utc=s.p10_utc, end_utc=s.p90_utc)
            if s.p10_utc is not None and s.p90_utc is not None and s.p10_utc < s.p90_utc
            else None
        )
        evidence = [
            f"{s.members_with_front}/{s.members_evaluated} {s.model_name} members show a front",
            f"passage P10 {_fmt(s.p10_utc)}, P50 {_fmt(s.p50_utc)}, P90 {_fmt(s.p90_utc)} "
            "(conditional on a front)",
        ]
        if s.deterministic is not None and s.deterministic.passage_time_utc is not None:
            evidence.append(
                f"{s.deterministic.model_name}: passage {_fmt(s.deterministic.passage_time_utc)}"
            )
        builder.add(
            alert_id=f"front-{s.name.lower().replace(' ', '-')}",
            severity=severity,
            category=AlertCategory.WIND,
            headline=f"Cold front likely at {s.name} ({s.probability:.0%} of members)",
            detail=(
                f"Ensemble front probability {s.probability:.0%} at {s.name}; median passage "
                f"{_fmt(s.p50_utc)}. Expect a NW->SW wind shift, gusts and a cloud band. "
                + FRONT_CAVEAT
            ),
            evidence=evidence,
            recommended_action="Brief the team on the wind shift timing; humans decide.",
            valid_window=window,
            distance_along_route_m=s.route_distance_m,
            location=s.point,
        )
    return builder.items
