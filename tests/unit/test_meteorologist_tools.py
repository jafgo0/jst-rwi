"""Tool adapters: they cite, they do not compute.

A tool that had no input produces ``ToolResult.unavailable`` with a
specific reason - never a fabricated value. A tool that had input
produces a result whose citation points at the originating engine.
"""

from __future__ import annotations

from datetime import UTC, datetime

from rwi.contracts import (
    Alert,
    AlertCategory,
    AlertSeverity,
    EnsembleSummary,
    ModelDisagreement,
    SolarDayEvents,
    VerificationScore,
)
from rwi.core.geo import GeoPoint
from rwi.core.position import ActiveState, PositionFix, PositionMode
from rwi.core.provenance import DataKind, Provenance
from rwi.meteorologist import tools as mt

JONKOPING = GeoPoint(latitude_deg=57.7826, longitude_deg=14.1618, elevation_m=227.0)
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def _prov() -> Provenance:
    return Provenance(
        provider="test",
        dataset="fx",
        kind=DataKind.FORECAST,
        retrieved_at_utc=NOW,
        valid_time_utc=NOW,
    )


def test_where_and_when_without_fix_is_unavailable_with_mode() -> None:
    state = ActiveState(mode=PositionMode.MANUAL, fix=None)
    result = mt.where_and_when(state)
    assert not result.available
    assert "manual" in result.reason


def test_where_and_when_with_fix_cites_core_state() -> None:
    fix = PositionFix(
        point=JONKOPING, mode=PositionMode.MANUAL, source="operator", received_at_utc=NOW
    )
    state = ActiveState(mode=PositionMode.MANUAL, fix=fix)
    result = mt.where_and_when(state)
    assert result.available
    assert result.citation is not None
    assert result.citation.source_module == "rwi.core.state"
    assert result.citation.valid_time_utc == NOW


def test_solar_day_none_input_is_unavailable() -> None:
    assert not mt.solar_day(None).available


def test_solar_day_with_events_cites_geometry_module() -> None:
    events = SolarDayEvents(
        point=JONKOPING,
        local_date="2026-11-08",
        timezone_name="Europe/Stockholm",
        sunrise_utc=datetime(2026, 11, 8, 6, 30, tzinfo=UTC),
        sunset_utc=datetime(2026, 11, 8, 15, 0, tzinfo=UTC),
        solar_noon_utc=datetime(2026, 11, 8, 11, 0, tzinfo=UTC),
        transit_elevation_deg=15.0,
        day_length_s=8.5 * 3600.0,
        provenance=_prov(),
    )
    result = mt.solar_day(events)
    assert result.available
    assert result.citation is not None
    assert result.citation.source_module == "rwi.solar.geometry"


def test_ensemble_summary_none_names_the_variable_in_reason() -> None:
    result = mt.ensemble_summary(None, variable="ghi")
    assert not result.available
    assert "ghi" in result.reason


def test_ensemble_summary_ok_builds_citation_with_member_count() -> None:
    summary = EnsembleSummary(
        variable="ghi",
        valid_time_utc=NOW,
        point=JONKOPING,
        unit="W/m^2",
        member_count=51,
        mean=200.0,
        median=190.0,
        minimum=100.0,
        maximum=350.0,
        standard_deviation=60.0,
        percentiles={"10": 120.0, "90": 310.0},
        provider="openmeteo",
        model_name="ecmwf_ifs_ens",
        provenance=_prov(),
    )
    result = mt.ensemble_summary([summary], variable="ghi")
    assert result.available
    assert result.citation is not None
    assert result.citation.source_module == "rwi.ensemble.summary"
    assert "51" in result.citation.description


def test_model_disagreements_empty_is_ok_not_unavailable() -> None:
    """Empty means models agree - a positive statement."""
    result = mt.model_disagreements([], variable="ghi")
    assert result.available
    assert result.value == []
    assert "överens" in result.reason


def test_model_disagreements_none_is_unavailable() -> None:
    result = mt.model_disagreements(None, variable="ghi")
    assert not result.available
    assert "två källor" in result.reason


def test_model_disagreements_filters_to_significant() -> None:
    insig = ModelDisagreement(
        variable="ghi",
        valid_time_utc=NOW,
        point=JONKOPING,
        unit="W/m^2",
        values_by_model={"a": 200.0, "b": 205.0},
        spread=5.0,
        standard_deviation=2.5,
        mean=202.5,
        median=202.5,
        is_significant=False,
        threshold=50.0,
        provenance=_prov(),
    )
    sig = insig.model_copy(
        update={
            "values_by_model": {"a": 100.0, "b": 300.0},
            "spread": 200.0,
            "standard_deviation": 100.0,
            "is_significant": True,
        }
    )
    result = mt.model_disagreements([insig, sig], variable="ghi")
    assert result.available
    assert len(result.value or []) == 1


def test_verification_skill_none_is_unavailable_with_lead_time() -> None:
    result = mt.verification_skill(None, variable="ghi", lead_time_s=6 * 3600.0)
    assert not result.available
    assert "6 h" in result.reason


def test_verification_skill_below_threshold_is_provisional() -> None:
    score = VerificationScore(
        variable="ghi",
        provider="openmeteo",
        model_name="icon",
        lead_time_bucket_s=(0.0, 6 * 3600.0),
        sample_count=5,
        mean_absolute_error=50.0,
        root_mean_square_error=70.0,
        unit="W/m^2",
        provenance=_prov(),
    )
    result = mt.verification_skill(score, variable="ghi", lead_time_s=6 * 3600.0)
    assert result.available
    assert "provisorisk" in result.reason


def test_active_alerts_none_is_unavailable() -> None:
    assert not mt.active_alerts(None).available


def test_active_alerts_empty_is_ok_with_zero() -> None:
    result = mt.active_alerts([])
    assert result.available
    assert result.value == []
    assert "0" in result.reason


def test_active_alerts_nonempty_cites_rules_module() -> None:
    alert = Alert(
        id="a",
        severity=AlertSeverity.WARNING,
        category=AlertCategory.WIND,
        headline="test",
        detail="d",
        issued_at_utc=NOW,
        provenance=_prov(),
    )
    result = mt.active_alerts([alert])
    assert result.available
    assert result.citation is not None
    assert result.citation.source_module == "rwi.alerts.rules"
