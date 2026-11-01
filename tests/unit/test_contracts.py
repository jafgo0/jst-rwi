"""The cross-module contracts, including the physical guards they enforce."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rwi.contracts import (
    IrradianceComponents,
    Route,
    RouteWaypoint,
    WaypointKind,
    WeatherSeries,
    WeatherStep,
)
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance, measured

NOW = datetime(2027, 8, 24, 3, 0, tzinfo=UTC)


def _prov(kind: DataKind = DataKind.CLEAR_SKY_MODEL, valid: datetime | None = None) -> Provenance:
    return Provenance(
        provider="rwi",
        dataset="test",
        kind=kind,
        retrieved_at_utc=NOW,
        valid_time_utc=valid if kind is DataKind.FORECAST else None,
    )


def test_sun_below_horizon_cannot_have_positive_irradiance(jonkoping: GeoPoint) -> None:
    """The master specification's hardest rule, enforced by the type itself."""
    with pytest.raises(ValueError, match="sun below horizon"):
        IrradianceComponents(
            time_utc=NOW,
            point=jonkoping,
            ghi_w_m2=120.0,
            dni_w_m2=0.0,
            dhi_w_m2=120.0,
            kind=DataKind.CLEAR_SKY_MODEL,
            provenance=_prov(),
            solar_elevation_deg=-8.0,
        )


def test_night_with_all_components_zero_is_valid(jonkoping: GeoPoint) -> None:
    night = IrradianceComponents(
        time_utc=NOW,
        point=jonkoping,
        ghi_w_m2=0.0,
        dni_w_m2=0.0,
        dhi_w_m2=0.0,
        kind=DataKind.CLEAR_SKY_MODEL,
        provenance=_prov(),
        solar_elevation_deg=-8.0,
    )
    assert night.ghi_w_m2 == 0.0


def test_irradiance_components_expose_registry_checked_measurements(darwin: GeoPoint) -> None:
    comps = IrradianceComponents(
        time_utc=NOW,
        point=darwin,
        ghi_w_m2=820.0,
        dni_w_m2=900.0,
        dhi_w_m2=95.0,
        kind=DataKind.CLEAR_SKY_MODEL,
        provenance=_prov(),
        solar_elevation_deg=62.0,
    )
    as_measured = comps.as_measured()
    assert set(as_measured) == {"ghi", "dni", "dhi"}
    assert all(m.unit == "W/m2" for m in as_measured.values())


def _series(times: list[datetime], kind: DataKind = DataKind.FORECAST) -> WeatherSeries:
    steps = [
        WeatherStep(
            valid_time_utc=t,
            values={"temperature_2m": measured("temperature_2m", 20.0 + i, _prov(kind, t))},
        )
        for i, t in enumerate(times)
    ]
    return WeatherSeries(
        point=GeoPoint(latitude_deg=-23.7, longitude_deg=133.9),
        provider="open-meteo",
        dataset="forecast",
        kind=kind,
        retrieved_at_utc=NOW,
        steps=steps,
    )


def test_series_must_be_time_sorted() -> None:
    with pytest.raises(ValueError, match="sorted"):
        _series([NOW + timedelta(hours=2), NOW])


def test_series_lookup_and_tolerance() -> None:
    series = _series([NOW + timedelta(hours=h) for h in range(4)])
    assert series.variables == frozenset({"temperature_2m"})
    assert len(series.series("temperature_2m")) == 4
    nearest = series.nearest_step(NOW + timedelta(minutes=70))
    assert nearest is not None
    assert nearest.valid_time_utc == NOW + timedelta(hours=1)
    assert series.nearest_step(NOW + timedelta(days=5), tolerance_s=1800.0) is None
    assert series.series("cloud_cover") == []


def test_series_base_provenance_keeps_the_kind() -> None:
    forecast = _series([NOW + timedelta(hours=1)]).base_provenance()
    assert forecast.kind is DataKind.FORECAST
    assert forecast.is_prediction


def test_route_requires_monotonic_distances(darwin: GeoPoint, adelaide: GeoPoint) -> None:
    waypoints = [
        RouteWaypoint(point=darwin, cumulative_distance_m=0.0, kind=WaypointKind.START),
        RouteWaypoint(point=adelaide, cumulative_distance_m=100.0, kind=WaypointKind.FINISH),
    ]
    route = Route(
        id="t",
        name="test",
        waypoints=waypoints,
        total_distance_m=100.0,
        provenance=_prov(DataKind.SIMULATION),
        source_description="synthetic two-point route for tests",
    )
    assert route.control_stops == []
    # Distances that go backwards along the route: each value is individually
    # valid, so only the route-level check can catch the ordering mistake.
    bad = [
        waypoints[0],
        RouteWaypoint(point=adelaide, cumulative_distance_m=100.0),
        RouteWaypoint(point=darwin, cumulative_distance_m=50.0, kind=WaypointKind.FINISH),
    ]
    with pytest.raises(ValueError, match="non-decreasing"):
        Route(
            id="t",
            name="test",
            waypoints=bad,
            total_distance_m=100.0,
            provenance=_prov(DataKind.SIMULATION),
            source_description="bad",
        )


def test_a_route_needs_at_least_two_points(darwin: GeoPoint) -> None:
    with pytest.raises(ValueError, match="two waypoints"):
        Route(
            id="t",
            name="test",
            waypoints=[RouteWaypoint(point=darwin, cumulative_distance_m=0.0)],
            total_distance_m=0.0,
            provenance=_prov(DataKind.SIMULATION),
            source_description="single point",
        )
