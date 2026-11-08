"""Deterministic composer: tool results → :class:`Briefing`.

One function per question kind. The composer never computes a number.
It picks values straight out of :class:`ToolResult.value` and puts them
in :class:`DataPoint` objects tied to the matching :class:`Citation`.
Prose is fixed Swedish template text.

The composer's invariants (checked by the Briefing's own construction
and by :mod:`rwi.meteorologist.numeric_guard` on rendered output):

* every :class:`DataPoint` resolves to a :class:`Citation`;
* :attr:`Briefing.uncertainty` is never empty - the fallback is one
  ``kind='unproven'`` item saying skill is not established;
* :attr:`Briefing.unknowns` includes static platform gaps (no telemetry,
  no on-vehicle pyranometer) in addition to the tool-reported ones.
"""

from __future__ import annotations

from datetime import datetime

from rwi.contracts import (
    Alert,
    AlertSeverity,
    EnsembleSummary,
    ModelDisagreement,
    PowerEstimate,
    RouteWeatherForecast,
    SolarDayEvents,
    VerificationScore,
)
from rwi.core.position import ActiveState
from rwi.meteorologist.contract import (
    Briefing,
    Citation,
    DataPoint,
    QuestionKind,
    UncertaintyItem,
    WatchItem,
)
from rwi.meteorologist.tools import ToolResult

STATIC_UNKNOWNS: tuple[str, ...] = (
    "Ingen pyranometer eller arraytelemetri — modellerad irradians kan inte "
    "jämföras mot mätning här.",
    "Ingen verifieringshistorik från den här rutten — skicklighet från "
    "andra platser är inte automatiskt överförbar.",
)


class BriefingContext:
    """The bundle of tool results the composer reads.

    Every field defaults to ``ToolResult.unavailable`` with a specific
    reason so an API caller that forgot to populate one does not get
    silent absence.
    """

    def __init__(
        self,
        *,
        state: ToolResult[ActiveState],
        solar_day: ToolResult[SolarDayEvents],
        route_weather: ToolResult[RouteWeatherForecast],
        irradiance: ToolResult[list[PowerEstimate]],
        ensemble_ghi: ToolResult[list[EnsembleSummary]],
        ensemble_wind: ToolResult[list[EnsembleSummary]],
        disagreements_ghi: ToolResult[list[ModelDisagreement]],
        verification_ghi: ToolResult[VerificationScore],
        alerts: ToolResult[list[Alert]],
    ) -> None:
        self.state = state
        self.solar_day = solar_day
        self.route_weather = route_weather
        self.irradiance = irradiance
        self.ensemble_ghi = ensemble_ghi
        self.ensemble_wind = ensemble_wind
        self.disagreements_ghi = disagreements_ghi
        self.verification_ghi = verification_ghi
        self.alerts = alerts

    def available_citations(self) -> list[Citation]:
        """Return citations from every tool that produced a value."""
        results: list[ToolResult] = [
            self.state,
            self.solar_day,
            self.route_weather,
            self.irradiance,
            self.ensemble_ghi,
            self.ensemble_wind,
            self.disagreements_ghi,
            self.verification_ghi,
            self.alerts,
        ]
        return [r.citation for r in results if r.citation is not None]

    def tool_gaps(self) -> list[str]:
        """One line per tool that could not answer."""
        pairs: list[tuple[ToolResult, str]] = [
            (self.state, "position"),
            (self.solar_day, "soldag"),
            (self.route_weather, "ruttprognos"),
            (self.irradiance, "irradians-/effektprognos"),
            (self.ensemble_ghi, "GHI-ensemble"),
            (self.ensemble_wind, "vind-ensemble"),
            (self.disagreements_ghi, "GHI-modelljämförelse"),
            (self.verification_ghi, "GHI-verifiering"),
            (self.alerts, "varningar"),
        ]
        return [f"{label} saknas: {r.reason}" for r, label in pairs if not r.available]


def _alert_summary(alerts: list[Alert]) -> tuple[str, list[str]]:
    """Return (assessment_sentence, operational_consequences)."""
    if not alerts:
        return (
            "Inga aktiva varningar i väntan.",
            ["Ingen omedelbar operativ åtgärd från varningsmotorn."],
        )
    severity_order = [
        AlertSeverity.CRITICAL,
        AlertSeverity.WARNING,
        AlertSeverity.WATCH,
        AlertSeverity.INFO,
    ]
    by_sev = {s: [a for a in alerts if a.severity == s] for s in severity_order}
    top = next((a for s in severity_order for a in by_sev.get(s, [])), alerts[0])
    consequences: list[str] = []
    for a in alerts:
        action = a.recommended_action or "ingen rekommenderad åtgärd specificerad"
        consequences.append(f"[{a.severity.value}] {a.headline}: {action}")
    return (
        f"Högsta aktiva varningen: {top.severity.value} — {top.headline}.",
        consequences,
    )


def _ensemble_uncertainty(
    summaries: list[EnsembleSummary], variable: str, citation_id: str
) -> UncertaintyItem:
    """Pick the WORST-spread member of the series for the headline.

    Not aggregation: this is a selection from the engine's existing
    summaries and the citation points at that same engine.
    """
    worst = max(summaries, key=lambda s: s.standard_deviation)
    p10 = worst.percentiles.get("10")
    p90 = worst.percentiles.get("90")
    if p10 is not None and p90 is not None:
        return UncertaintyItem(
            kind="ensemble_spread",
            statement=(
                f"Ensemble-spridning ({worst.member_count} medlemmar) för "
                f"{variable}: p10 {p10:.1f}, p90 {p90:.1f} {worst.unit} "
                f"vid {worst.valid_time_utc.isoformat()}."
            ),
            citation_id=citation_id,
        )
    return UncertaintyItem(
        kind="ensemble_spread",
        statement=(
            f"Ensemble-spridning för {variable}: standardavvikelse "
            f"{worst.standard_deviation:.2f} {worst.unit} vid "
            f"{worst.valid_time_utc.isoformat()}."
        ),
        citation_id=citation_id,
    )


def _disagreement_uncertainty(
    disagreements: list[ModelDisagreement], variable: str, citation_id: str
) -> UncertaintyItem | None:
    if not disagreements:
        return None
    worst = max(disagreements, key=lambda d: d.spread)
    return UncertaintyItem(
        kind="model_disagreement",
        statement=(
            f"Modell-avvikelse för {variable}: spridning {worst.spread:.1f} "
            f"{worst.unit} mellan {worst.model_count} modeller vid "
            f"{worst.valid_time_utc.isoformat()} (tröskel {worst.threshold:.1f})."
        ),
        citation_id=citation_id,
    )


def _verification_uncertainty(
    score: VerificationScore, variable: str, citation_id: str
) -> UncertaintyItem:
    if not score.is_meaningful or score.mean_absolute_error is None:
        return UncertaintyItem(
            kind="unproven",
            statement=(
                f"Skickligheten för {variable} är inte etablerad: "
                f"{score.sample_count} lagrade par (minst 30 krävs)."
            ),
            citation_id=citation_id,
        )
    skill = score.skill_vs_persistence
    skill_text = (
        f"skicklighet mot persistens {skill:+.2f}"
        if skill is not None
        else "ingen persistens-baseline"
    )
    return UncertaintyItem(
        kind="verified_skill",
        statement=(
            f"Verifierad MAE för {variable}: {score.mean_absolute_error:.2f} "
            f"{score.unit} över {score.sample_count} par, {skill_text}."
        ),
        citation_id=citation_id,
    )


def _uncertainty_section(
    ctx: BriefingContext, *, variables_of_interest: tuple[str, ...]
) -> list[UncertaintyItem]:
    """Build section 3 in a fixed order: ensemble → disagreement → skill.

    If none of the three is available for any variable the fallback is
    one ``kind='unproven'`` item that says so explicitly.
    """
    items: list[UncertaintyItem] = []
    for var in variables_of_interest:
        if var == "ghi":
            if ctx.ensemble_ghi.available and ctx.ensemble_ghi.citation is not None:
                items.append(
                    _ensemble_uncertainty(
                        ctx.ensemble_ghi.value or [],
                        "GHI",
                        ctx.ensemble_ghi.citation.id,
                    )
                )
            if (
                ctx.disagreements_ghi.available
                and ctx.disagreements_ghi.citation is not None
            ):
                maybe = _disagreement_uncertainty(
                    ctx.disagreements_ghi.value or [],
                    "GHI",
                    ctx.disagreements_ghi.citation.id,
                )
                if maybe is not None:
                    items.append(maybe)
            if (
                ctx.verification_ghi.available
                and ctx.verification_ghi.value is not None
                and ctx.verification_ghi.citation is not None
            ):
                items.append(
                    _verification_uncertainty(
                        ctx.verification_ghi.value,
                        "GHI",
                        ctx.verification_ghi.citation.id,
                    )
                )
        elif var == "wind_speed_10m":
            if ctx.ensemble_wind.available and ctx.ensemble_wind.citation is not None:
                items.append(
                    _ensemble_uncertainty(
                        ctx.ensemble_wind.value or [],
                        "vind (10 m)",
                        ctx.ensemble_wind.citation.id,
                    )
                )
    if not items:
        items.append(
            UncertaintyItem(
                kind="unproven",
                statement=(
                    "Ingen ensemble-spridning, modelljämförelse eller verifierad "
                    "skicklighet tillgänglig — osäkerheten för denna fråga är oprövad."
                ),
            )
        )
    return items


def _warnings_from_state(ctx: BriefingContext) -> list[str]:
    """Static, data-driven warnings. No speculation."""
    out: list[str] = []
    if ctx.state.available and ctx.state.value is not None:
        state = ctx.state.value
        if state.suspect_fix is not None:
            out.append(
                "Misstänkt GPS-fix är parkerad — nedströms använder senaste "
                "betrodda fix."
            )
        if state.selected_time_utc is not None:
            out.append(
                f"Operatören har valt simulerad tid "
                f"{state.selected_time_utc.isoformat()} — detta är inte "
                "realtidsförhållanden."
            )
    if ctx.solar_day.available and ctx.solar_day.value is not None:
        events = ctx.solar_day.value
        if events.is_polar_day:
            out.append("Polardag — inga normala soluppgångs-/nedgångstider.")
        if events.is_polar_night:
            out.append("Polarnatt — ingen soluppgång för den lokala dagen.")
    return out


def _peak_from_series(
    estimates: list[PowerEstimate],
) -> tuple[DataPoint, DataPoint] | None:
    """Pick the max net-power step and its POA GHI.

    Both numbers come from the same :class:`PowerEstimate`, so one
    citation covers both.
    """
    if not estimates:
        return None
    peak = max(estimates, key=lambda e: e.net_power_w)
    return (
        DataPoint(
            label="toppeffekt netto",
            value=round(peak.net_power_w, 1),
            unit="W",
            valid_time_utc=peak.time_utc,
            citation_id="irradiance",
        ),
        DataPoint(
            label="POA GHI vid toppeffekt",
            value=round(peak.poa.poa_global_w_m2, 1),
            unit="W/m²",
            valid_time_utc=peak.time_utc,
            citation_id="irradiance",
        ),
    )


def _solar_day_data(events: SolarDayEvents) -> list[DataPoint]:
    out: list[DataPoint] = []
    if events.sunrise_utc is not None:
        out.append(
            DataPoint(
                label="soluppgång (UTC)",
                value=events.sunrise_utc.timestamp(),
                unit="s (unix)",
                valid_time_utc=events.sunrise_utc,
                citation_id="solar_day",
            )
        )
    if events.sunset_utc is not None:
        out.append(
            DataPoint(
                label="solnedgång (UTC)",
                value=events.sunset_utc.timestamp(),
                unit="s (unix)",
                valid_time_utc=events.sunset_utc,
                citation_id="solar_day",
            )
        )
    out.append(
        DataPoint(
            label="transit-elevation",
            value=round(events.transit_elevation_deg, 2),
            unit="deg",
            valid_time_utc=events.solar_noon_utc,
            citation_id="solar_day",
        )
    )
    return out


def _compose_risk_ahead(ctx: BriefingContext, asked_at: datetime) -> Briefing:
    alerts = ctx.alerts.value if ctx.alerts.available else []
    assessment, consequences = _alert_summary(alerts or [])
    supporting: list[DataPoint] = []
    if ctx.irradiance.available and ctx.irradiance.value is not None:
        pair = _peak_from_series(ctx.irradiance.value)
        if pair is not None:
            supporting.extend(pair)
    watch: list[WatchItem] = []
    if ctx.solar_day.available and ctx.solar_day.value is not None:
        events = ctx.solar_day.value
        if events.sunset_utc is not None:
            watch.append(
                WatchItem(
                    what="Solnedgång",
                    when=events.sunset_utc.isoformat(),
                    trigger="elevation_deg < 0",
                    why="Effekten går till noll — batteribanken tar över.",
                )
            )
    if (
        ctx.ensemble_ghi.available
        and ctx.ensemble_ghi.value is not None
        and ctx.ensemble_ghi.value
    ):
        worst = max(ctx.ensemble_ghi.value, key=lambda s: s.standard_deviation)
        watch.append(
            WatchItem(
                what="GHI-ensemble-spridning",
                when=worst.valid_time_utc.isoformat(),
                trigger="standardavvikelse > föregående uppdatering",
                why="Ökad spridning försämrar energibudgetens tillförlitlighet.",
            )
        )
    warnings = _warnings_from_state(ctx)
    warnings.extend(
        [
            "Varningströsklar är dokumenterade i rwi.alerts.rules; ingen av dem är "
            "anpassad efter fordonets verkliga specifikation.",
        ]
    )
    unknowns = list(STATIC_UNKNOWNS) + ctx.tool_gaps()
    return Briefing(
        question_kind=QuestionKind.RISK_AHEAD,
        question="Vad är den viktigaste väderrisken framför oss?",
        asked_at_utc=asked_at,
        assessment=assessment,
        supporting_data=supporting,
        uncertainty=_uncertainty_section(
            ctx, variables_of_interest=("ghi", "wind_speed_10m")
        ),
        operational_consequences=consequences,
        watch_next=watch,
        warnings_and_limits=warnings,
        citations=ctx.available_citations(),
        unknowns=unknowns,
    )


def _compose_forecast_reliability(
    ctx: BriefingContext, asked_at: datetime, *, variable: str
) -> Briefing:
    """Reliability-of-forecast briefing, framed around uncertainty.

    Section 1's headline is the verification state, not a numeric
    forecast: the question is 'how reliable is this', not 'what is it'.
    """
    verified = ctx.verification_ghi
    if verified.available and verified.value is not None and verified.value.is_meaningful:
        v = verified.value
        assessment = (
            f"Verifierad MAE för {variable}: {v.mean_absolute_error:.2f} {v.unit} "
            f"över {v.sample_count} par ({v.provider})."
        )
    elif verified.available and verified.value is not None:
        v = verified.value
        assessment = (
            f"Skickligheten för {variable} är oprövad: endast {v.sample_count} "
            "lagrade par (minst 30 krävs för en etablerad siffra)."
        )
    else:
        assessment = (
            f"Ingen verifierad skicklighet för {variable} — den här plattformen "
            "har inga lagrade prognospar ännu."
        )
    supporting: list[DataPoint] = []
    if verified.available and verified.value is not None:
        v = verified.value
        supporting.append(
            DataPoint(
                label=f"{variable} sample-antal",
                value=float(v.sample_count),
                unit="par",
                citation_id=verified.citation.id if verified.citation else "state",
            )
        )
        if v.mean_absolute_error is not None:
            supporting.append(
                DataPoint(
                    label=f"{variable} MAE",
                    value=round(v.mean_absolute_error, 3),
                    unit=v.unit,
                    citation_id=verified.citation.id
                    if verified.citation
                    else "state",
                )
            )
    consequences: list[str] = []
    if verified.available and verified.value is not None and verified.value.is_meaningful:
        consequences.append(
            "Använd den verifierade felnivån som nedre gräns för "
            "beslutsmarginal, inte prognosens punktvärde."
        )
    else:
        consequences.append(
            "Behandla punktprognosen som obevisad — kräv en fallback-plan som "
            "fungerar om den slår fel."
        )
    watch: list[WatchItem] = [
        WatchItem(
            what="Lead-tid-skicklighet",
            when="när fler lagrade par ackumuleras",
            trigger="sample_count >= 30",
            why="Först då blir skicklighetsmåttet meningsfullt i rwi.verification.",
        )
    ]
    warnings = _warnings_from_state(ctx)
    warnings.append(
        "Verifiering mäter historisk skicklighet, inte garanti för detta tillfälle."
    )
    unknowns = list(STATIC_UNKNOWNS) + ctx.tool_gaps()
    return Briefing(
        question_kind=QuestionKind.FORECAST_RELIABILITY,
        question=f"Hur pålitlig är {variable}-prognosen?",
        asked_at_utc=asked_at,
        assessment=assessment,
        supporting_data=supporting,
        uncertainty=_uncertainty_section(ctx, variables_of_interest=(variable,)),
        operational_consequences=consequences,
        watch_next=watch,
        warnings_and_limits=warnings,
        citations=ctx.available_citations(),
        unknowns=unknowns,
    )


def _compose_morning_briefing(ctx: BriefingContext, asked_at: datetime) -> Briefing:
    """Stub morning briefing — filled in Fas 2.

    Fas 1 lays the plumbing: this returns a Briefing with the exact same
    shape as the other kinds, with ``assessment`` clearly stating that
    the synoptic content arrives in Fas 2 and ``watch_next`` telling the
    user to come back for the convection flag.
    """
    assessment = (
        "Morgonbriefing-ramen finns men den synoptiska texten "
        "levereras i Fas 2 (konvektionsvarning först)."
    )
    supporting: list[DataPoint] = []
    if ctx.solar_day.available and ctx.solar_day.value is not None:
        supporting.extend(_solar_day_data(ctx.solar_day.value))
    consequences = [
        "Använd denna briefing som position-/soldags-ram; väntar på synoptisk analys."
    ]
    watch: list[WatchItem] = [
        WatchItem(
            what="Konvektionsvarning",
            when="Fas 2",
            trigger="instability-indicator från NWP",
            why="Eftermiddagskonvektion är det mest racerelevanta synoptiska fältet.",
        )
    ]
    warnings = [*_warnings_from_state(ctx), "Synoptisk briefing är inte implementerad i Fas 1."]
    unknowns = list(STATIC_UNKNOWNS) + ctx.tool_gaps()
    return Briefing(
        question_kind=QuestionKind.MORNING_BRIEFING,
        question="Morgonbriefing",
        asked_at_utc=asked_at,
        assessment=assessment,
        supporting_data=supporting,
        uncertainty=_uncertainty_section(
            ctx, variables_of_interest=("ghi", "wind_speed_10m")
        ),
        operational_consequences=consequences,
        watch_next=watch,
        warnings_and_limits=warnings,
        citations=ctx.available_citations(),
        unknowns=unknowns,
    )


def compose(
    kind: QuestionKind,
    ctx: BriefingContext,
    *,
    asked_at: datetime,
    variable: str = "ghi",
) -> Briefing:
    """Dispatch to the per-kind composer. ``variable`` is used for reliability."""
    if kind is QuestionKind.RISK_AHEAD:
        return _compose_risk_ahead(ctx, asked_at)
    if kind is QuestionKind.FORECAST_RELIABILITY:
        return _compose_forecast_reliability(ctx, asked_at, variable=variable)
    if kind is QuestionKind.MORNING_BRIEFING:
        return _compose_morning_briefing(ctx, asked_at)
    raise ValueError(
        f"QuestionKind {kind!r} is not handled by the deterministic composer; "
        "the LLM adapter must run instead (free-form questions)."
    )


__all__ = [
    "STATIC_UNKNOWNS",
    "BriefingContext",
    "compose",
]
