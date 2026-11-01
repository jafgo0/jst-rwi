"""Shared fixtures. Unit tests must not touch the network."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance

# Reference locations used across the suite.
JONKOPING = GeoPoint(latitude_deg=57.7826, longitude_deg=14.1618, elevation_m=227.0)
DARWIN = GeoPoint(latitude_deg=-12.4634, longitude_deg=130.8456, elevation_m=30.0)
ALICE_SPRINGS = GeoPoint(latitude_deg=-23.6980, longitude_deg=133.8807, elevation_m=545.0)
ADELAIDE = GeoPoint(latitude_deg=-34.9285, longitude_deg=138.6007, elevation_m=50.0)


@pytest.fixture
def jonkoping() -> GeoPoint:
    return JONKOPING


@pytest.fixture
def darwin() -> GeoPoint:
    return DARWIN


@pytest.fixture
def alice_springs() -> GeoPoint:
    return ALICE_SPRINGS


@pytest.fixture
def adelaide() -> GeoPoint:
    return ADELAIDE


@pytest.fixture
def race_day_noon_utc() -> datetime:
    """2027-08-24 03:00Z = 12:30 ACST, near local solar noon in the NT."""
    return datetime(2027, 8, 24, 3, 0, tzinfo=UTC)


@pytest.fixture
def swedish_winter_night_utc() -> datetime:
    """2026-01-15 23:00Z = 00:00 local in Jönköping: unambiguous night."""
    return datetime(2026, 1, 15, 23, 0, tzinfo=UTC)


@pytest.fixture
def test_provenance() -> Provenance:
    return Provenance(
        provider="test",
        dataset="fixture",
        kind=DataKind.SIMULATION,
        retrieved_at_utc=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
    )
