"""The deterministic composer: fixed Swedish prose over typed inputs.

Invariant tests, not language fluency tests. We verify that:
* the six sections are always filled;
* ``uncertainty`` is never empty; an 'unproven' fallback appears when
  no evidence is available;
* static unknowns are always present;
* data points resolve to citations;
* numbers in prose come from citations (via the numeric guard).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from rwi.contracts import (
    Alert,
    AlertCategory,
    AlertSeverity,
    EnsembleSummary,
    VerificationScore,
)
from rwi.core.geo import GeoPoint
from rwi.core.position import ActiveState, PositionFix, PositionMode
from rwi.core.provenance import DataKind, Provenance
from rwi.meteorologist import (
    BriefingContext,
    QuestionKind,
    ToolResult,
    compose,
)
from rwi.meteorologist import tools as mt
from rwi.meteorologist.composer import STATIC_UNKNOWNS
from rwi.meteorologist.numeric_guard import check_prose

JONKOPING = GeoPoint(latitude_deg=57.7826, longitude_deg=14.1618, elevation_m=227.0)
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def _prov() -> Provenance:
    return Provenance(
        provider="openmeteo",
        dataset="fx",
        kind=DataKind.FORECAST,
        retrieved_at_utc=NOW,
        valid_time_utc=NOW,
    )


def _empty_ctx() -> BriefingContext:
    """Context in which every tool is unavailable."""
    return BriefingContext(
        state=ToolResult.unavailable("x"),
        solar_day=ToolResult.unavailable("x"),
        route_weather=ToolResult.unavailable("x"),
        irradiance=ToolResult.unavailable("x"),
        ensemble_ghi=mt.ensemble_summary(None, variable="ghi"),
        ensemble_wind=mt.ensemble_summary(None, variable="wind_speed_10m"),
        disagreements_ghi=mt.model_disagreements(None, variable="ghi"),
        verification_ghi=mt.verification_skill(
            None, variable="ghi", lead_time_s=6 * 3600.0
        ),
        alerts=ToolResult.unavailable("x"),
    )


def _state_fixture() -> ActiveState:
    fix = PositionFix(
        point=JONKOPING,
        mode=PositionMode.MANUAL,
        source="operator",
        received_at_utc=NOW,
    )
    return ActiveState(mode=PositionMode.MANUAL, fix=fix)


def test_risk_ahead_fills_six_sections_even_when_everything_is_unavailable() -> None:
    briefing = compose(QuestionKind.RISK_AHEAD, _empty_ctx(), asked_at=NOW)
    assert briefing.assessment  # section 1
    assert briefing.supporting_data == []  # section 2 may be empty
    assert briefing.uncertainty  # section 3 always has content
    assert briefing.operational_consequences  # section 4
    assert briefing.watch_next == []  # section 5 can be empty without data
    assert briefing.warnings_and_limits  # section 6
    # unknowns must list the static platform gaps PLUS the tool reasons
    for line in STATIC_UNKNOWNS:
        assert line in briefing.unknowns
    assert any("ruttprognos" in u for u in briefing.unknowns)


def test_uncertainty_fallback_is_unproven_when_no_evidence() -> None:
    briefing = compose(QuestionKind.RISK_AHEAD, _empty_ctx(), asked_at=NOW)
    kinds = {item.kind for item in briefing.uncertainty}
    # Fas 3: the track-record row is obligatory in every briefing. The
    # unproven fallback still appears when nothing else is available.
    assert kinds == {"unproven", "track_record"}


def test_track_record_item_is_present_in_every_briefing_kind() -> None:
    """Fas 3: self-knowledge must lead the uncertainty section.

    When no archive summary is available the item still appears, with
    kind='track_record' and a statement that says the skill is obevisad
    lokalt - so the user never reads a briefing that silently hides what
    the platform does not know about itself.
    """
    for kind in (
        QuestionKind.RISK_AHEAD,
        QuestionKind.FORECAST_RELIABILITY,
        QuestionKind.MORNING_BRIEFING,
    ):
        briefing = compose(kind, _empty_ctx(), asked_at=NOW, variable="ghi")
        first = briefing.uncertainty[0]
        assert first.kind == "track_record"
        assert "obevisad lokalt" in first.statement


def test_ensemble_spread_populates_uncertainty_with_citation() -> None:
    summary = EnsembleSummary(
        variable="ghi",
        valid_time_utc=NOW,
        point=JONKOPING,
        unit="W/m^2",
        member_count=51,
        mean=200.0,
        median=195.0,
        minimum=100.0,
        maximum=350.0,
        standard_deviation=60.0,
        percentiles={"10": 120.0, "90": 310.0},
        provider="openmeteo",
        model_name="ecmwf_ifs_ens",
        provenance=_prov(),
    )
    ctx = _empty_ctx()
    ctx.ensemble_ghi = mt.ensemble_summary([summary], variable="ghi")
    briefing = compose(QuestionKind.RISK_AHEAD, ctx, asked_at=NOW)
    kinds = [item.kind for item in briefing.uncertainty]
    assert "ensemble_spread" in kinds
    spread_item = next(i for i in briefing.uncertainty if i.kind == "ensemble_spread")
    assert spread_item.citation_id is not None
    assert briefing.citation(spread_item.citation_id) is not None


def test_critical_alert_climbs_to_assessment_headline() -> None:
    alert = Alert(
        id="a1",
        severity=AlertSeverity.CRITICAL,
        category=AlertCategory.THUNDERSTORM,
        headline="Thunderstorm risk at ETA 15:00",
        detail="d",
        issued_at_utc=NOW,
        provenance=_prov(),
    )
    ctx = _empty_ctx()
    ctx.alerts = mt.active_alerts([alert])
    briefing = compose(QuestionKind.RISK_AHEAD, ctx, asked_at=NOW)
    assert briefing.assessment.startswith("Högsta aktiva varningen: critical")


def test_forecast_reliability_without_skill_says_so_explicitly() -> None:
    ctx = _empty_ctx()
    briefing = compose(
        QuestionKind.FORECAST_RELIABILITY, ctx, asked_at=NOW, variable="ghi"
    )
    assert "Ingen verifierad skicklighet" in briefing.assessment
    assert any("obevisad" in c for c in briefing.operational_consequences)


def test_forecast_reliability_with_meaningful_score_cites_mae() -> None:
    score = VerificationScore(
        variable="ghi",
        provider="openmeteo",
        model_name="icon",
        lead_time_bucket_s=(0.0, 6 * 3600.0),
        sample_count=120,
        mean_absolute_error=45.3,
        root_mean_square_error=72.0,
        skill_vs_persistence=0.18,
        unit="W/m^2",
        provenance=_prov(),
    )
    ctx = _empty_ctx()
    ctx.verification_ghi = mt.verification_skill(
        score, variable="ghi", lead_time_s=6 * 3600.0
    )
    briefing = compose(
        QuestionKind.FORECAST_RELIABILITY, ctx, asked_at=NOW, variable="ghi"
    )
    assert "45.3" in briefing.assessment or "45.30" in briefing.assessment
    assert any(dp.label == "ghi MAE" for dp in briefing.supporting_data)


def test_suspect_fix_triggers_a_warning() -> None:
    fix = PositionFix(
        point=JONKOPING,
        mode=PositionMode.LIVE_GPS,
        source="t",
        received_at_utc=NOW,
    )
    suspect = fix.model_copy(
        update={
            "point": GeoPoint(latitude_deg=58.0, longitude_deg=14.1, elevation_m=227.0)
        }
    )
    state = ActiveState(mode=PositionMode.LIVE_GPS, fix=fix, suspect_fix=suspect)
    ctx = _empty_ctx()
    ctx.state = mt.where_and_when(state)
    briefing = compose(QuestionKind.RISK_AHEAD, ctx, asked_at=NOW)
    assert any("Misstänkt GPS" in w for w in briefing.warnings_and_limits)


def test_morning_briefing_critical_convection_climbs_to_assessment() -> None:
    from datetime import timedelta

    from rwi.contracts import WeatherSeries, WeatherStep
    from rwi.core.provenance import measured
    from rwi.meteorologist import tools as mt

    t2 = NOW + timedelta(hours=4)
    prov = Provenance(
        provider="openmeteo",
        dataset="forecast",
        kind=DataKind.FORECAST,
        retrieved_at_utc=NOW,
        valid_time_utc=t2,
        point=JONKOPING,
    )
    series = WeatherSeries(
        point=JONKOPING,
        requested_point=JONKOPING,
        provider="openmeteo",
        dataset="forecast",
        kind=DataKind.FORECAST,
        model_name="icon",
        retrieved_at_utc=NOW,
        temporal_resolution_s=3600.0,
        steps=[
            WeatherStep(
                valid_time_utc=t2,
                values={
                    "cape": measured("cape", 2800.0, prov),
                    "lifted_index": measured("lifted_index", -5.0, prov),
                    "precipitation_probability": measured(
                        "precipitation_probability", 70.0, prov
                    ),
                },
            )
        ],
    )
    ctx = _empty_ctx()
    ctx.convection = mt.convection_outlook(series, now=NOW, horizon_s=12 * 3600.0)
    briefing = compose(QuestionKind.MORNING_BRIEFING, ctx, asked_at=NOW)
    assert briefing.assessment.startswith("Konvektionsrisk critical")
    # CAPE and lifted_index land in supporting_data with the convection citation.
    labels = {dp.label for dp in briefing.supporting_data}
    assert "peak CAPE" in labels
    assert "min lifted index" in labels


def test_morning_briefing_degrades_gracefully_when_convection_unavailable() -> None:
    """When cape/lifted_index are missing the briefing MUST NOT crash.

    The assessment says convection could not be computed; the six
    sections are still filled; the gap shows up in unknowns; the
    uncertainty section falls back to the 'unproven' line.
    """
    briefing = compose(QuestionKind.MORNING_BRIEFING, _empty_ctx(), asked_at=NOW)
    assert briefing.assessment  # section 1
    assert briefing.uncertainty  # section 3 - never empty
    assert briefing.warnings_and_limits  # section 6
    assert any("konvektions" in u.lower() for u in briefing.unknowns)
    assert "MODELLHÄRLEDDA" in " ".join(briefing.warnings_and_limits)
    assert "GENERISKA" in " ".join(briefing.warnings_and_limits)
    assert "CIN" in " ".join(briefing.warnings_and_limits)


def test_free_form_through_deterministic_composer_is_refused() -> None:
    with pytest.raises(ValueError, match="free-form"):
        compose(QuestionKind.FREE_FORM, _empty_ctx(), asked_at=NOW)


def test_deterministic_prose_passes_the_numeric_guard() -> None:
    """A rendered-prose string built from the composer's own fields must
    pass :func:`check_prose` - the fixed Swedish template never
    introduces numbers that are not in supporting_data."""
    state = _state_fixture()
    ctx = _empty_ctx()
    ctx.state = mt.where_and_when(state)
    briefing = compose(QuestionKind.RISK_AHEAD, ctx, asked_at=NOW)
    # Simulate the prose an LLM adapter would emit: concatenate the six
    # sections' own text, which must only draw numbers from citations.
    prose = " ".join(
        [
            briefing.assessment,
            *(dp.label for dp in briefing.supporting_data),
            *(u.statement for u in briefing.uncertainty),
            *briefing.operational_consequences,
            *(w.what + " " + w.when + " " + w.trigger for w in briefing.watch_next),
            *briefing.warnings_and_limits,
        ]
    )
    check_prose(prose, briefing)
