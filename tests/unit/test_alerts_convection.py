"""Convection alerts: threshold matrix + graceful degradation.

The rules consume CAPE / lifted_index / precipitation_probability from
a WeatherSeries and never invent a value: a step without the indices
is simply skipped, not treated as 'stable'. These tests pin the four
severity bands and the empty-series behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rwi.alerts.convection import ConvectionThresholds, convection_alerts
from rwi.contracts import AlertSeverity, WeatherSeries, WeatherStep
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance, measured

NOW = datetime(2026, 11, 8, 0, 0, tzinfo=UTC)
POINT = GeoPoint(latitude_deg=-23.698, longitude_deg=133.8807, elevation_m=545.0)


def _step(offset_h: float, **values: float) -> WeatherStep:
    t = NOW + timedelta(hours=offset_h)
    prov = Provenance(
        provider="openmeteo",
        dataset="forecast",
        kind=DataKind.FORECAST,
        retrieved_at_utc=NOW,
        valid_time_utc=t,
        point=POINT,
    )
    return WeatherStep(
        valid_time_utc=t,
        values={k: measured(k, v, prov) for k, v in values.items()},
    )


def _series(*steps: WeatherStep) -> WeatherSeries:
    return WeatherSeries(
        point=POINT,
        requested_point=POINT,
        provider="openmeteo",
        dataset="forecast",
        kind=DataKind.FORECAST,
        model_name="icon",
        retrieved_at_utc=NOW,
        temporal_resolution_s=3600.0,
        steps=list(steps),
    )


def test_series_with_no_indices_returns_no_alerts() -> None:
    series = _series(_step(1, temperature_2m=30.0, wind_speed_10m=5.0))
    assert convection_alerts(series, now=NOW) == []


def test_watch_fires_on_lifted_index_alone_at_zero() -> None:
    series = _series(_step(2, lifted_index=0.0))
    alerts = convection_alerts(series, now=NOW)
    assert len(alerts) == 1
    assert alerts[0].severity is AlertSeverity.WATCH


def test_watch_fires_on_cape_alone_above_the_floor() -> None:
    series = _series(_step(2, cape=600.0))
    alerts = convection_alerts(series, now=NOW)
    assert len(alerts) == 1
    assert alerts[0].severity is AlertSeverity.WATCH


def test_warning_fires_when_two_bands_clear_warning_level() -> None:
    series = _series(_step(2, cape=1200.0, lifted_index=-2.5))
    alerts = convection_alerts(series, now=NOW)
    assert len(alerts) == 1
    assert alerts[0].severity is AlertSeverity.WARNING


def test_critical_requires_all_three_bands_at_critical() -> None:
    series = _series(
        _step(2, cape=2800.0, lifted_index=-5.0, precipitation_probability=70.0)
    )
    alerts = convection_alerts(series, now=NOW)
    assert len(alerts) == 1
    assert alerts[0].severity is AlertSeverity.CRITICAL


def test_critical_bands_without_pop_only_reach_warning() -> None:
    # CAPE crit + LI crit but PoP missing -> two-of-three WARN path.
    series = _series(_step(2, cape=2800.0, lifted_index=-5.0))
    alerts = convection_alerts(series, now=NOW)
    assert len(alerts) == 1
    assert alerts[0].severity is AlertSeverity.WARNING


def test_each_alert_carries_citation_and_caveats_in_evidence() -> None:
    series = _series(_step(2, cape=1200.0, lifted_index=-3.0))
    alert = convection_alerts(series, now=NOW)[0]
    joined = " || ".join(alert.evidence)
    assert "Galway" in joined  # LI source
    assert "Moncrieff" in joined or "SPC" in joined  # CAPE source
    assert "CIN not evaluated" in joined
    assert "Stuart Highway" in joined


def test_horizon_truncates_future_steps() -> None:
    series = _series(
        _step(2, cape=1200.0, lifted_index=-3.0),
        _step(20, cape=3500.0, lifted_index=-6.0, precipitation_probability=80.0),
    )
    alerts = convection_alerts(series, now=NOW, horizon_s=6 * 3600.0)
    # Only the first step is inside the 6 h horizon; the CRITICAL at t+20 h
    # is correctly ignored.
    assert len(alerts) == 1
    assert alerts[0].severity is AlertSeverity.WARNING


def test_custom_thresholds_are_honoured() -> None:
    th = ConvectionThresholds(cape_watch=100.0)
    series = _series(_step(2, cape=150.0))
    alerts = convection_alerts(series, now=NOW, thresholds=th)
    assert len(alerts) == 1
