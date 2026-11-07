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
    mode: PositionMode = PositionMode.HISTORICAL_REPLAY,
    fix_quality: FixQuality = FixQuality.SIMULATED,
) -> VehicleTelemetrySample:
    now = utc_now()
    when = now + timedelta(seconds=seconds_from_now)
    fix = PositionFix(
        point=GeoPoint(latitude_deg=lat, longitude_deg=lon),
        mode=mode,
        source="unit test",
        received_at_utc=now,
        gps_time_utc=when,
        fix_quality=fix_quality,
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


async def test_a_live_gps_jump_is_parked_as_suspect_not_promoted_to_trusted_state() -> None:
    """A jump between live_gps samples now follows the same rule as the HTTP
    endpoint: park the suspect fix on ActiveState, keep the previous trusted
    fix authoritative. Freezing on a glitch was the old danger; silently
    teleporting on a glitch was a worse one."""
    store = StateStore()
    coordinator = TelemetryCoordinator(store)
    samples = [
        _sample(
            seconds_from_now=-2.0, lat=-14.46, lon=132.26,
            source=TelemetrySource.LIVE_RADIO,
            mode=PositionMode.LIVE_GPS, fix_quality=FixQuality.GPS,
        ),
        _sample(
            seconds_from_now=-1.0, lat=-14.00, lon=132.26,  # ~51 km in 1 s
            source=TelemetrySource.LIVE_RADIO,
            mode=PositionMode.LIVE_GPS, fix_quality=FixQuality.GPS,
        ),
    ]
    stats = await coordinator.consume(
        FakeReader(samples, source=TelemetrySource.LIVE_RADIO)
    )
    assert stats.samples_applied == 1
    assert stats.suspect_parked == 1
    assert stats.position_jumps_logged == 1
    assert store.state.fix is not None
    assert store.state.fix.point.latitude_deg == pytest.approx(-14.46)  # first, trusted
    assert store.state.suspect_fix is not None
    assert store.state.suspect_fix.point.latitude_deg == pytest.approx(-14.00)
    assert store.state.suspect_fix.fix_quality is FixQuality.UNKNOWN


async def test_a_replay_jump_is_applied_because_replay_cadence_is_synthetic() -> None:
    """Replay streams reconstruct historical data at wall-clock speed; two
    samples 'a second apart' are really minutes or hours apart in the
    original run. The live-fix gate would wrongly reject ordinary driving.
    Replay therefore skips the gate entirely."""
    store = StateStore()
    coordinator = TelemetryCoordinator(store)
    samples = [
        _sample(seconds_from_now=-2.0, lat=-14.46, lon=132.26),
        _sample(seconds_from_now=-1.0, lat=-14.00, lon=132.26),
    ]
    stats = await coordinator.consume(FakeReader(samples))
    assert stats.samples_applied == 2
    assert stats.suspect_parked == 0


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


async def test_a_future_dated_live_sample_is_dropped_by_the_freshness_gate() -> None:
    """Old code accepted anything within |age| <= limit. A sender with a
    skewed clock could time-stamp a sample 30 minutes in the future and have
    it pass. The gate now bounds age to [-grace, +limit]."""
    store = StateStore()
    coordinator = TelemetryCoordinator(store)
    ahead = _sample(seconds_from_now=60.0, source=TelemetrySource.LIVE_RADIO)
    stats = await coordinator.consume(
        FakeReader([ahead], source=TelemetrySource.LIVE_RADIO)
    )
    assert stats.stale_dropped == 1
    assert stats.samples_applied == 0


async def test_a_jump_is_decided_by_implied_speed_not_absolute_distance() -> None:
    """The old fixed-distance threshold confused cadence with travel: a
    500 m hop between samples a second apart is a 500 m/s jump but passes
    a fixed-distance gate; a 500 m hop ten minutes apart is normal driving
    but fails one. Implied speed is the physically meaningful test."""
    store = StateStore()
    coordinator = TelemetryCoordinator(store)
    # 500 m in 50 s = 10 m/s - a plausible solar-car speed, must not flag.
    # Well inside the live-freshness limit so the freshness gate never
    # interferes with the jump test.
    samples = [
        _sample(
            seconds_from_now=-50.0, lat=-14.4600, lon=132.26,
            source=TelemetrySource.LIVE_RADIO,
            mode=PositionMode.LIVE_GPS, fix_quality=FixQuality.GPS,
        ),
        _sample(
            seconds_from_now=0.0, lat=-14.4555, lon=132.26,  # ~500 m
            source=TelemetrySource.LIVE_RADIO,
            mode=PositionMode.LIVE_GPS, fix_quality=FixQuality.GPS,
        ),
    ]
    stats = await coordinator.consume(
        FakeReader(samples, source=TelemetrySource.LIVE_RADIO)
    )
    assert stats.position_jumps_logged == 0
    assert stats.samples_applied == 2
