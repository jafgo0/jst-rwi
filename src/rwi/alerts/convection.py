"""Convection alerts from forecast CAPE / Lifted Index / precipitation probability.

Why this lives in a separate module from :mod:`rwi.alerts.rules`:

* The thresholds here are index-based, not threshold-on-a-single-observation
  rules. Mixing them into ``weather_alerts`` would bloat that function with
  a different mental model.
* These rules read MODEL-DERIVED INDICES, not observations. The composer
  layer and the briefing explicitly flag that, and keeping the rule set
  isolated makes it obvious that the limitation attaches only to
  convection alerts, not to the gust/precipitation/visibility rules.

Design assumptions (frozen here so we can revise them in one place):

* The thresholds below are GENERIC and documented in the meteorological
  literature; they are NOT calibrated to Stuart Highway dry-season climatology.
* Convective Inhibition (CIN) is not used - a high-CAPE / high-CIN atmosphere
  may never actually convect, so a CAPE-only WATCH will over-warn in capped
  environments. The briefing's limits section calls this out.
* The sign convention for ``lifted_index`` is negative-equals-unstable
  (Galway 1956). The adapter level pins that; see
  tests/unit/test_provider_openmeteo.py::test_lifted_index_sign_convention_is_negative_for_unstable.

References for the threshold bands (all placed in each alert's evidence[]):

* Galway, J. G. (1956). "The Lifted Index as a Predictor of Latent
  Instability." Bull. Amer. Meteor. Soc. 37(10), 528-529.
* Moncrieff, M. W., Miller, M. J. (1976). "The dynamics and simulation
  of tropical cumulonimbus and squall lines." QJRMS 102(432), 373-394.
* NOAA/NWS Storm Prediction Center convective outlook thresholds, public
  documentation (verified 2026-11-08).
* AMS Glossary of Meteorology, s.v. "Convective Available Potential
  Energy".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from rwi.contracts import (
    Alert,
    AlertCategory,
    AlertSeverity,
    WeatherSeries,
)
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import TimeWindow, ensure_utc, utc_now

_ALERT_PROVIDER = "rwi"
_ALERT_DATASET = "alerts"

# Short-form citations placed in evidence[]. The long references are in the
# module docstring so the alert payload stays readable.
_EVIDENCE_LI_SOURCE = (
    "LI bands: Galway 1956 (BAMS 37(10)); AMS Glossary; SPC outlook thresholds"
)
_EVIDENCE_CAPE_SOURCE = (
    "CAPE bands: Moncrieff & Miller 1976 (QJRMS 102); AMS Glossary; "
    "SPC outlook thresholds"
)


@dataclass(frozen=True)
class ConvectionThresholds:
    """Trigger levels. Every value here is an ASSUMPTION pending calibration.

    These are generic literature bands; they have not been calibrated against
    observed Stuart Highway dry-season convective cells. A calibration pass
    against Himawari/SatCORPS-detected cells is pre-race work.
    """

    # Convective Available Potential Energy, J/kg
    cape_watch: float = 500.0
    cape_warning: float = 1000.0
    cape_critical: float = 2500.0

    # Lifted Index (K); negative = unstable (Galway convention)
    li_watch: float = 0.0       # LI <= 0 -> watch
    li_warning: float = -2.0    # LI <= -2 -> warning
    li_critical: float = -4.0   # LI <= -4 -> critical

    # Precipitation probability (%). Used ONLY as a corroborator; without
    # it the index-only path can still fire a WATCH.
    pop_warning: float = 40.0
    pop_critical: float = 60.0


@dataclass(frozen=True)
class _Builder:
    now: datetime
    items: list[Alert] = field(default_factory=list)

    def add(
        self,
        *,
        alert_id: str,
        severity: AlertSeverity,
        headline: str,
        detail: str,
        evidence: list[str],
        valid_window: TimeWindow,
        location: object | None = None,
        recommended_action: str | None = None,
    ) -> None:
        self.items.append(
            Alert(
                id=alert_id,
                severity=severity,
                category=AlertCategory.THUNDERSTORM,
                headline=headline,
                detail=detail,
                issued_at_utc=self.now,
                valid_window=valid_window,
                location=location,  # type: ignore[arg-type]
                evidence=evidence,
                recommended_action=recommended_action,
                provenance=Provenance(
                    provider=_ALERT_PROVIDER,
                    dataset=_ALERT_DATASET,
                    kind=DataKind.DERIVED,
                    retrieved_at_utc=self.now,
                    processing="convection_rules",
                ),
            )
        )


def _severity(
    *,
    cape: float | None,
    li: float | None,
    pop: float | None,
    th: ConvectionThresholds,
) -> AlertSeverity | None:
    """Score a single forecast step. None when the atmosphere is stable.

    CRITICAL requires all three signals to agree; WARNING two of three.
    WATCH fires on either CAPE or LI alone so a capped-but-unstable
    morning is not silently missed.
    """
    cape_crit = cape is not None and cape >= th.cape_critical
    cape_warn = cape is not None and cape >= th.cape_warning
    cape_watch = cape is not None and cape >= th.cape_watch

    li_crit = li is not None and li <= th.li_critical
    li_warn = li is not None and li <= th.li_warning
    li_watch = li is not None and li <= th.li_watch

    pop_crit = pop is not None and pop >= th.pop_critical
    pop_warn = pop is not None and pop >= th.pop_warning

    if cape_crit and li_crit and pop_crit:
        return AlertSeverity.CRITICAL
    strong = sum((cape_warn, li_warn, pop_warn))
    if strong >= 2:
        return AlertSeverity.WARNING
    if cape_watch or li_watch:
        return AlertSeverity.WATCH
    return None


def convection_alerts(
    series: WeatherSeries,
    *,
    thresholds: ConvectionThresholds | None = None,
    now: datetime | None = None,
    horizon_s: float = 12 * 3600.0,
) -> list[Alert]:
    """Thunderstorm risk in a single-point forecast series within the horizon.

    Returns an empty list when the series carries no convection indices,
    which the caller should surface as a data gap rather than "no risk".
    """
    th = thresholds or ConvectionThresholds()
    builder = _Builder(now=ensure_utc(now) if now else utc_now())
    limit = builder.now + timedelta(seconds=horizon_s)

    for step in series.steps:
        if step.valid_time_utc > limit:
            break
        cape = step.value("cape")
        li = step.value("lifted_index")
        if cape is None and li is None:
            continue
        pop = step.value("precipitation_probability")
        severity = _severity(cape=cape, li=li, pop=pop, th=th)
        if severity is None:
            continue
        window = TimeWindow(
            start_utc=step.valid_time_utc,
            end_utc=step.valid_time_utc + timedelta(hours=1),
        )
        evidence: list[str] = []
        if cape is not None:
            evidence.append(f"cape={cape:.0f} J/kg ({series.provider})")
        if li is not None:
            evidence.append(f"lifted_index={li:.1f} K ({series.provider})")
        if pop is not None:
            evidence.append(f"precipitation_probability={pop:.0f} % ({series.provider})")
        evidence.append(_EVIDENCE_CAPE_SOURCE)
        evidence.append(_EVIDENCE_LI_SOURCE)
        evidence.append(
            "CIN not evaluated; a capped column with high CAPE may not convect. "
            "Thresholds are generic literature bands, not calibrated to Stuart Highway."
        )
        headline_parts: list[str] = []
        if cape is not None:
            headline_parts.append(f"CAPE {cape:.0f} J/kg")
        if li is not None:
            headline_parts.append(f"LI {li:.1f}")
        headline = f"Convection {severity.value}: " + ", ".join(headline_parts)
        detail = (
            f"Thunderstorm risk valid {step.valid_time_utc.isoformat()}; "
            + ", ".join(headline_parts)
            + (f", PoP {pop:.0f} %" if pop is not None else "")
        )
        builder.add(
            alert_id=f"convection-{step.valid_time_utc.isoformat()}",
            severity=severity,
            headline=headline,
            detail=detail,
            evidence=evidence,
            valid_window=window,
            location=series.point,
            recommended_action=(
                "Plan a safe stop window; brief driver on lightning and squall-line "
                "gust-front behaviour."
            ),
        )
    return builder.items


__all__ = ["ConvectionThresholds", "convection_alerts"]
