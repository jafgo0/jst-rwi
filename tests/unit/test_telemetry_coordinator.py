"""The coordinator between a reader and StateStore.

The decoupling exists so a decoder bug cannot silently drive the car on
screen. These tests pin the gates that make that true: a sample from the past
must not rewind the clock, a stale live sample must not pretend the car is
where it was a minute ago, and a position jump must be visible rather than
silent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from rwi.contracts import (
    TelemetryGap,
    TelemetrySource,
    VehicleTelemetrySample,
)
from rwi.core.geo import GeoPoint
from rwi.core.position import FixQuality, PositionFix, PositionMode
from rwi.core.provenance import DataKind, Provenance
from rwi.core.state import StateStore
from rwi.core.timebase import utc_now
from rwi.telemetry.coordinator import TelemetryCoordinator


def _sample(
    *,
    seconds_from_now: float = 0.0,
    lat: float = -14.46,
    lon: float = 132.26,
    source: TelemetrySource = TelemetrySource.FILE_REPLAY,
) -> VehicleTelemetrySample:
    now = utc_now()
    when = now + timedelta(seconds=seconds_from_now)
    fix = PositionFix(
        point=GeoPoint(latitude_deg=lat, longitude_deg=lon),
        mode=PositionMode.HISTORICAL_REPLAY,
        source="unit test",
        received_at_utc=now,
        gps_time_utc=when,
        fix_quality=FixQuality.SIMULATED,
    )
    return VehicleTelemetrySample(
        time_utc=when,
        source=source,
        position=fix,
        provenance=Provenance(
            provider="t", dataset="t", kind=DataKind.OBSERVATION,
            retrieved_at_utc=now, observation_time_utc=when,
        ),
    )


class FakeReader:
    """A minimal TelemetryReader for the coordinator to pump."""

    def __init__(
        self,
        samples: list[VehicleTelemetrySample],
        source: TelemetrySource = TelemetrySource.FILE_REPLAY,
        gaps: list[TelemetryGap] | None = None,
    ) -> None:
        self._samples = samples
        self.source = source
        self._gaps = gaps or []
        self.closed = False

    async def stream(self) -> AsyncIterator[VehicleTelemetrySample]:
        for sample in self._samples:
            yield sample

    async def gaps_so_far(self) -> list[TelemetryGap]:
        return list(self._gaps)

    async def close(self) -> None:
        self.closed = True


async def test_all_samples_apply_when_the_feed_is_clean() -> None:
    """Baseline: a monotonic, fresh stream reaches the StateStore unchanged."""
    store = StateStore()
    coordinator = TelemetryCoordinator(store)
    samples = [
        _sample(seconds_from_now=-2.0, lat=-14.46),
        _sample(seconds_from_now=-1.0, lat=-14.47),
        _sample(seconds_from_now=0.0, lat=-14.48),
    ]
    stats = await coordinator.consume(FakeReader(samples))
    assert stats.samples_applied == 3
    assert stats.dropped == 0
    assert store.state.fix is not None
    assert store.state.fix.point.latitude_deg == pytest.approx(-14.48)


async def test_a_sample_from_before_the_last_one_is_refused_not_rewinding_state() -> None:
    """A reader bug that re-emits an earlier sample must not make the car on
    screen jump back in time. The sample is counted and dropped."""
    store = StateStore()
    coordinator = TelemetryCoordinator(store)
    samples = [
        _sample(seconds_from_now=-3.0, lat=-14.46),
        _sample(seconds_from_now=0.0, lat=-14.49),
        _sample(seconds_from_now=-2.0, lat=-14.47),  # back in time
    ]
    stats = await coordinator.consume(FakeReader(samples))
    assert stats.samples_applied == 2
    assert stats.out_of_order_dropped == 1
    assert store.state.fix is not None
    assert store.state.fix.point.latitude_deg == pytest.approx(-14.49)


async def test_a_stale_live_sample_is_dropped_rather_than_dating_the_dashboard() -> None:
    """On the live feed a two-minute-old sample means the car has rolled
    without the dashboard knowing it. Freshness gate must drop it."""
    store = StateStore()
    coordinator = TelemetryCoordinator(store)
    stale = _sample(seconds_from_now=-120.0, source=TelemetrySource.LIVE_RADIO)
    stats = await coordinator.consume(
        FakeReader([stale], source=TelemetrySource.LIVE_RADIO)
    )
    assert stats.stale_dropped == 1
    assert stats.samples_applied == 0
    assert store.state.fix is None


async def test_the_freshness_gate_does_not_fire_on_a_replay_source() -> None:
    """Replaying historical BWSC 2025 data means every sample is a year old
    by construction. Freshness is meaningless for a replay and must not drop
    anything."""
    store = StateStore()
    coordinator = TelemetryCoordinator(store)
    ancient = _sample(seconds_from_now=-3600.0)
    stats = await coordinator.consume(FakeReader([ancient]))
    assert stats.samples_applied == 1
    assert stats.stale_dropped == 0


async def test_a_position_jump_is_logged_but_the_sample_still_applies() -> None:
    """Freezing the state at the old position when the next sample disagrees
    is more dangerous than a visible outlier. Count it, log it, apply it."""
    store = StateStore()
    coordinator = TelemetryCoordinator(store)
    samples = [
        _sample(seconds_from_now=-2.0, lat=-14.46, lon=132.26),
        _sample(seconds_from_now=-1.0, lat=-14.00, lon=132.26),  # ~51 km jump
    ]
    stats = await coordinator.consume(FakeReader(samples))
    assert stats.samples_applied == 2
    assert stats.position_jumps_logged == 1


async def test_gap_events_from_the_reader_are_carried_through() -> None:
    """A dropout the reader detected must reach the operator, not vanish."""
    store = StateStore()
    coordinator = TelemetryCoordinator(store)
    gap = TelemetryGap(
        start_utc=datetime(2025, 7, 8, 15, 0, tzinfo=UTC),
        end_utc=datetime(2025, 7, 8, 15, 1, tzinfo=UTC),
        expected_cadence_s=1.0,
        missing_samples_estimate=60,
        reason="radio dropout",
    )
    stats = await coordinator.consume(FakeReader([], gaps=[gap]))
    assert len(stats.gap_events) == 1
    assert "radio dropout" in stats.gap_events[0]


async def test_the_reader_is_always_closed_even_when_the_coordinator_catches_nothing() -> None:
    """A finally-close contract: resources must not leak when there are no
    samples to process."""
    store = StateStore()
    coordinator = TelemetryCoordinator(store)
    reader = FakeReader([])
    await coordinator.consume(reader)
    assert reader.closed is True


async def test_samples_with_no_position_do_not_touch_state() -> None:
    """A battery-only sample is useful data but it must not clear or set the
    fix. StateStore stays unchanged."""
    store = StateStore()
    coordinator = TelemetryCoordinator(store)
    no_pos = _sample().model_copy(update={"position": None})
    stats = await coordinator.consume(FakeReader([no_pos]))
    assert stats.samples_applied == 1
    assert store.state.fix is None
