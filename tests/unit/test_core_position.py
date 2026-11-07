"""Positioning: four labelled modes, one authoritative state, no invented location."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rwi.core.errors import PositionUnavailableError, StaleDataError
from rwi.core.geo import GeoPoint
from rwi.core.position import ActiveState, FixQuality, PositionFix, PositionMode
from rwi.core.state import StateStore

NOW = datetime(2027, 8, 24, 3, 0, tzinfo=UTC)


def _fix(mode: PositionMode = PositionMode.LIVE_GPS, **kwargs: object) -> PositionFix:
    defaults: dict[str, object] = {
        "point": GeoPoint(latitude_deg=-14.46, longitude_deg=132.26, elevation_m=108.0),
        "mode": mode,
        "source": "vehicle_telemetry",
        "received_at_utc": NOW,
        "gps_time_utc": NOW,
        "accuracy_m": 2.5,
        "speed_mps": 24.0,
        "course_deg": 181.0,
        "fix_quality": FixQuality.GPS,
        "satellites": 11,
    }
    defaults.update(kwargs)
    return PositionFix(**defaults)  # type: ignore[arg-type]


def test_simulated_modes_are_marked_as_such() -> None:
    assert _fix(PositionMode.ROUTE_SIM).is_simulated
    assert _fix(PositionMode.HISTORICAL_REPLAY).is_simulated
    assert not _fix(PositionMode.LIVE_GPS).is_simulated
    assert not _fix(PositionMode.MANUAL).is_simulated


def test_gps_time_and_receipt_time_are_kept_apart() -> None:
    fix = _fix(gps_time_utc=NOW - timedelta(seconds=12))
    assert fix.clock_skew_s == pytest.approx(-12.0)
    assert fix.age_s(now=NOW) == pytest.approx(12.0)


def test_staleness_detection() -> None:
    fix = _fix(gps_time_utc=NOW - timedelta(seconds=120))
    assert fix.is_stale(30.0, now=NOW)
    assert not fix.is_stale(300.0, now=NOW)


def test_a_fix_cannot_contradict_the_active_mode() -> None:
    with pytest.raises(ValueError, match="does not match active mode"):
        ActiveState(mode=PositionMode.LIVE_GPS, fix=_fix(PositionMode.ROUTE_SIM))


def test_frozen_time_overrides_wall_clock() -> None:
    chosen = datetime(2027, 8, 24, 6, 30, tzinfo=UTC)
    state = ActiveState(mode=PositionMode.MANUAL, selected_time_utc=chosen)
    assert state.is_time_frozen
    assert state.effective_time(now=NOW) == chosen
    live = ActiveState(mode=PositionMode.LIVE_GPS, fix=_fix())
    assert not live.is_time_frozen
    assert live.effective_time(now=NOW) == NOW


def test_heading_falls_back_to_course_over_ground() -> None:
    state = ActiveState(mode=PositionMode.LIVE_GPS, fix=_fix())
    assert state.effective_heading_deg == pytest.approx(181.0)
    explicit = ActiveState(mode=PositionMode.LIVE_GPS, fix=_fix(), heading_deg=95.0)
    assert explicit.effective_heading_deg == pytest.approx(95.0)


async def test_missing_position_raises_instead_of_inventing_one() -> None:
    store = StateStore(ActiveState(mode=PositionMode.LIVE_GPS, fix=None))
    with pytest.raises(PositionUnavailableError, match="no position available"):
        store.require_fix()


async def test_stale_position_is_refused_when_a_limit_is_given() -> None:
    store = StateStore()
    await store.update_fix(_fix(PositionMode.LIVE_GPS, gps_time_utc=NOW - timedelta(minutes=5)))
    with pytest.raises(StaleDataError):
        store.require_fix(max_age_s=30.0, now=NOW)
    assert store.require_fix(max_age_s=3600.0, now=NOW) is not None


async def test_updating_a_fix_switches_the_mode_and_records_history() -> None:
    store = StateStore()
    await store.update_fix(_fix(PositionMode.LIVE_GPS))
    assert store.state.mode is PositionMode.LIVE_GPS
    await store.update_fix(_fix(PositionMode.ROUTE_SIM, source="route_sim"))
    assert store.state.mode is PositionMode.ROUTE_SIM
    assert len(store.recent_fixes()) == 2


async def test_a_new_fix_preserves_the_selected_time_and_route() -> None:
    chosen = datetime(2027, 8, 25, 1, 0, tzinfo=UTC)
    store = StateStore(
        ActiveState(
            mode=PositionMode.MANUAL,
            selected_time_utc=chosen,
            route_id="bwsc-2027",
            route_distance_m=1234.0,
        )
    )
    await store.update_fix(_fix(PositionMode.LIVE_GPS))
    assert store.state.selected_time_utc == chosen
    assert store.state.route_id == "bwsc-2027"


async def test_patch_is_atomic_under_concurrent_fix_updates() -> None:
    """A compound write that reads state.model_copy outside the lock is
    race-prone: a fix landing between the read and the write clobbers that
    fix. patch() must hold the lock across read and write."""
    import asyncio

    store = StateStore()
    await store.update_fix(_fix(PositionMode.LIVE_GPS))

    async def writer_fix() -> None:
        for _ in range(100):
            await store.update_fix(_fix(PositionMode.LIVE_GPS))

    async def writer_patch() -> None:
        for _ in range(100):
            await store.patch(route_distance_m=42.0)

    await asyncio.gather(writer_fix(), writer_patch())
    # Both writers converged; neither clobbered the other's surface.
    assert store.state.fix is not None
    assert store.state.route_distance_m == 42.0
