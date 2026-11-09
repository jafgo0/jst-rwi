"""Every response carries X-Rwi-Product-Version and X-Rwi-Product-Tier."""

from __future__ import annotations

from fastapi.testclient import TestClient

from rwi import __version__
from rwi.api.app import create_app


def test_state_endpoint_carries_product_version_and_tier() -> None:
    client = TestClient(create_app())
    resp = client.get("/api/state")
    assert resp.status_code == 200
    assert resp.headers["X-Rwi-Product-Version"] == __version__
    assert resp.headers["X-Rwi-Product-Tier"] == "stable"


def test_strategy_endpoint_is_experimental() -> None:
    client = TestClient(create_app())
    resp = client.get("/api/strategy/vehicle")
    assert resp.headers["X-Rwi-Product-Tier"] == "experimental"
    assert resp.headers["X-Rwi-Product-Version"] == __version__


def test_satellite_endpoint_is_internal() -> None:
    client = TestClient(create_app())
    # The actual endpoint may 500 without creds; we only care about the header.
    resp = client.get("/api/satellite/status")
    assert resp.headers["X-Rwi-Product-Tier"] == "internal"
