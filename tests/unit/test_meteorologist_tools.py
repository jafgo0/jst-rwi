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


# --------------------------------------------------------------------- convection / wind

from datetime import timedelta  # noqa: E402

from rwi.contracts import WeatherSeries, WeatherStep  # noqa: E402
from rwi.core.geo import GeoPoint  # noqa: E402
from rwi.core.provenance import measured  # noqa: E402

_POINT = GeoPoint(latitude_deg=-23.698, longitude_deg=133.8807, elevation_m=545.0)


def _c_step(offset_h: float, **values: float) -> WeatherStep:
    t = NOW + timedelta(hours=offset_h)
    prov = Provenance(
        provider="openmeteo",
        dataset="forecast",
        kind=DataKind.FORECAST,
        retrieved_at_utc=NOW,
        valid_time_utc=t,
        point=_POINT,
    )
    return WeatherStep(
        valid_time_utc=t,
        values={k: measured(k, v, prov) for k, v in values.items()},
    )


def _c_series(*steps: WeatherStep) -> WeatherSeries:
    return WeatherSeries(
        point=_POINT,
        requested_point=_POINT,
        provider="openmeteo",
        dataset="forecast",
        kind=DataKind.FORECAST,
        model_name="icon",
        retrieved_at_utc=NOW,
        temporal_resolution_s=3600.0,
        steps=list(steps),
    )


def test_convection_outlook_none_is_unavailable() -> None:
    result = mt.convection_outlook(None, now=NOW, horizon_s=12 * 3600.0)
    assert not result.available
    assert "ingen väderserie" in result.reason


def test_convection_outlook_without_indices_is_unavailable_not_stable() -> None:
    series = _c_series(_c_step(2, temperature_2m=28.0))
    result = mt.convection_outlook(series, now=NOW, horizon_s=12 * 3600.0)
    assert not result.available
    assert "cape" in result.reason


def test_convection_outlook_stable_atmosphere_is_available_with_no_severity() -> None:
    series = _c_series(_c_step(2, cape=50.0, lifted_index=5.0))
    result = mt.convection_outlook(series, now=NOW, horizon_s=12 * 3600.0)
    assert result.available
    assert result.value is not None
    assert result.value.worst_severity is None
    assert result.value.peak_window is None
    assert "stabil" in result.value.verdict


def test_convection_outlook_peak_is_the_worst_step_in_horizon() -> None:
    series = _c_series(
        _c_step(1, cape=500.0, lifted_index=-1.0),
        _c_step(5, cape=2800.0, lifted_index=-5.0, precipitation_probability=70.0),
        _c_step(9, cape=1200.0, lifted_index=-3.0),
    )
    result = mt.convection_outlook(series, now=NOW, horizon_s=12 * 3600.0)
    assert result.available
    out = result.value
    assert out is not None
    assert out.worst_severity is AlertSeverity.CRITICAL
    assert out.peak_cape == 2800.0
    assert out.min_lifted_index == -5.0
    assert out.peak_pop == 70.0
    assert out.satellite_cell_hint is None  # future seam, not wired yet


def test_wind_outlook_extracts_peak_gust_window() -> None:
    series = _c_series(
        _c_step(1, wind_speed_10m=5.0, wind_gusts_10m=9.0, wind_direction_10m=180.0),
        _c_step(4, wind_speed_10m=8.0, wind_gusts_10m=18.5, wind_direction_10m=220.0),
    )
    result = mt.wind_outlook(series, now=NOW, horizon_s=12 * 3600.0)
    assert result.available
    out = result.value
    assert out is not None
    assert out.peak_gust_mps == 18.5
    assert out.dominant_direction_deg == 220.0


# ---------------------------------------------------------------- synoptic_outlook (#10, #14)

from rwi.alerts.synoptic import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    FrontSignature,
    SynopticAnalysis,
)


def _front_sig(name: str, h: float, strength: str = "signature") -> FrontSignature:
    t = NOW + timedelta(hours=h)
    return FrontSignature(
        name=name,
        position=GeoPoint(latitude_deg=-29.0, longitude_deg=134.75),
        route_distance_m=2_180_000.0,
        passage_time_utc=t,
        criteria_met=("pressure_trough", "wind_shift", "dewpoint_drop"),
        criteria_evaluated=("pressure_trough", "wind_shift", "dewpoint_drop"),
        strength=strength,  # type: ignore[arg-type]
        dp_rise_pa=150.0,
        wind_shift_deg=110.0,
        dt_24h_k=None,
        dtd_k=-8.0,
        provenance=Provenance(
            provider="rwi", dataset="synoptic", kind=DataKind.DERIVED, retrieved_at_utc=NOW
        ),
    )


def _analysis(
    *fronts: FrontSignature, points: tuple[str, ...] = ("A", "B", "C")
) -> SynopticAnalysis:
    return SynopticAnalysis(
        issued_at_utc=NOW,
        points=points,
        missing=(),
        tendencies=(),
        meridional_profile=(),
        fronts=fronts,
        front_speed_m_s=None,
        front_direction=None,
        thresholds=DEFAULT_THRESHOLDS,
        provider="openmeteo",
        model_name="ecmwf_ifs025",
    )


def test_synoptic_outlook_none_is_unavailable() -> None:
    assert not mt.synoptic_outlook(None, now=NOW, horizon_s=86400).available


def test_synoptic_outlook_too_few_points_is_unavailable_and_names_missing() -> None:
    a = SynopticAnalysis(
        issued_at_utc=NOW,
        points=("A",),
        missing=("B", "C"),
        tendencies=(),
        meridional_profile=(),
        fronts=(),
        front_speed_m_s=None,
        front_direction=None,
        thresholds=DEFAULT_THRESHOLDS,
    )
    r = mt.synoptic_outlook(a, now=NOW, horizon_s=86400)
    assert not r.available
    assert "B, C" in r.reason


def test_synoptic_outlook_filters_to_horizon_and_cites_engine() -> None:
    r = mt.synoptic_outlook(
        _analysis(_front_sig("Coober Pedy", 6), _front_sig("Adelaide", 40)),
        now=NOW,
        horizon_s=24 * 3600,
    )
    assert r.available and r.value is not None and r.citation is not None
    assert [f.name for f in r.value.fronts] == ["Coober Pedy"]
    assert "Coober Pedy" in r.reason and "Adelaide" not in r.reason
    assert r.citation.source_module == "rwi.alerts.synoptic"
    assert r.citation.model_name == "ecmwf_ifs025"
    assert r.citation.valid_time_utc == NOW + timedelta(hours=6)


def test_synoptic_outlook_without_fronts_says_so() -> None:
    r = mt.synoptic_outlook(_analysis(), now=NOW, horizon_s=86400)
    assert r.available
    assert "ingen frontsignatur" in r.reason


def test_synoptic_outlook_possible_only() -> None:
    r = mt.synoptic_outlook(
        _analysis(_front_sig("Alice Springs", 3, "possible")), now=NOW, horizon_s=86400
    )
    assert r.reason.startswith("möjlig front")
