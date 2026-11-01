"""Quality checks on weather values and series.

Contract (docs/CONTRACTS.md section 3):

    check_series(series, *, now=None, max_age_s=None) -> WeatherSeries
    check_value(value, *, previous=None)              -> Measured
    summarise(series)                                  -> QualityReport

Principle: **flags only**. A check never deletes a step, never changes a value
and never "repairs" an inconsistency; it sets ``Measured.quality`` (to the most
severe applicable flag) and, in `analyse_series`, explains itself in a
`QualityReport`. The layer above decides what to do with flagged data. Checks are
idempotent: re-running them on their own output changes nothing (a previous
STALE flag is re-evaluated against the new ``now``; every other existing flag is
kept unless a more severe one applies).

Checks and the flag each sets
-----------------------------
=========================  ===================  ====================================
check                      flag                 rule
=========================  ===================  ====================================
missing                    MISSING              value is None
range                      OUT_OF_RANGE         outside the registry's min/max
rate                       RATE_IMPLAUSIBLE     |dv|/dt > ``max_plausible_rate_per_hour``
                                                (dt clamped to >= 15 min; skipped where the
                                                limit is vacuous, i.e. >= the variable's span)
dew_point_le_temperature   SENSOR_SUSPECT       dew point > temperature + 0.1 degC (both flagged)
gust_ge_mean_wind          SENSOR_SUSPECT       gust < mean wind - 0.1 m/s (both flagged)
dhi_le_ghi                 SENSOR_SUSPECT       DHI > GHI + 1 W/m2 (both flagged)
ghi_zero_at_night          SENSOR_SUSPECT       provider is_day == 0 (here and at the previous
                                                step, when there is one) yet GHI > 5 W/m2 (GHI)
duplicate_timestamp        SENSOR_SUSPECT       two steps share a valid time (all copies flagged)
non_monotonic_timestamp    SENSOR_SUSPECT       a step is earlier than its predecessor
frozen                     SENSOR_SUSPECT       >= 6 identical usable values of a normally
                                                varying variable (temperature, dew point, RH,
                                                pressure, wind direction, ...)
stale                      STALE                newest data older than ``max_age_s``
=========================  ===================  ====================================

Notes on judgement calls
------------------------
* ``ghi_zero_at_night``: Open-Meteo's ``is_day`` is instantaneous while its GHI is
  the mean of the PRECEDING hour, so the first ``is_day == 0`` hour after sunset
  legitimately has GHI > 0 (seen in real data: GHI 2 W/m2 at the first night hour).
  The rule therefore needs ``is_day == 0`` at two consecutive steps (or a lone first
  step) AND GHI above a 5 W/m2 allowance.
* Staleness is judged on the *newest* data of the series: for observations the newest
  observation time, for every other kind the retrieval time (a forecast for a future
  valid time is not "old"). If the newest is older than the limit every value is
  flagged STALE; older steps of a fresh observation history are not.
* Default ``max_age_s`` comes from `rwi.config.Settings` (observations:
  ``max_observation_age_s``; forecasts/analyses: ``max_forecast_age_s``).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from rwi.config import get_settings
from rwi.contracts import WeatherSeries, WeatherStep
from rwi.core.provenance import DataKind, Measured, QualityFlag
from rwi.core.timebase import ensure_utc, utc_now
from rwi.core.variables import spec_for
from rwi.quality.report import QualityIssue, QualityReport

__all__ = [
    "DEW_POINT_TOLERANCE_C",
    "DHI_TOLERANCE_W_M2",
    "FROZEN_MIN_STEPS",
    "FROZEN_VARIABLES",
    "GUST_TOLERANCE_MS",
    "MIN_RATE_INTERVAL_H",
    "NIGHT_GHI_ALLOWANCE_W_M2",
    "analyse_series",
    "check_series",
    "check_value",
    "summarise",
]

DEW_POINT_TOLERANCE_C = 0.1
GUST_TOLERANCE_MS = 0.1
DHI_TOLERANCE_W_M2 = 1.0
NIGHT_GHI_ALLOWANCE_W_M2 = 5.0
MIN_RATE_INTERVAL_H = 0.25
FROZEN_MIN_STEPS = 6
FROZEN_VARIABLES = frozenset(
    {
        "temperature_2m",
        "apparent_temperature",
        "dew_point_2m",
        "relative_humidity_2m",
        "pressure_msl",
        "surface_pressure",
        "wind_direction_10m",
        "soil_temperature_0cm",
    }
)
"""Variables that cannot plausibly be bit-identical for 6+ consecutive steps. Wind speed,
cloud, precipitation, irradiance, is_day and weather_code are excluded: calm, overcast, dry
and night all produce legitimate constant runs."""

_SEVERITY: dict[QualityFlag, int] = {
    QualityFlag.MISSING: 5,
    QualityFlag.OUT_OF_RANGE: 4,
    QualityFlag.SENSOR_SUSPECT: 3,
    QualityFlag.RATE_IMPLAUSIBLE: 2,
    QualityFlag.STALE: 1,
}


def _rank(flag: QualityFlag) -> int:
    return _SEVERITY.get(flag, 0)


def _merge(existing: QualityFlag, new: QualityFlag) -> QualityFlag:
    return new if _rank(new) > _rank(existing) else existing


def _rate_applies(variable: str) -> float | None:
    """Rate limit in unit/hour, or None when absent or vacuous (>= variable span)."""
    spec = spec_for(variable)
    rate = spec.max_plausible_rate_per_hour
    if rate is None or rate >= (spec.maximum - spec.minimum):
        return None
    return rate


def _rate_exceeded(
    variable: str, prev_value: float, prev_time: datetime, value: float, time: datetime
) -> str | None:
    rate = _rate_applies(variable)
    if rate is None:
        return None
    dt_h = (ensure_utc(time) - ensure_utc(prev_time)).total_seconds() / 3600.0
    if dt_h <= 0:
        return None
    observed = abs(value - prev_value) / max(dt_h, MIN_RATE_INTERVAL_H)
    if observed > rate:
        return (
            f"{variable} changed {value - prev_value:+.3f} {spec_for(variable).unit} in "
            f"{dt_h * 60:.0f} min ({observed:.1f}/h > limit {rate:g}/h)"
        )
    return None


def _in_range(variable: str, value: float) -> bool:
    spec = spec_for(variable)
    return spec.minimum <= value <= spec.maximum


def _value_age_s(m: Measured, now: datetime) -> float:
    prov = m.provenance
    ref = prov.observation_time_utc if prov.kind is DataKind.OBSERVATION else None
    return (now - (ref or prov.retrieved_at_utc)).total_seconds()


# ---------------------------------------------------------------------------
# Public: single value
# ---------------------------------------------------------------------------


def check_value(
    value: Measured,
    *,
    previous: Measured | None = None,
    now: datetime | None = None,
    max_age_s: float | None = None,
) -> Measured:
    """Return a flagged copy of ``value`` (missing, range, rate; optionally staleness).

    ``previous`` is the preceding value of the *same* variable; the rate check uses
    the two values' reference times (observation time, else valid time, else
    retrieval time). ``now``/``max_age_s`` are optional extensions of the contract:
    only when ``max_age_s`` is given is age checked (observations by observation
    time, everything else by retrieval time). The input is never mutated.
    """
    flag: QualityFlag = QualityFlag.OK if value.quality is QualityFlag.STALE else value.quality
    if value.value is None:
        flag = _merge(flag, QualityFlag.MISSING)
    elif not _in_range(value.variable, value.value):
        flag = _merge(flag, QualityFlag.OUT_OF_RANGE)
    elif (
        previous is not None
        and previous.value is not None
        and previous.variable == value.variable
        and _in_range(value.variable, previous.value)
        and _rate_exceeded(
            value.variable,
            previous.value,
            previous.provenance.reference_time_utc,
            value.value,
            value.provenance.reference_time_utc,
        )
    ):
        flag = _merge(flag, QualityFlag.RATE_IMPLAUSIBLE)
    if max_age_s is not None:
        current = ensure_utc(now) if now else utc_now()
        if _value_age_s(value, current) > max_age_s:
            flag = _merge(flag, QualityFlag.STALE)
    return value if flag is value.quality else value.model_copy(update={"quality": flag})


# ---------------------------------------------------------------------------
# Public: series
# ---------------------------------------------------------------------------


def analyse_series(
    series: WeatherSeries,
    *,
    now: datetime | None = None,
    max_age_s: float | None = None,
) -> tuple[WeatherSeries, QualityReport]:
    """`check_series` plus the report explaining every finding."""
    current = ensure_utc(now) if now else utc_now()
    steps = series.steps
    flags: dict[tuple[int, str], QualityFlag] = {}
    for i, step in enumerate(steps):
        for name, m in step.values.items():
            flags[(i, name)] = QualityFlag.OK if m.quality is QualityFlag.STALE else m.quality
    issues: list[QualityIssue] = []

    def flag(i: int, variable: str, new: QualityFlag, check: str, detail: str) -> None:
        issues.append(
            QualityIssue(
                check=check,
                flag=new,
                variable=variable,
                time_utc=steps[i].valid_time_utc,
                detail=detail,
            )
        )
        flags[(i, variable)] = _merge(flags[(i, variable)], new)

    def series_issue(new: QualityFlag, check: str, detail: str) -> None:
        issues.append(QualityIssue(check=check, flag=new, variable=None, detail=detail))

    # -- 1. missing / range ---------------------------------------------------
    for i, step in enumerate(steps):
        for name, m in step.values.items():
            if m.value is None:
                flag(i, name, QualityFlag.MISSING, "missing", f"{name} has no value")
            elif not _in_range(name, m.value):
                spec = spec_for(name)
                flag(
                    i,
                    name,
                    QualityFlag.OUT_OF_RANGE,
                    "range",
                    f"{name}={m.value:g} outside [{spec.minimum:g}, {spec.maximum:g}] {spec.unit}",
                )

    # -- 2. timestamps: duplicates and ordering -------------------------------
    by_time: dict[datetime, list[int]] = defaultdict(list)
    for i, step in enumerate(steps):
        by_time[ensure_utc(step.valid_time_utc)].append(i)
    duplicate_idx: set[int] = set()
    for t, idxs in by_time.items():
        if len(idxs) > 1:
            duplicate_idx.update(idxs)
            series_issue(
                QualityFlag.SENSOR_SUSPECT,
                "duplicate_timestamp",
                f"{len(idxs)} steps share valid time {t.isoformat()}",
            )
            for i in idxs:
                for name in steps[i].values:
                    flag(
                        i,
                        name,
                        QualityFlag.SENSOR_SUSPECT,
                        "duplicate_timestamp",
                        f"{name}: duplicated timestamp {t.isoformat()}",
                    )
    for i in range(1, len(steps)):
        if ensure_utc(steps[i].valid_time_utc) < ensure_utc(steps[i - 1].valid_time_utc):
            series_issue(
                QualityFlag.SENSOR_SUSPECT,
                "non_monotonic_timestamp",
                f"step {i} ({steps[i].valid_time_utc.isoformat()}) precedes step {i - 1}",
            )
            for name in steps[i].values:
                flag(
                    i,
                    name,
                    QualityFlag.SENSOR_SUSPECT,
                    "non_monotonic_timestamp",
                    f"{name}: timestamp earlier than the previous step",
                )

    order = sorted(
        (i for i in range(len(steps)) if i not in duplicate_idx),
        key=lambda i: ensure_utc(steps[i].valid_time_utc),
    )

    # -- 3. rate of change (against the previous plausible value) --------------
    variables = sorted({n for s in steps for n in s.values})
    for name in variables:
        prev: tuple[float, datetime] | None = None
        for i in order:
            cur = steps[i].values.get(name)
            if cur is None or cur.value is None or not _in_range(name, cur.value):
                continue
            t = steps[i].valid_time_utc
            if prev is not None:
                msg = _rate_exceeded(name, prev[0], prev[1], cur.value, t)
                if msg:
                    flag(i, name, QualityFlag.RATE_IMPLAUSIBLE, "rate", msg)
            prev = (cur.value, t)

    # -- 4. internal consistency (flag only, never correct) ---------------------
    def num(i: int, name: str) -> float | None:
        m = steps[i].values.get(name)
        return m.value if m is not None else None

    for pos, i in enumerate(order):
        t_air, t_dew = num(i, "temperature_2m"), num(i, "dew_point_2m")
        if t_air is not None and t_dew is not None and t_dew > t_air + DEW_POINT_TOLERANCE_C:
            detail = f"dew point {t_dew:g} degC exceeds air temperature {t_air:g} degC"
            for name in ("dew_point_2m", "temperature_2m"):
                flag(i, name, QualityFlag.SENSOR_SUSPECT, "dew_point_le_temperature", detail)
        mean, gust = num(i, "wind_speed_10m"), num(i, "wind_gusts_10m")
        if mean is not None and gust is not None and gust < mean - GUST_TOLERANCE_MS:
            detail = f"gust {gust:g} m/s is below mean wind {mean:g} m/s"
            flag(i, "wind_gusts_10m", QualityFlag.SENSOR_SUSPECT, "gust_ge_mean_wind", detail)
            flag(i, "wind_speed_10m", QualityFlag.SENSOR_SUSPECT, "gust_ge_mean_wind", detail)
        ghi, dhi = num(i, "ghi"), num(i, "dhi")
        if ghi is not None and dhi is not None and dhi > ghi + DHI_TOLERANCE_W_M2:
            detail = f"DHI {dhi:g} W/m2 exceeds GHI {ghi:g} W/m2"
            flag(i, "dhi", QualityFlag.SENSOR_SUSPECT, "dhi_le_ghi", detail)
            flag(i, "ghi", QualityFlag.SENSOR_SUSPECT, "dhi_le_ghi", detail)
        is_day = num(i, "is_day")
        if ghi is not None and ghi > NIGHT_GHI_ALLOWANCE_W_M2 and is_day == 0:
            prev_is_day = num(order[pos - 1], "is_day") if pos > 0 else 0.0
            if prev_is_day is None or prev_is_day == 0:
                flag(
                    i,
                    "ghi",
                    QualityFlag.SENSOR_SUSPECT,
                    "ghi_zero_at_night",
                    f"GHI {ghi:g} W/m2 while the provider's own is_day is 0",
                )

    # -- 5. frozen series -------------------------------------------------------
    for name in sorted(FROZEN_VARIABLES.intersection(variables)):
        usable = [
            (i, steps[i].values[name].value)
            for i in order
            if name in steps[i].values
            and steps[i].values[name].value is not None
            and _in_range(name, steps[i].values[name].value)  # type: ignore[arg-type]
        ]
        if len(usable) < FROZEN_MIN_STEPS:
            continue
        distinct = {v for _, v in usable}
        spec = spec_for(name)
        if len(distinct) == 1 and next(iter(distinct)) not in (spec.minimum, spec.maximum):
            const = next(iter(distinct))
            series_issue(
                QualityFlag.SENSOR_SUSPECT,
                "frozen",
                f"{name} is constant at {const:g} over {len(usable)} steps",
            )
            for i, _ in usable:
                flag(
                    i,
                    name,
                    QualityFlag.SENSOR_SUSPECT,
                    "frozen",
                    f"{name} identical ({const:g}) across {len(usable)} steps",
                )

    # -- 6. staleness (judged on the newest data) --------------------------------
    limit = max_age_s if max_age_s is not None else _default_max_age_s(series.kind)
    if limit is not None and steps:
        if series.kind is DataKind.OBSERVATION:
            reference = max(ensure_utc(s.valid_time_utc) for s in steps)
            what = "newest observation"
        else:
            reference = ensure_utc(series.retrieved_at_utc)
            what = "retrieval"
        age = (current - reference).total_seconds()
        if age > limit:
            series_issue(
                QualityFlag.STALE, "stale", f"{what} is {age:.0f} s old, limit {limit:g} s"
            )
            for i, step in enumerate(steps):
                for name in step.values:
                    flags[(i, name)] = _merge(flags[(i, name)], QualityFlag.STALE)

    # -- build the flagged copy ------------------------------------------------------
    new_steps: list[WeatherStep] = []
    for i, step in enumerate(steps):
        values: dict[str, Measured] = {}
        for name, m in step.values.items():
            f = flags[(i, name)]
            values[name] = m if f is m.quality else m.model_copy(update={"quality": f})
        new_steps.append(WeatherStep(valid_time_utc=step.valid_time_utc, values=values))
    checked = series.model_copy(update={"steps": new_steps})
    return checked, _report(checked, current, issues)


def check_series(
    series: WeatherSeries,
    *,
    now: datetime | None = None,
    max_age_s: float | None = None,
) -> WeatherSeries:
    """Flagged copy of ``series``: nothing deleted, no value changed (see module doc)."""
    return analyse_series(series, now=now, max_age_s=max_age_s)[0]


def summarise(series: WeatherSeries) -> QualityReport:
    """Count the flags already on ``series`` (per flag and per variable).

    Pure bookkeeping on existing flags - it does not run any check. Use
    `analyse_series` to get the explanatory ``issues`` list.
    """
    return _report(series, utc_now(), [])


def _default_max_age_s(kind: DataKind) -> float | None:
    s = get_settings()
    if kind is DataKind.OBSERVATION:
        return s.max_observation_age_s
    if kind in (DataKind.FORECAST, DataKind.ANALYSIS):
        return s.max_forecast_age_s
    return None


def _report(
    series: WeatherSeries, checked_at: datetime, issues: list[QualityIssue]
) -> QualityReport:
    counts: dict[str, int] = {f.value: 0 for f in QualityFlag}
    per_var: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    usable = 0
    total = 0
    for step in series.steps:
        for name, m in step.values.items():
            total += 1
            counts[m.quality.value] += 1
            per_var[name][m.quality.value] += 1
            usable += 1 if m.is_usable else 0
    return QualityReport(
        provider=series.provider,
        dataset=series.dataset,
        kind=series.kind,
        checked_at_utc=checked_at,
        step_count=len(series.steps),
        value_count=total,
        flag_counts=counts,
        by_variable={k: dict(v) for k, v in sorted(per_var.items())},
        usable_count=usable,
        usable_fraction=(usable / total) if total else 0.0,
        issues=issues,
    )
