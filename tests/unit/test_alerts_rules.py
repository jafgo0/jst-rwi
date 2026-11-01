"""Alert rules: they must fire on real hazards and stay silent otherwise."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rwi.alerts import (
    AlertThresholds,
    data_integrity_alerts,
    route_alerts,
    sort_alerts,
    strategy_alerts,
    weather_alerts,
)
from rwi.contracts import (
    AlertCategory,
    AlertSeverity,
    ProviderHealth,
    RouteWeatherForecast,
    RouteWeatherSample,
    ScenarioConstraintViolation,
    ScenarioResult,
    TrajectoryPoint,
    WeatherSeries,
    WeatherStep,
)
from rwi.core.geo import GeoPoint
from rwi.core.position import FixQuality, PositionFix, PositionMode
from rwi.core.provenance import DataKind, Provenance, QualityFlag, measured
from rwi.core.wind import relative_wind

NOW = datetime(2027, 8, 24, 3, 0, tzinfo=UTC)
POINT = GeoPoint(latitude_deg=-14.47, longitude_deg=132.26, elevation_m=108.0)


def _series(values: list[dict[str, float]], *, retrieved: datetime = NOW) -> WeatherSeries:
    steps = []
    for index, mapping in enumerate(values):
        valid = NOW + timedelta(hours=index)
        prov = Provenance(
            provider="open-meteo",
            dataset="forecast",
            kind=DataKind.FORECAST,
            retrieved_at_utc=retrieved,
            valid_time_utc=valid,
            model_name="ecmwf_ifs025",
        )
        steps.append(
            WeatherStep(
                valid_time_utc=valid,
                values={name: measured(name, value, prov) for name, value in mapping.items()},
            )
        )
    return WeatherSeries(
        point=POINT,
        provider="open-meteo",
        dataset="forecast",
        kind=DataKind.FORECAST,
        retrieved_at_utc=retrieved,
        steps=steps,
    )


def test_calm_weather_raises_nothing() -> None:
    series = _series([{"wind_gusts_10m": 5.0, "precipitation": 0.0, "temperature_2m": 24.0}] * 3)
    assert weather_alerts(series, now=NOW) == []


def test_gusts_escalate_with_severity() -> None:
    series = _series([{"wind_gusts_10m": 14.0}, {"wind_gusts_10m": 20.0}, {"wind_gusts_10m": 26.0}])
    alerts = weather_alerts(series, now=NOW)
    severities = [a.severity for a in alerts if a.category is AlertCategory.GUST]
    assert severities == [AlertSeverity.WATCH, AlertSeverity.WARNING, AlertSeverity.CRITICAL]
    assert all(a.evidence for a in alerts)
    assert all("m/s" in a.evidence[0] for a in alerts)


def test_rain_and_heat_and_visibility_fire() -> None:
    series = _series(
        [
            {
                "precipitation": 6.0,
                "precipitation_probability": 80.0,
                "temperature_2m": 44.0,
                "visibility": 400.0,
            }
        ]
    )
    categories = {a.category for a in weather_alerts(series, now=NOW)}
    assert AlertCategory.PRECIPITATION in categories
    assert AlertCategory.HEAT in categories
    assert AlertCategory.VISIBILITY in categories


def test_irradiance_drop_is_detected_but_not_at_dusk() -> None:
    cloud = _series([{"ghi": 900.0}, {"ghi": 300.0}])
    assert any(a.category is AlertCategory.IRRADIANCE_DROP for a in weather_alerts(cloud, now=NOW))
    # The same relative fall from a low base near sunset is normal, not an event.
    dusk = _series([{"ghi": 90.0}, {"ghi": 30.0}])
    assert not any(
        a.category is AlertCategory.IRRADIANCE_DROP for a in weather_alerts(dusk, now=NOW)
    )


def test_alerts_beyond_the_horizon_are_ignored() -> None:
    series = _series([{"wind_gusts_10m": 5.0}] * 3 + [{"wind_gusts_10m": 30.0}])
    assert weather_alerts(series, now=NOW, horizon_s=2 * 3600.0) == []
    assert weather_alerts(series, now=NOW, horizon_s=6 * 3600.0)


def test_crosswind_alert_names_the_side_and_the_place() -> None:
    point = TrajectoryPoint(
        distance_m=512_000.0,
        point=POINT,
        eta_utc=NOW + timedelta(hours=2),
        heading_deg=180.0,
        speed_mps=25.0,
    )
    # Driving south with a wind from the west: it arrives on the vehicle's right.
    rel = relative_wind(
        vehicle_speed_mps=25.0, vehicle_heading_deg=180.0, wind_speed_mps=14.0, wind_from_deg=90.0
    )
    forecast = RouteWeatherForecast(
        route_id="bwsc",
        generated_at_utc=NOW,
        provider="open-meteo",
        samples=[
            RouteWeatherSample(
                trajectory_point=point, series_point=POINT, values={}, relative_wind=rel
            )
        ],
    )
    alerts = route_alerts(forecast, now=NOW)
    assert len(alerts) == 1
    assert alerts[0].category is AlertCategory.CROSSWIND
    assert alerts[0].severity is AlertSeverity.WARNING
    assert alerts[0].distance_along_route_m == 512_000.0
    assert "512 km" in alerts[0].detail


def test_missing_position_is_a_critical_alert() -> None:
    alerts = data_integrity_alerts(fix=None, now=NOW)
    assert alerts[0].severity is AlertSeverity.CRITICAL
    assert alerts[0].category is AlertCategory.POSITION
    assert "Nothing is being substituted" in alerts[0].detail


def test_stale_position_and_clock_skew() -> None:
    fix = PositionFix(
        point=POINT,
        mode=PositionMode.LIVE_GPS,
        source="vehicle_telemetry",
        received_at_utc=NOW,
        gps_time_utc=NOW - timedelta(minutes=5),
        fix_quality=FixQuality.GPS,
    )
    ids = {a.id for a in data_integrity_alerts(fix=fix, now=NOW)}
    assert "position-stale" in ids
    assert "position-clock-skew" in ids


def test_simulation_mode_is_always_announced() -> None:
    fix = PositionFix(
        point=POINT,
        mode=PositionMode.ROUTE_SIM,
        source="route_sim",
        received_at_utc=NOW,
        gps_time_utc=NOW,
        fix_quality=FixQuality.SIMULATED,
    )
    alerts = data_integrity_alerts(fix=fix, now=NOW)
    simulation = [a for a in alerts if a.id == "position-simulated"]
    assert len(simulation) == 1
    assert "not live race data" in simulation[0].detail


def test_stale_weather_is_reported_as_stale() -> None:
    old = _series([{"temperature_2m": 25.0}], retrieved=NOW - timedelta(hours=6))
    alerts = data_integrity_alerts(series=[old], fix=None, now=NOW)
    stale = [a for a in alerts if a.id.startswith("stale-")]
    assert len(stale) == 1
    assert "360 min old" in stale[0].headline


def test_quality_flags_surface_as_an_alert() -> None:
    prov = Provenance(
        provider="open-meteo",
        dataset="forecast",
        kind=DataKind.FORECAST,
        retrieved_at_utc=NOW,
        valid_time_utc=NOW,
    )
    series = WeatherSeries(
        point=POINT,
        provider="open-meteo",
        dataset="forecast",
        kind=DataKind.FORECAST,
        retrieved_at_utc=NOW,
        steps=[
            WeatherStep(
                valid_time_utc=NOW,
                values={
                    "temperature_2m": measured(
                        "temperature_2m", 95.0, prov, quality=QualityFlag.OUT_OF_RANGE
                    )
                },
            )
        ],
    )
    alerts = data_integrity_alerts(series=[series], fix=None, now=NOW)
    quality = [a for a in alerts if a.id.startswith("quality-")]
    assert len(quality) == 1
    assert "temperature_2m:out_of_range" in quality[0].evidence


def test_unreachable_provider_is_reported() -> None:
    health = ProviderHealth(
        provider="bom", reachable=False, checked_at_utc=NOW, message="timeout after 15 s"
    )
    alerts = data_integrity_alerts(provider_health=[health], fix=None, now=NOW)
    provider = [a for a in alerts if a.id == "provider-bom"]
    assert provider and provider[0].detail == "timeout after 15 s"


def _scenario(
    name: str, *, feasible: bool, end_soc: float, violations: list[str] | None = None
) -> ScenarioResult:
    return ScenarioResult(
        name=name,
        description=name,
        target_speed_kmh=80.0,
        distance_m=500_000.0,
        energy_generated_wh=4000.0,
        energy_consumed_wh=5000.0,
        net_energy_wh=-1000.0,
        start_soc_fraction=0.8,
        end_soc_fraction=end_soc,
        arrival_utc=NOW + timedelta(hours=7),
        travel_time_s=25_200.0,
        feasible=feasible,
        violations=[
            ScenarioConstraintViolation(constraint=v, detail=v, severity="blocking")
            for v in (violations or [])
        ],
        provenance=Provenance(
            provider="rwi", dataset="strategy", kind=DataKind.DERIVED, retrieved_at_utc=NOW
        ),
    )


def test_no_feasible_scenario_is_critical_and_offers_no_recommendation() -> None:
    results = [
        _scenario("conservative", feasible=False, end_soc=0.0, violations=["battery_reserve"]),
        _scenario("faster", feasible=False, end_soc=0.0, violations=["legal_speed"]),
    ]
    alerts = strategy_alerts(results, now=NOW)
    assert alerts[0].severity is AlertSeverity.CRITICAL
    assert "Human review required" in alerts[0].detail


def test_thin_margin_is_a_watch_not_a_failure() -> None:
    alerts = strategy_alerts([_scenario("balanced", feasible=True, end_soc=0.02)], now=NOW)
    assert [a.severity for a in alerts] == [AlertSeverity.WATCH]


def test_sorting_is_most_severe_first_and_deterministic() -> None:
    series = _series([{"wind_gusts_10m": 26.0, "temperature_2m": 39.0}])
    alerts = sort_alerts(weather_alerts(series, now=NOW) + data_integrity_alerts(fix=None, now=NOW))
    assert alerts[0].severity is AlertSeverity.CRITICAL
    assert [a.severity for a in alerts] == sorted(
        [a.severity for a in alerts],
        key=lambda s: [
            AlertSeverity.CRITICAL,
            AlertSeverity.WARNING,
            AlertSeverity.WATCH,
            AlertSeverity.INFO,
        ].index(s),
    )
    assert sort_alerts(alerts) == alerts


def test_thresholds_are_declared_as_assumptions() -> None:
    assert "assumed" in AlertThresholds().source
