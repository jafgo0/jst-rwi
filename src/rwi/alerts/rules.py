"""Operational alert rules.

Alerts are derived, never invented: each one names the values that triggered it
and carries the provenance of those values. Thresholds are explicit, declared in
:class:`AlertThresholds`, and documented as assumptions until the team confirms
them against the vehicle's measured limits and the official regulations.

Three families of alert exist, and they are deliberately kept apart:
  * weather hazards (gusts, crosswind, rain, visibility, heat, irradiance loss);
  * data integrity problems (stale, missing or implausible data, provider down,
    position unavailable) - a platform that hides its own blindness is worse
    than one that has no data;
  * energy/strategy consequences (projected deficit, infeasible scenarios).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from rwi.contracts import (
    Alert,
    AlertCategory,
    AlertSeverity,
    ProviderHealth,
    RouteWeatherForecast,
    ScenarioResult,
    WeatherSeries,
)
from rwi.core.position import PositionFix, PositionMode
from rwi.core.provenance import DataKind, Provenance, QualityFlag
from rwi.core.timebase import TimeWindow, ensure_utc, utc_now
from rwi.core.units import ms_to_kmh


@dataclass(frozen=True)
class AlertThresholds:
    """Trigger levels. Every value here is an ASSUMPTION pending team review.

    The crosswind and gust levels in particular depend on the vehicle's measured
    stability envelope; see the organisers' "Stability Considerations" paper and
    replace these with the team's own figures before the race.
    """

    gust_watch_mps: float = 13.9          # ~50 km/h
    gust_warning_mps: float = 19.4        # ~70 km/h
    gust_critical_mps: float = 25.0       # ~90 km/h
    crosswind_watch_mps: float = 8.0
    crosswind_warning_mps: float = 12.0
    crosswind_critical_mps: float = 16.0
    precipitation_watch_mm: float = 0.5
    precipitation_warning_mm: float = 5.0
    visibility_warning_m: float = 2000.0
    visibility_critical_m: float = 500.0
    heat_watch_c: float = 38.0
    heat_warning_c: float = 43.0
    irradiance_drop_fraction: float = 0.35
    """Relative GHI fall between consecutive steps that counts as a cloud event."""
    irradiance_drop_floor_w_m2: float = 150.0
    """Ignore relative drops below this absolute level: near sunset they are normal."""
    max_observation_age_s: float = 1800.0
    max_forecast_age_s: float = 10800.0
    max_position_age_s: float = 30.0
    energy_margin_warning_fraction: float = 0.05
    """End-of-day state of charge this close above the reserve is a warning."""
    source: str = "assumed - not yet confirmed against vehicle limits or regulations"


_ALERT_PROVIDER = "rwi"
_ALERT_DATASET = "alerts"


@dataclass
class _Builder:
    now: datetime
    items: list[Alert] = field(default_factory=list)

    def add(
        self,
        *,
        alert_id: str,
        severity: AlertSeverity,
        category: AlertCategory,
        headline: str,
        detail: str,
        evidence: list[str],
        recommended_action: str | None = None,
        valid_window: TimeWindow | None = None,
        distance_along_route_m: float | None = None,
        location: object | None = None,
    ) -> None:
        self.items.append(
            Alert(
                id=alert_id,
                severity=severity,
                category=category,
                headline=headline,
                detail=detail,
                issued_at_utc=self.now,
                valid_window=valid_window,
                location=location,  # type: ignore[arg-type]
                distance_along_route_m=distance_along_route_m,
                evidence=evidence,
                recommended_action=recommended_action,
                provenance=Provenance(
                    provider=_ALERT_PROVIDER,
                    dataset=_ALERT_DATASET,
                    kind=DataKind.DERIVED,
                    retrieved_at_utc=self.now,
                    processing="threshold_rules",
                ),
            )
        )


def _severity_for(
    value: float, watch: float, warning: float, critical: float
) -> AlertSeverity | None:
    if value >= critical:
        return AlertSeverity.CRITICAL
    if value >= warning:
        return AlertSeverity.WARNING
    if value >= watch:
        return AlertSeverity.WATCH
    return None


def weather_alerts(
    series: WeatherSeries,
    *,
    thresholds: AlertThresholds | None = None,
    now: datetime | None = None,
    horizon_s: float = 6 * 3600.0,
) -> list[Alert]:
    """Hazards found in a single-point weather series within the horizon."""
    th = thresholds or AlertThresholds()
    builder = _Builder(now=ensure_utc(now) if now else utc_now())
    limit = builder.now + timedelta(seconds=horizon_s)
    kind_label = "forecast" if series.kind is DataKind.FORECAST else series.kind.value

    previous_ghi: tuple[datetime, float] | None = None
    for step in series.steps:
        if step.valid_time_utc > limit:
            break
        window = TimeWindow(
            start_utc=step.valid_time_utc, end_utc=step.valid_time_utc + timedelta(hours=1)
        )
        gust = step.value("wind_gusts_10m")
        if gust is not None:
            severity = _severity_for(
                gust, th.gust_watch_mps, th.gust_warning_mps, th.gust_critical_mps
            )
            if severity is not None:
                builder.add(
                    alert_id=f"gust-{step.valid_time_utc.isoformat()}",
                    severity=severity,
                    category=AlertCategory.GUST,
                    headline=f"Wind gusts {ms_to_kmh(gust):.0f} km/h",
                    detail=(
                        f"{kind_label} gusts of {gust:.1f} m/s ({ms_to_kmh(gust):.0f} km/h) "
                        f"valid {step.valid_time_utc.isoformat()}"
                    ),
                    evidence=[f"wind_gusts_10m={gust:.1f} m/s ({series.provider})"],
                    recommended_action=(
                        "Review vehicle stability limits and driver briefing before this period."
                    ),
                    valid_window=window,
                    location=series.point,
                )

        precip = step.value("precipitation")
        if precip is not None and precip >= th.precipitation_watch_mm:
            severity = (
                AlertSeverity.WARNING
                if precip >= th.precipitation_warning_mm
                else AlertSeverity.WATCH
            )
            probability = step.value("precipitation_probability")
            builder.add(
                alert_id=f"precip-{step.valid_time_utc.isoformat()}",
                severity=severity,
                category=AlertCategory.PRECIPITATION,
                headline=f"Rain {precip:.1f} mm expected",
                detail=(
                    f"{kind_label} precipitation {precip:.1f} mm"
                    + (f", probability {probability:.0f} %" if probability is not None else "")
                    + f" valid {step.valid_time_utc.isoformat()}"
                ),
                evidence=[f"precipitation={precip:.1f} mm ({series.provider})"],
                recommended_action=(
                    "Expect reduced irradiance and wet road; reassess speed and braking margins."
                ),
                valid_window=window,
                location=series.point,
            )

        visibility = step.value("visibility")
        if visibility is not None and visibility <= th.visibility_warning_m:
            severity = (
                AlertSeverity.CRITICAL
                if visibility <= th.visibility_critical_m
                else AlertSeverity.WARNING
            )
            builder.add(
                alert_id=f"visibility-{step.valid_time_utc.isoformat()}",
                severity=severity,
                category=AlertCategory.VISIBILITY,
                headline=f"Low visibility {visibility:.0f} m",
                detail=(
                    f"{kind_label} visibility {visibility:.0f} m "
                    f"at {step.valid_time_utc.isoformat()}"
                ),
                evidence=[f"visibility={visibility:.0f} m ({series.provider})"],
                recommended_action="Confirm convoy spacing and lighting requirements.",
                valid_window=window,
                location=series.point,
            )

        temperature = step.value("temperature_2m")
        if temperature is not None and temperature >= th.heat_watch_c:
            severity = (
                AlertSeverity.WARNING if temperature >= th.heat_warning_c else AlertSeverity.WATCH
            )
            builder.add(
                alert_id=f"heat-{step.valid_time_utc.isoformat()}",
                severity=severity,
                category=AlertCategory.HEAT,
                headline=f"Air temperature {temperature:.0f} degC",
                detail=(
                    f"{kind_label} air temperature {temperature:.1f} degC at "
                    f"{step.valid_time_utc.isoformat()}; expect reduced array and battery "
                    "efficiency and higher cockpit load"
                ),
                evidence=[f"temperature_2m={temperature:.1f} degC ({series.provider})"],
                recommended_action="Check battery and driver cooling margins.",
                valid_window=window,
                location=series.point,
            )

        ghi = step.value("ghi")
        if ghi is not None:
            if previous_ghi is not None:
                before_time, before = previous_ghi
                if before >= th.irradiance_drop_floor_w_m2 and before > 0.0:
                    drop = (before - ghi) / before
                    if drop >= th.irradiance_drop_fraction:
                        builder.add(
                            alert_id=f"irradiance-{step.valid_time_utc.isoformat()}",
                            severity=(
                                AlertSeverity.WARNING if drop >= 0.6 else AlertSeverity.WATCH
                            ),
                            category=AlertCategory.IRRADIANCE_DROP,
                            headline=f"Irradiance falling {drop * 100:.0f} %",
                            detail=(
                                f"GHI drops from {before:.0f} to {ghi:.0f} W/m2 between "
                                f"{before_time.isoformat()} and {step.valid_time_utc.isoformat()} "
                                f"({kind_label}); likely cloud."
                            ),
                            evidence=[
                                f"ghi {before:.0f} -> {ghi:.0f} W/m2 ({series.provider})",
                            ],
                            recommended_action=(
                                "Re-run the energy forecast; consider conserving charge before "
                                "the cloud band."
                            ),
                            valid_window=window,
                            location=series.point,
                        )
            previous_ghi = (step.valid_time_utc, ghi)
    return builder.items


def route_alerts(
    forecast: RouteWeatherForecast,
    *,
    thresholds: AlertThresholds | None = None,
    now: datetime | None = None,
) -> list[Alert]:
    """Crosswind and headwind hazards located along the predicted trajectory."""
    th = thresholds or AlertThresholds()
    builder = _Builder(now=ensure_utc(now) if now else utc_now())
    for sample in forecast.samples:
        rel = sample.relative_wind
        if rel is None:
            continue
        magnitude = abs(rel.crosswind_mps)
        severity = _severity_for(
            magnitude,
            th.crosswind_watch_mps,
            th.crosswind_warning_mps,
            th.crosswind_critical_mps,
        )
        if severity is None:
            continue
        side = "right" if rel.crosswind_mps > 0 else "left"
        point = sample.trajectory_point
        builder.add(
            alert_id=f"crosswind-{point.distance_m:.0f}",
            severity=severity,
            category=AlertCategory.CROSSWIND,
            headline=f"Crosswind {magnitude:.0f} m/s from the {side}",
            detail=(
                f"At {point.distance_m / 1000:.0f} km the forecast crosswind component is "
                f"{magnitude:.1f} m/s from the {side} (apparent wind yaw "
                f"{rel.yaw_angle_deg:.0f} deg), estimated arrival {point.eta_utc.isoformat()}"
            ),
            evidence=[
                f"crosswind={rel.crosswind_mps:.1f} m/s",
                f"headwind={rel.headwind_mps:.1f} m/s",
                f"provider={forecast.provider}",
            ],
            recommended_action="Brief the driver; consider reducing speed through this segment.",
            distance_along_route_m=point.distance_m,
            location=point.point,
        )
    return builder.items


def data_integrity_alerts(
    *,
    series: list[WeatherSeries] | None = None,
    fix: PositionFix | None = None,
    suspect_fix: PositionFix | None = None,
    provider_health: list[ProviderHealth] | None = None,
    thresholds: AlertThresholds | None = None,
    now: datetime | None = None,
) -> list[Alert]:
    """Alerts about the platform's own data, not about the weather."""
    th = thresholds or AlertThresholds()
    builder = _Builder(now=ensure_utc(now) if now else utc_now())

    if fix is None:
        builder.add(
            alert_id="position-unavailable",
            severity=AlertSeverity.CRITICAL,
            category=AlertCategory.POSITION,
            headline="Position unavailable",
            detail=(
                "No position fix is available, so no location-dependent value can be "
                "computed. Nothing is being substituted."
            ),
            evidence=["active state has no fix"],
            recommended_action="Restore GPS/telemetry or set a manual coordinate.",
        )
    else:
        age = fix.age_s(builder.now)
        if age > th.max_position_age_s:
            builder.add(
                alert_id="position-stale",
                severity=(
                    AlertSeverity.CRITICAL
                    if age > 10 * th.max_position_age_s
                    else AlertSeverity.WARNING
                ),
                category=AlertCategory.POSITION,
                headline=f"Position {age:.0f} s old",
                detail=(
                    f"Latest {fix.mode.value} fix from {fix.source} is {age:.0f} s old "
                    f"(limit {th.max_position_age_s:.0f} s)."
                ),
                evidence=[f"fix_quality={fix.fix_quality.value}", f"age_s={age:.0f}"],
                recommended_action="Check the telemetry link before trusting route progress.",
                location=fix.point,
            )
        skew = fix.clock_skew_s
        if skew is not None and abs(skew) > 60.0:
            builder.add(
                alert_id="position-clock-skew",
                severity=AlertSeverity.WARNING,
                category=AlertCategory.DATA_QUALITY,
                headline=f"GPS clock skew {skew:.0f} s",
                detail=(
                    "The GPS timestamp and the receipt time disagree by more than a minute, "
                    "so solar geometry and ETA may be computed for the wrong instant."
                ),
                evidence=[f"clock_skew_s={skew:.0f}"],
                recommended_action="Verify time synchronisation on the telemetry source.",
                location=fix.point,
            )
        if fix.mode in {PositionMode.ROUTE_SIM, PositionMode.HISTORICAL_REPLAY}:
            builder.add(
                alert_id="position-simulated",
                severity=AlertSeverity.INFO,
                category=AlertCategory.POSITION,
                headline=f"Simulation mode: {fix.mode.value}",
                detail="Position is simulated. Values on screen are not live race data.",
                evidence=[f"mode={fix.mode.value}", f"source={fix.source}"],
                location=fix.point,
            )

    if suspect_fix is not None and fix is not None:
        from rwi.core.geo import geodesic_distance_m

        jump = geodesic_distance_m(fix.point, suspect_fix.point)
        suspect_age = suspect_fix.age_s(builder.now)
        trusted_age = fix.age_s(builder.now)
        builder.add(
            alert_id="position-suspect-jump",
            severity=AlertSeverity.CRITICAL,
            category=AlertCategory.POSITION,
            headline=f"Suspect GPS jump {jump / 1000:.1f} km - ignored",
            detail=(
                f"The last live_gps sample claimed a position "
                f"{jump / 1000:.1f} km from the previous one, which is physically "
                "implausible. It is NOT being used for weather, route or strategy; "
                f"the last trusted fix ({trusted_age:.0f} s old) is still "
                "authoritative. Confirm by posting a manual fix, or wait for a "
                "fresh trusted telemetry sample."
            ),
            evidence=[
                f"suspect_lat={suspect_fix.point.latitude_deg:.4f}",
                f"suspect_lon={suspect_fix.point.longitude_deg:.4f}",
                f"jump_m={jump:.0f}",
                f"suspect_age_s={suspect_age:.0f}",
                f"trusted_age_s={trusted_age:.0f}",
            ],
            recommended_action=(
                "Verify the telemetry link; if the suspect position is correct, "
                "post a manual fix to accept it."
            ),
            location=fix.point,
        )

    for item in series or []:
        base = item.base_provenance()
        age = base.age_s(builder.now)
        limit = (
            th.max_observation_age_s
            if item.kind is DataKind.OBSERVATION
            else th.max_forecast_age_s
        )
        if age > limit:
            builder.add(
                alert_id=f"stale-{item.provider}-{item.dataset}",
                severity=AlertSeverity.WARNING,
                category=AlertCategory.DATA_QUALITY,
                headline=f"{item.provider} data {age / 60:.0f} min old",
                detail=(
                    f"{item.kind.value} series from {base.label()} was retrieved "
                    f"{age / 60:.0f} min ago (limit {limit / 60:.0f} min). Displayed as stale, "
                    "not as current."
                ),
                evidence=[f"retrieved_at_utc={base.retrieved_at_utc.isoformat()}"],
                recommended_action="Refresh the provider or switch to a fallback source.",
            )
        flagged = [
            f"{name}:{value.quality.value}"
            for step in item.steps
            for name, value in step.values.items()
            if value.quality not in {QualityFlag.OK, QualityFlag.INTERPOLATED}
        ]
        if flagged:
            builder.add(
                alert_id=f"quality-{item.provider}-{item.dataset}",
                severity=AlertSeverity.WATCH,
                category=AlertCategory.DATA_QUALITY,
                headline=f"{len(flagged)} flagged values from {item.provider}",
                detail="Quality control flagged values in this series; see evidence.",
                evidence=sorted(set(flagged))[:10],
                recommended_action="Inspect the series before using it for decisions.",
            )

    for health in provider_health or []:
        if not health.reachable:
            builder.add(
                alert_id=f"provider-{health.provider}",
                severity=AlertSeverity.WARNING,
                category=AlertCategory.PROVIDER,
                headline=f"Provider unreachable: {health.provider}",
                detail=health.message or "No successful response from this provider.",
                evidence=[f"checked_at_utc={health.checked_at_utc.isoformat()}"],
                recommended_action="Fallback provider in use; verify network and rate limits.",
            )
    return builder.items


def strategy_alerts(
    results: list[ScenarioResult],
    *,
    thresholds: AlertThresholds | None = None,
    now: datetime | None = None,
) -> list[Alert]:
    """Energy and feasibility consequences of the evaluated scenarios."""
    th = thresholds or AlertThresholds()
    builder = _Builder(now=ensure_utc(now) if now else utc_now())
    if results and not any(r.feasible for r in results):
        builder.add(
            alert_id="strategy-no-feasible-scenario",
            severity=AlertSeverity.CRITICAL,
            category=AlertCategory.ENERGY_DEFICIT,
            headline="No feasible scenario",
            detail=(
                "Every evaluated scenario violates an energy, regulatory or safety "
                "constraint. Human review required; the platform offers no recommendation."
            ),
            evidence=[
                f"{r.name}: " + ", ".join(v.constraint for v in r.violations) for r in results
            ],
            recommended_action="Reassess the inputs and the day's objective with the strategists.",
        )
    for result in results:
        if result.feasible and result.end_soc_fraction < th.energy_margin_warning_fraction:
            builder.add(
                alert_id=f"strategy-thin-margin-{result.name}",
                severity=AlertSeverity.WATCH,
                category=AlertCategory.ENERGY_DEFICIT,
                headline=f"Thin energy margin in '{result.name}'",
                detail=(
                    f"Scenario '{result.name}' finishes at "
                    f"{result.end_soc_fraction * 100:.1f} % state of charge, leaving little "
                    "room for forecast error."
                ),
                evidence=[
                    f"net_energy_wh={result.net_energy_wh:.0f}",
                    f"end_soc_fraction={result.end_soc_fraction:.3f}",
                ],
                recommended_action=(
                    "Consider a slower scenario or re-check the irradiance forecast."
                ),
            )
    return builder.items


def sort_alerts(alerts: list[Alert]) -> list[Alert]:
    """Most severe first, then earliest issued. Stable and deterministic."""
    order = {
        AlertSeverity.CRITICAL: 0,
        AlertSeverity.WARNING: 1,
        AlertSeverity.WATCH: 2,
        AlertSeverity.INFO: 3,
    }
    return sorted(alerts, key=lambda a: (order[a.severity], a.issued_at_utc, a.id))
