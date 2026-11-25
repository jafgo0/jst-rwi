"""Tool adapters for the meteorologist layer.

Each tool is a pure function that takes an already-computed structured
output from one engine and packages it into a :class:`ToolResult` the
composer can consume. The tools RECKON NOTHING - they neither aggregate
across engines nor invent a value. When the engine returned nothing, the
tool returns :meth:`ToolResult.unavailable` with the reason.

The split between "fetch" (done by the API router) and "shape" (done
here) exists so the composer is fully testable against fixtures: unit
tests feed it :class:`ToolResult` objects directly, with no network.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel, ConfigDict

from rwi.contracts import (
    Alert,
    AlertSeverity,
    EnsembleSummary,
    ModelDisagreement,
    PowerEstimate,
    RouteWeatherForecast,
    SolarDayEvents,
    VerificationScore,
    WeatherSeries,
)
from rwi.core.position import ActiveState
from rwi.core.timebase import TimeWindow
from rwi.meteorologist.contract import Citation
from rwi.meteorologist.self_knowledge import SelfKnowledgeSummary

if TYPE_CHECKING:  # typing only: the layer receives the engine output, never runs it
    from rwi.alerts.synoptic import SynopticAnalysis

T = TypeVar("T")


@dataclass(frozen=True)
class ToolResult[T]:
    """One tool invocation's output.

    ``value`` is ``None`` iff the tool could not answer. ``reason`` is
    filled in both cases - as the explanation of why there is no value,
    or as a one-line description of what the value contains.
    ``citation`` is ``None`` only when ``value`` is ``None``: a value is
    always traceable.
    """

    value: T | None
    reason: str
    citation: Citation | None

    @property
    def available(self) -> bool:
        return self.value is not None

    @classmethod
    def unavailable(cls, reason: str) -> ToolResult[T]:
        return cls(value=None, reason=reason, citation=None)

    @classmethod
    def ok(cls, value: T, reason: str, citation: Citation) -> ToolResult[T]:
        return cls(value=value, reason=reason, citation=citation)


def _state_citation(state: ActiveState) -> Citation:
    fix_time = state.fix.received_at_utc if state.fix is not None else None
    return Citation(
        id="state",
        tool="where_and_when",
        source_module="rwi.core.state",
        description=f"active state in mode {state.mode.value}",
        valid_time_utc=fix_time,
        retrieved_at_utc=fix_time,
    )


def where_and_when(state: ActiveState) -> ToolResult[ActiveState]:
    """Report the active position/time state.

    Returns the whole :class:`ActiveState` so downstream tools can read
    position, heading, selected time and suspect-fix state in one place.
    When no position is available the tool reports unavailable with the
    mode as the reason - the state object itself is never fabricated.
    """
    if state.fix is None:
        return ToolResult.unavailable(
            f"ingen position tillgänglig (mode {state.mode.value})"
        )
    return ToolResult.ok(
        state,
        f"aktiv position i mode {state.mode.value}",
        _state_citation(state),
    )


def solar_day(events: SolarDayEvents | None) -> ToolResult[SolarDayEvents]:
    """Shape a :class:`SolarDayEvents` as a tool result.

    ``None`` means the caller could not compute it (typically because
    there is no active position); polar day/night are still valid
    results and come through with their flags intact.
    """
    if events is None:
        return ToolResult.unavailable(
            "soldag-händelser saknas: ingen position, ingen kalkyl"
        )
    citation = Citation(
        id="solar_day",
        tool="solar_day",
        source_module="rwi.solar.geometry",
        description=f"soldag {events.local_date} vid {events.point.latitude_deg:.3f}, "
        f"{events.point.longitude_deg:.3f}",
        valid_time_utc=events.solar_noon_utc,
    )
    return ToolResult.ok(events, f"soldag {events.local_date}", citation)


def route_weather_ahead(
    forecast: RouteWeatherForecast | None,
) -> ToolResult[RouteWeatherForecast]:
    """Shape a :class:`RouteWeatherForecast` as a tool result."""
    if forecast is None or not forecast.samples:
        return ToolResult.unavailable(
            "ingen ruttprognos: antingen saknas rutt/position eller provider svarade inte"
        )
    valid_time = forecast.samples[0].trajectory_point.eta_utc
    citation = Citation(
        id="route_weather",
        tool="route_weather_ahead",
        source_module="rwi.api.integration",
        description=f"ruttprognos från {forecast.provider}, "
        f"{len(forecast.samples)} punkter",
        valid_time_utc=valid_time,
        retrieved_at_utc=forecast.generated_at_utc,
        provider=forecast.provider,
    )
    return ToolResult.ok(
        forecast,
        f"{len(forecast.samples)} punkter längs rutten från {forecast.provider}",
        citation,
    )


def irradiance_forecast(
    estimates: Sequence[PowerEstimate] | None,
) -> ToolResult[list[PowerEstimate]]:
    """Shape a power-estimate series (irradiance + array power) as a tool result."""
    if not estimates:
        return ToolResult.unavailable(
            "ingen irradians-/effektprognos: provider saknar GHI/DNI/DHI eller rutt saknas"
        )
    first = estimates[0]
    citation = Citation(
        id="irradiance",
        tool="irradiance_forecast",
        source_module="rwi.energy.array",
        description=f"effektserie, {len(estimates)} steg",
        valid_time_utc=first.time_utc,
        retrieved_at_utc=first.provenance.retrieved_at_utc,
        provider=first.provenance.provider,
        model_init_time_utc=first.provenance.model_init_time_utc,
    )
    return ToolResult.ok(
        list(estimates),
        f"effektserie om {len(estimates)} steg",
        citation,
    )


def ensemble_summary(
    summaries: Sequence[EnsembleSummary] | None, *, variable: str
) -> ToolResult[list[EnsembleSummary]]:
    """Shape an ensemble summary list as a tool result.

    An empty or ``None`` input is reported as unavailable with the
    variable named, so the composer can distinguish 'no ensemble data
    for GHI' from 'no ensemble data at all'.
    """
    if not summaries:
        return ToolResult.unavailable(
            f"ingen ensemblesammanfattning för {variable}"
        )
    first = summaries[0]
    citation = Citation(
        id=f"ensemble_{variable}",
        tool="ensemble_summary",
        source_module="rwi.ensemble.summary",
        description=f"ensemble för {variable}: "
        f"{first.member_count} medlemmar, {len(summaries)} giltiga tider",
        valid_time_utc=first.valid_time_utc,
        provider=first.provider,
        model_name=first.model_name,
    )
    return ToolResult.ok(
        list(summaries),
        f"ensemble ({first.member_count} medlemmar) för {variable}",
        citation,
    )


def model_disagreements(
    items: Sequence[ModelDisagreement] | None, *, variable: str
) -> ToolResult[list[ModelDisagreement]]:
    """Shape model-disagreement entries. Empty = models agree, not 'no data'."""
    if items is None:
        return ToolResult.unavailable(
            f"ingen modell-jämförelse för {variable}: färre än två källor"
        )
    significant = [d for d in items if d.is_significant]
    if not significant:
        citation = Citation(
            id=f"fusion_{variable}",
            tool="model_disagreements",
            source_module="rwi.fusion.compare",
            description=f"modellenighet för {variable}: ingen signifikant avvikelse",
        )
        return ToolResult.ok(
            [],
            f"modellerna är överens om {variable} inom tröskeln",
            citation,
        )
    first = significant[0]
    citation = Citation(
        id=f"fusion_{variable}",
        tool="model_disagreements",
        source_module="rwi.fusion.compare",
        description=f"{len(significant)} signifikanta avvikelser för {variable}",
        valid_time_utc=first.valid_time_utc,
    )
    return ToolResult.ok(
        list(significant),
        f"{len(significant)} signifikanta modellavvikelser för {variable}",
        citation,
    )


def verification_skill(
    score: VerificationScore | None, *, variable: str, lead_time_s: float
) -> ToolResult[VerificationScore]:
    """Shape a verification score as a tool result.

    A ``None`` score is reported as unavailable with the variable and
    lead time named. A score with too few samples returns the value with
    a reason that says the skill is provisional - the composer then
    emits an ``kind='unproven'`` uncertainty item rather than quoting
    the number as if it were established skill.
    """
    if score is None:
        return ToolResult.unavailable(
            f"ingen verifierad skicklighet för {variable} vid lead-tid "
            f"{lead_time_s / 3600:.0f} h"
        )
    citation = Citation(
        id=f"verification_{variable}",
        tool="verification_skill",
        source_module="rwi.verification.metrics",
        description=f"verifiering av {variable} över "
        f"{score.sample_count} par ({score.provider})",
        provider=score.provider,
        model_name=score.model_name,
    )
    sample_qualifier = "provisorisk" if not score.is_meaningful else "etablerad"
    return ToolResult.ok(
        score,
        f"{sample_qualifier} skicklighet, {score.sample_count} par",
        citation,
    )


def active_alerts(alerts: Sequence[Alert] | None) -> ToolResult[list[Alert]]:
    """Shape a list of alerts (sorted by caller) as a tool result.

    An empty list is 'no active alerts' - a positive statement - not
    'alerts unavailable'. ``None`` means the alerts engine could not
    run (typically missing weather data).
    """
    if alerts is None:
        return ToolResult.unavailable(
            "varningsmotorn kunde inte köras (saknad väderdata eller position)"
        )
    citation = Citation(
        id="alerts",
        tool="active_alerts",
        source_module="rwi.alerts.rules",
        description=f"{len(alerts)} aktiva varningar",
    )
    return ToolResult.ok(
        list(alerts),
        f"{len(alerts)} aktiva varningar",
        citation,
    )


class ConvectionOutlook(BaseModel):
    """Convection summary for the morning briefing.

    All numbers come from the forecast series; nothing is invented.
    ``peak_window`` is None when no step cleared the WATCH threshold -
    then ``worst_severity`` is None too and ``verdict`` describes the
    stable case. A separate seam, ``satellite_cell_hint``, is reserved
    for the satellite-nowcast integration (growing cells) that will
    land in a later phase; it stays None until that work lands so the
    composer can plan around the field without wiring it now.
    """

    model_config = ConfigDict(frozen=True)

    worst_severity: AlertSeverity | None
    peak_cape: float | None
    min_lifted_index: float | None
    peak_pop: float | None
    peak_window: TimeWindow | None
    verdict: str
    satellite_cell_hint: str | None = None


class WindOutlook(BaseModel):
    """Peak-gust window in the forecast horizon, for the briefing."""

    model_config = ConfigDict(frozen=True)

    peak_gust_mps: float | None
    peak_wind_mps: float | None
    peak_window: TimeWindow | None
    dominant_direction_deg: float | None
    verdict: str


def convection_outlook(
    series: WeatherSeries | None,
    *,
    now: datetime,
    horizon_s: float,
) -> ToolResult[ConvectionOutlook]:
    """Summarise convection risk across ``horizon_s`` from a forecast series.

    Graciously degrades: when ``series`` is None, or when it carries
    neither ``cape`` nor ``lifted_index``, returns :meth:`unavailable`
    so the composer can emit a data gap rather than claim "stable".
    """
    if series is None:
        return ToolResult.unavailable(
            "ingen väderserie att läsa instabilitets-index från"
        )
    limit = now + timedelta(seconds=horizon_s)
    peak_cape: float | None = None
    min_li: float | None = None
    peak_pop: float | None = None
    peak_window: TimeWindow | None = None
    worst: AlertSeverity | None = None
    has_index = False
    for step in series.steps:
        if step.valid_time_utc > limit:
            break
        cape = step.value("cape")
        li = step.value("lifted_index")
        if cape is None and li is None:
            continue
        has_index = True
        pop = step.value("precipitation_probability")
        if cape is not None and (peak_cape is None or cape > peak_cape):
            peak_cape = cape
        if li is not None and (min_li is None or li < min_li):
            min_li = li
        if pop is not None and (peak_pop is None or pop > peak_pop):
            peak_pop = pop
        severity = _score_step(cape=cape, li=li, pop=pop)
        if severity is not None and (
            worst is None or _severity_rank(severity) > _severity_rank(worst)
        ):
            worst = severity
            peak_window = TimeWindow(
                start_utc=step.valid_time_utc,
                end_utc=step.valid_time_utc + timedelta(hours=1),
            )
    if not has_index:
        return ToolResult.unavailable(
            "serien saknar cape/lifted_index — kan ej bedöma konvektion"
        )
    verdict = _convection_verdict(worst, peak_cape, min_li, peak_pop, peak_window)
    outlook = ConvectionOutlook(
        worst_severity=worst,
        peak_cape=peak_cape,
        min_lifted_index=min_li,
        peak_pop=peak_pop,
        peak_window=peak_window,
        verdict=verdict,
    )
    citation = Citation(
        id="convection",
        tool="convection_outlook",
        source_module="rwi.alerts.convection",
        description=verdict,
        valid_time_utc=peak_window.start_utc if peak_window is not None else None,
        retrieved_at_utc=now,
    )
    return ToolResult.ok(outlook, verdict, citation)


def wind_outlook(
    series: WeatherSeries | None,
    *,
    now: datetime,
    horizon_s: float,
) -> ToolResult[WindOutlook]:
    """Peak gust / mean wind in the horizon, from the same forecast series."""
    if series is None:
        return ToolResult.unavailable("ingen väderserie för vindutsikt")
    limit = now + timedelta(seconds=horizon_s)
    peak_gust: float | None = None
    peak_wind: float | None = None
    peak_dir: float | None = None
    peak_window: TimeWindow | None = None
    for step in series.steps:
        if step.valid_time_utc > limit:
            break
        gust = step.value("wind_gusts_10m")
        if gust is not None and (peak_gust is None or gust > peak_gust):
            peak_gust = gust
            peak_window = TimeWindow(
                start_utc=step.valid_time_utc,
                end_utc=step.valid_time_utc + timedelta(hours=1),
            )
            direction = step.value("wind_direction_10m")
            if direction is not None:
                peak_dir = direction
        wind = step.value("wind_speed_10m")
        if wind is not None and (peak_wind is None or wind > peak_wind):
            peak_wind = wind
    if peak_gust is None and peak_wind is None:
        return ToolResult.unavailable("serien saknar vindfält")
    if peak_gust is not None:
        verdict = f"topp byvind {peak_gust:.1f} m/s"
    else:
        verdict = f"topp vind {peak_wind:.1f} m/s"
    outlook = WindOutlook(
        peak_gust_mps=peak_gust,
        peak_wind_mps=peak_wind,
        peak_window=peak_window,
        dominant_direction_deg=peak_dir,
        verdict=verdict,
    )
    citation = Citation(
        id="wind_outlook",
        tool="wind_outlook",
        source_module="rwi.meteorologist.tools",
        description=verdict,
        valid_time_utc=peak_window.start_utc if peak_window is not None else None,
        retrieved_at_utc=now,
    )
    return ToolResult.ok(outlook, verdict, citation)


def synoptic_outlook(
    analysis: SynopticAnalysis | None,
    *,
    now: datetime,
    horizon_s: float,
) -> ToolResult[SynopticAnalysis]:
    """Shape a :class:`~rwi.alerts.synoptic.SynopticAnalysis` for the composer.

    Keeps only front signatures inside ``[now, now + horizon_s]``. It does not
    compute anything: speed, tendencies and the profile are the engine's.
    """
    if analysis is None:
        return ToolResult.unavailable("ingen synoptisk analys hämtad")
    if not analysis.available:
        missing = ", ".join(analysis.missing) or "alla"
        return ToolResult.unavailable(
            f"för få ruttpunkter för synoptisk analys (saknas: {missing})"
        )
    limit = now + timedelta(seconds=horizon_s)
    fronts = tuple(f for f in analysis.fronts if now <= f.passage_time_utc <= limit)
    shaped = replace(analysis, fronts=fronts)
    signatures = [f for f in fronts if f.strength == "signature"]
    if signatures:
        names = ", ".join(dict.fromkeys(f.name for f in signatures))
        verdict = f"frontsignatur i modellen vid {names}"
    elif fronts:
        names = ", ".join(dict.fromkeys(f.name for f in fronts))
        verdict = f"möjlig front (bara två kriterier uppfyllda) vid {names}"
    else:
        verdict = "ingen frontsignatur längs rutten i horisonten"
    citation = Citation(
        id="synoptic_outlook",
        tool="synoptic_outlook",
        source_module="rwi.alerts.synoptic",
        description=verdict,
        valid_time_utc=fronts[0].passage_time_utc if fronts else None,
        retrieved_at_utc=now,
        provider=analysis.provider,
        model_name=analysis.model_name,
        model_init_time_utc=analysis.model_init_time_utc,
    )
    return ToolResult.ok(shaped, verdict, citation)


def _severity_rank(s: AlertSeverity) -> int:
    return {
        AlertSeverity.INFO: 0,
        AlertSeverity.WATCH: 1,
        AlertSeverity.WARNING: 2,
        AlertSeverity.CRITICAL: 3,
    }[s]


def _score_step(
    *, cape: float | None, li: float | None, pop: float | None
) -> AlertSeverity | None:
    # Mirrors rwi.alerts.convection._severity; kept local so the tool
    # layer does not import private helpers across the lane boundary.
    cape_crit = cape is not None and cape >= 2500.0
    cape_warn = cape is not None and cape >= 1000.0
    cape_watch = cape is not None and cape >= 500.0
    li_crit = li is not None and li <= -4.0
    li_warn = li is not None and li <= -2.0
    li_watch = li is not None and li <= 0.0
    pop_crit = pop is not None and pop >= 60.0
    pop_warn = pop is not None and pop >= 40.0
    if cape_crit and li_crit and pop_crit:
        return AlertSeverity.CRITICAL
    strong = sum((cape_warn, li_warn, pop_warn))
    if strong >= 2:
        return AlertSeverity.WARNING
    if cape_watch or li_watch:
        return AlertSeverity.WATCH
    return None


def _convection_verdict(
    worst: AlertSeverity | None,
    cape: float | None,
    li: float | None,
    pop: float | None,
    window: TimeWindow | None,
) -> str:
    if worst is None:
        return "stabil atmosfär, ingen konvektionsrisk inom horisonten"
    headline = f"konvektion {worst.value}"
    parts = []
    if cape is not None:
        parts.append(f"peak CAPE {cape:.0f} J/kg")
    if li is not None:
        parts.append(f"min LI {li:.1f}")
    if pop is not None:
        parts.append(f"max PoP {pop:.0f}%")
    when = f" runt {window.start_utc.isoformat()}" if window is not None else ""
    return headline + ": " + ", ".join(parts) + when


def platform_self_knowledge(
    summary: SelfKnowledgeSummary | None,
) -> ToolResult[SelfKnowledgeSummary]:
    """Shape a self-knowledge summary as a tool result.

    ``None`` means the context assembler did not compute one (e.g. no
    settings handed in a unit test). An actual summary with
    ``archive_available=False`` is still ok at this layer - it is the
    engine's honest answer and the composer surfaces it as a
    track-record line saying the platform has no local evidence yet.
    """
    if summary is None:
        return ToolResult.unavailable(
            "självkännedom beräknades inte (ingen arkivkoppling)"
        )
    citation = Citation(
        id="self_knowledge",
        tool="platform_self_knowledge",
        source_module="rwi.meteorologist.self_knowledge",
        description=summary.verdict,
        valid_time_utc=summary.newest_pair_utc,
        retrieved_at_utc=summary.newest_pair_utc,
    )
    return ToolResult.ok(summary, summary.verdict, citation)


__all__ = [
    "ConvectionOutlook",
    "ToolResult",
    "WindOutlook",
    "active_alerts",
    "convection_outlook",
    "ensemble_summary",
    "irradiance_forecast",
    "model_disagreements",
    "platform_self_knowledge",
    "route_weather_ahead",
    "solar_day",
    "synoptic_outlook",
    "verification_skill",
    "where_and_when",
    "wind_outlook",
]
