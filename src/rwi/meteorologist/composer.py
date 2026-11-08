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
from rwi.meteorologist.self_knowledge import SelfKnowledgeSummary
from rwi.meteorologist.tools import ConvectionOutlook, ToolResult, WindOutlook

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
        convection: ToolResult[ConvectionOutlook] | None = None,
        wind: ToolResult[WindOutlook] | None = None,
        self_knowledge: ToolResult[SelfKnowledgeSummary] | None = None,
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
        # Defaulted to unavailable so Fas 1 call sites keep working
        # without passing them. The morning-briefing path requires both.
        self.convection: ToolResult[ConvectionOutlook] = convection or ToolResult.unavailable(
            "konvektions-utsikt är inte beräknad för denna fråga"
        )
        self.wind: ToolResult[WindOutlook] = wind or ToolResult.unavailable(
            "vindutsikt är inte beräknad för denna fråga"
        )
        # Fas 3: the briefing must always disclose what the platform knows
        # about its own skill, so this is treated as structurally required
        # even though the default lets Fas 1/2 fixtures keep working.
        self.self_knowledge: ToolResult[SelfKnowledgeSummary] = (
            self_knowledge
            or ToolResult.unavailable(
                "självkännedom beräknades inte för denna fråga"
            )
        )

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
            self.convection,
            self.wind,
            self.self_knowledge,
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
            (self.convection, "konvektions-utsikt"),
            (self.wind, "vindutsikt"),
            (self.self_knowledge, "självkännedom (arkiv)"),
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
        elif (
            var == "wind_speed_10m"
            and ctx.ensemble_wind.available
            and ctx.ensemble_wind.citation is not None
        ):
            items.append(
                _ensemble_uncertainty(
                    ctx.ensemble_wind.value or [],
                    "vind (10 m)",
                    ctx.ensemble_wind.citation.id,
                )
            )
    # Fas 3: track-record is obligatory in every briefing. Prepend it so
    # the operator reads 'how good has this platform been' before the
    # ensemble / disagreement / verification bucket details.
    items.insert(0, _track_record_item(ctx))
    if len(items) == 1:
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


def _track_record_item(ctx: BriefingContext) -> UncertaintyItem:
    """Obligatory track-record line for every briefing (Fas 3).

    The platform speaks plainly about what it knows: zero local pairs,
    a model that beats persistence, or - the most self-critical case -
    a model that is worse than persistence locally.
    """
    citation_id = (
        ctx.self_knowledge.citation.id
        if ctx.self_knowledge.citation is not None
        else None
    )
    if not ctx.self_knowledge.available or ctx.self_knowledge.value is None:
        return UncertaintyItem(
            kind="track_record",
            statement=(
                "Plattformens egen prognosskicklighet är obevisad lokalt — "
                "ingen lagrad verifiering ("
                + ctx.self_knowledge.reason
                + ")."
            ),
            citation_id=citation_id,
        )
    sk = ctx.self_knowledge.value
    if not sk.archive_available:
        return UncertaintyItem(
            kind="track_record",
            statement=(
                f"Plattformens track record saknas: {sk.verdict}. "
                f"{sk.detail}"
            ),
            citation_id=citation_id,
        )
    if sk.total_pairs == 0:
        return UncertaintyItem(
            kind="track_record",
            statement=(
                "Arkivet är tomt — plattformen har ingen egen track record "
                "ännu; alla skicklighetsutsagor nedan är obevisade lokalt."
            ),
            citation_id=citation_id,
        )
    worse = [
        s for s in sk.coverage_by_variable.values()
        if s.verdict == "sämre_än_persistens"
    ]
    if worse:
        worst_names = ", ".join(sorted(s.variable for s in worse))
        return UncertaintyItem(
            kind="track_record",
            statement=(
                f"Plattformens track record: {sk.verdict}. Varning — "
                f"{worst_names} är sämre än persistens lokalt. {sk.detail}"
            ),
            citation_id=citation_id,
        )
    return UncertaintyItem(
        kind="track_record",
        statement=f"Plattformens track record: {sk.verdict}. {sk.detail}",
        citation_id=citation_id,
    )


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
    """Synoptic morning briefing. Convection first, then everything else.

    The six sections are always filled; every number is drawn from a
    tool-cited value and the uncertainty section is never empty. When
    the convection tool could not read the indices (missing cape /
    lifted_index in the series) the briefing degrades to the stable-day
    template and the gap appears in ``unknowns``.
    """
    supporting: list[DataPoint] = []
    watch: list[WatchItem] = []
    consequences: list[str] = []

    # --- Section 1: assessment (convection first, then wind, then stable).
    conv_value = ctx.convection.value if ctx.convection.available else None
    wind_value = ctx.wind.value if ctx.wind.available else None
    conv_citation_id = (
        ctx.convection.citation.id if ctx.convection.citation is not None else None
    )
    wind_citation_id = ctx.wind.citation.id if ctx.wind.citation is not None else None
    assessment_parts: list[str] = []
    if conv_value is not None and conv_value.worst_severity is not None:
        sev = conv_value.worst_severity
        when = (
            f" runt {conv_value.peak_window.start_utc.isoformat()}"
            if conv_value.peak_window is not None
            else ""
        )
        assessment_parts.append(
            f"Konvektionsrisk {sev.value}{when}."
        )
    elif conv_value is not None:
        assessment_parts.append("Stabil atmosfär i dagens horisont, ingen konvektionsrisk.")
    else:
        assessment_parts.append("Konvektions-utsikt kunde inte beräknas (saknad data).")
    if wind_value is not None and wind_value.peak_gust_mps is not None:
        assessment_parts.append(
            f"Topp byvind {wind_value.peak_gust_mps:.1f} m/s."
        )
    assessment = " ".join(assessment_parts)

    # --- Section 2: supporting data, each tied to a citation.
    if conv_value is not None and conv_citation_id is not None:
        if conv_value.peak_cape is not None:
            supporting.append(
                DataPoint(
                    label="peak CAPE",
                    value=round(conv_value.peak_cape, 1),
                    unit="J/kg",
                    valid_time_utc=(
                        conv_value.peak_window.start_utc
                        if conv_value.peak_window is not None
                        else None
                    ),
                    citation_id=conv_citation_id,
                )
            )
        if conv_value.min_lifted_index is not None:
            supporting.append(
                DataPoint(
                    label="min lifted index",
                    value=round(conv_value.min_lifted_index, 2),
                    unit="K",
                    valid_time_utc=(
                        conv_value.peak_window.start_utc
                        if conv_value.peak_window is not None
                        else None
                    ),
                    citation_id=conv_citation_id,
                )
            )
        if conv_value.peak_pop is not None:
            supporting.append(
                DataPoint(
                    label="max precipitation probability",
                    value=round(conv_value.peak_pop, 1),
                    unit="%",
                    citation_id=conv_citation_id,
                )
            )
    if wind_value is not None and wind_citation_id is not None:
        if wind_value.peak_gust_mps is not None:
            supporting.append(
                DataPoint(
                    label="peak byvind",
                    value=round(wind_value.peak_gust_mps, 2),
                    unit="m/s",
                    valid_time_utc=(
                        wind_value.peak_window.start_utc
                        if wind_value.peak_window is not None
                        else None
                    ),
                    citation_id=wind_citation_id,
                )
            )
        if wind_value.peak_wind_mps is not None:
            supporting.append(
                DataPoint(
                    label="peak medelvind",
                    value=round(wind_value.peak_wind_mps, 2),
                    unit="m/s",
                    citation_id=wind_citation_id,
                )
            )
    if ctx.solar_day.available and ctx.solar_day.value is not None:
        supporting.extend(_solar_day_data(ctx.solar_day.value))
    if ctx.irradiance.available and ctx.irradiance.value is not None:
        pair = _peak_from_series(ctx.irradiance.value)
        if pair is not None:
            supporting.extend(pair)

    # --- Section 4: operational consequences.
    if conv_value is not None and conv_value.worst_severity is not None:
        sev = conv_value.worst_severity
        if sev in (AlertSeverity.WARNING, AlertSeverity.CRITICAL):
            consequences.append(
                "Planera ett säkert stopp-fönster runt konvektionsperioden; "
                "åska och byfronter är icke-förhandlingsbara stoppvillkor."
            )
            consequences.append(
                "Lad batteri i morgontimmarna — eftermiddagens laddning kan "
                "försämras av molnsvep från konvektionscellerna."
            )
        else:
            consequences.append(
                "Håll konvektionsindex under uppsikt; en WATCH kan växa till "
                "WARNING om CAPE stiger eller LI faller ytterligare."
            )
    else:
        consequences.append(
            "Standarddrift — inget konvektionshot avlett ur dagens indexen."
        )
    if wind_value is not None and wind_value.peak_gust_mps is not None:
        consequences.append(
            "Granska fordonets stabilitetsbudget mot topp-byvinden; "
            "sidvindskomponenten beror på kursen."
        )

    # --- Section 5: watch_next (next things to monitor, with trigger).
    if conv_value is not None and conv_value.peak_window is not None:
        watch.append(
            WatchItem(
                what="Konvektions-fönster",
                when=conv_value.peak_window.start_utc.isoformat(),
                trigger="CAPE stiger eller LI faller vidare mot tröskelbanden",
                why="Markerar när ett säkert stopp kan bli nödvändigt.",
            )
        )
    if wind_value is not None and wind_value.peak_window is not None:
        watch.append(
            WatchItem(
                what="Topp byvind",
                when=wind_value.peak_window.start_utc.isoformat(),
                trigger="wind_gusts_10m över stabilitetsgräns",
                why="Byvind-topparna styr säker körhastighet mer än medelvinden.",
            )
        )
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

    # --- Section 6: warnings & limits. Convection-specific caveats join
    # the static ones so the briefing is honest about index skill.
    warnings = _warnings_from_state(ctx)
    warnings.append(
        "CAPE och lifted index är MODELLHÄRLEDDA prognos-index, inte "
        "observationer; grid-/timupplösning begränsar skickligheten och "
        "eftermiddagskonvektion i inlandet är notoriskt svår att tajma."
    )
    warnings.append(
        "Konvektionströsklarna är GENERISKA (Galway 1956, SPC, AMS); de är "
        "inte kalibrerade mot Stuart Highway torrsäsong."
    )
    warnings.append(
        "Convective inhibition (CIN) är inte vägd in — en hög-CAPE / hög-CIN "
        "atmosfär kan vara kapad och aldrig konvektera."
    )
    if conv_value is not None and conv_value.satellite_cell_hint is None:
        warnings.append(
            "Satellit-nowcast av växande celler (Himawari/SatCORPS) är ännu "
            "inte integrerad i konvektionsutsikten — kommande kalibrering."
        )
    # Fas 3: if the briefing just emitted a convection warning but the
    # platform has no local CAPE/LI pairs to verify against, SAY SO.
    if conv_value is not None and conv_value.worst_severity is not None:
        sk = ctx.self_knowledge.value if ctx.self_knowledge.available else None
        unverified = (
            sk is None
            or not sk.archive_available
            or sk.coverage_by_variable.get("cape") is None
            or sk.coverage_by_variable["cape"].sample_count == 0
            or sk.coverage_by_variable.get("lifted_index") is None
            or sk.coverage_by_variable["lifted_index"].sample_count == 0
        )
        if unverified:
            warnings.append(
                "Konvektionsindexen är INTE verifierade på denna plattform "
                "(inga lagrade CAPE/LI-par) — behandla varningen som "
                "vägledning, inte som en kalibrerad siffra."
            )

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
