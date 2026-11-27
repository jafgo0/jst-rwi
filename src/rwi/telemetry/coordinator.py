"""Coordinator between a TelemetryReader and the authoritative StateStore.

Why this exists as its own thing: a reader converts bytes to typed samples.
Writing to StateStore - the single source of truth the whole platform reads
from - is a different responsibility. Decoupling them means a bug in the
decoder cannot silently drive the car on screen, and a replay can be sanity-
checked (gaps logged, out-of-order samples refused) before the dashboard sees
anything.

Live samples go through :func:`rwi.core.fix_guard.assess_live_fix`, the same
decision function the HTTP ``POST /api/state/position`` endpoint uses, so an
implausible jump produces the same outcome regardless of transport: the
suspect fix is parked on :class:`ActiveState` and the last trusted fix stays
authoritative for weather, route and strategy. Replayed historical and
simulated streams use a looser path because their cadence is synthetic.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rwi.contracts import TelemetrySource, VehicleTelemetrySample
from rwi.core.errors import RwiError
from rwi.core.fix_guard import FixVerdict, assess_live_fix
from rwi.core.position import FixQuality, PositionFix, PositionMode
from rwi.core.state import StateStore
from rwi.core.timebase import utc_now

if TYPE_CHECKING:
    from rwi.contracts import TelemetryReader

log = logging.getLogger(__name__)


class CoordinatorError(RwiError):
    """An unrecoverable error reached the coordinator."""


@dataclass
class CoordinatorStats:
    """What the coordinator saw. Deliberately a plain dataclass, not a model."""

    samples_received: int = 0
    samples_applied: int = 0
    out_of_order_dropped: int = 0
    stale_dropped: int = 0
    position_jumps_logged: int = 0
    suspect_parked: int = 0
    last_sample_time_utc: object | None = None
    gap_events: list[str] = field(default_factory=list)

    @property
    def dropped(self) -> int:
        return self.out_of_order_dropped + self.stale_dropped


class TelemetryCoordinator:
    """Pump samples from a reader into the StateStore under the guardrails.

    Live telemetry (``LIVE_RADIO``, ``LIVE_CELLULAR`` or ``LIVE_ONBOARD_SENSOR``) and
    simulated streams (``SIMULATED``) are treated as live for the freshness
    gate: a 60-second-old sample means the car has rolled a long way without
    the dashboard knowing it. Replayed historical data skips that gate
    because the age of a replayed sample is meaningless.
    """

    #: Live samples older than this are dropped rather than written to state.
    #: 60 s is forgiving - the radio can drop out for a slot and recover - but
    #: not so forgiving that a stale sample masquerades as current.
    live_freshness_limit_s: float = 60.0

    #: A small negative grace for live samples timestamped slightly in the
    #: future: receivers may stamp a sub-second early, but anything beyond
    #: this is a clock bug and dropping it is safer than trusting the age.
    live_future_grace_s: float = 5.0

    #: Implausible implied speed between consecutive samples. 110 m/s is
    #: about 400 km/h; nothing on the race convoy moves that fast. When
    #: the gate fires the sample is still applied (freezing on the
    #: previous fix is more dangerous), but a jump event is logged and
    #: counted. A fixed-distance threshold was wrong because the implied
    #: speed depends on the cadence, which varies per telemetry link.
    position_jump_speed_threshold_mps: float = 110.0

    def __init__(
        self,
        store: StateStore,
        *,
        project_progress: Callable[[str, PositionFix], float | None] | None = None,
    ) -> None:
        """``project_progress(route_id, fix) -> float | None`` is injected to
        avoid the heavy ``rwi.route`` import inside this module. Passing None
        keeps route progress untouched (the HTTP endpoint still sets it)."""
        self._store = store
        self._project_progress = project_progress
        self.stats = CoordinatorStats()

    async def consume(self, reader: TelemetryReader) -> CoordinatorStats:
        """Pump the reader to end-of-stream, updating state as we go.

        Returns the final :class:`CoordinatorStats`. Never raises on a dropped
        sample - decisions about what to do with a bad feed belong to a
        higher-level operator alert, not to this layer.
        """
        last_trusted: VehicleTelemetrySample | None = None
        is_live = reader.source in {
            TelemetrySource.LIVE_RADIO,
            TelemetrySource.LIVE_CELLULAR,
            TelemetrySource.LIVE_ONBOARD_SENSOR,
            TelemetrySource.SIMULATED,
        }

        try:
            async for sample in reader.stream():
                self.stats.samples_received += 1
                if not self._monotonic_ok(sample, last_trusted):
                    self.stats.out_of_order_dropped += 1
                    continue
                if is_live and not self._fresh_enough(sample):
                    self.stats.stale_dropped += 1
                    continue
                if sample.position is None:
                    self.stats.samples_applied += 1
                    self.stats.last_sample_time_utc = sample.time_utc
                    last_trusted = sample
                    continue

                applied = await self._dispatch_sample(sample, last_trusted, is_live)
                if applied:
                    self.stats.samples_applied += 1
                    self.stats.last_sample_time_utc = sample.time_utc
                    last_trusted = sample
                # If not applied the fix was parked or rejected; the
                # "last trusted sample" baseline for the jump gate stays
                # at the previous accepted one.

            for gap in await reader.gaps_so_far():
                self.stats.gap_events.append(
                    f"{gap.duration_s:.1f} s silence from "
                    f"{gap.start_utc.isoformat()} ({gap.reason or 'no reason given'})"
                )
        finally:
            await reader.close()
        return self.stats

    async def _dispatch_sample(
        self,
        sample: VehicleTelemetrySample,
        last_trusted: VehicleTelemetrySample | None,
        is_live: bool,
    ) -> bool:
        """Route one sample's position fix through the shared gate.

        Returns True if the trusted active state was updated. Live samples
        go through :func:`assess_live_fix` to decide park/accept/reject;
        replayed historical samples skip the gate because their cadence is
        synthetic.
        """
        fix = sample.position
        assert fix is not None  # consume() filters out None

        if is_live and fix.mode is PositionMode.LIVE_GPS:
            previous_fix: PositionFix | None = (
                last_trusted.position if last_trusted is not None else None
            )
            assessment = assess_live_fix(
                candidate=fix,
                previous=previous_fix,
                now=utc_now(),
                jump_speed_threshold_mps=self.position_jump_speed_threshold_mps,
                future_grace_s=self.live_future_grace_s,
                stale_limit_s=self.live_freshness_limit_s,
            )
            if assessment.verdict is FixVerdict.REJECT:
                self.stats.stale_dropped += 1
                log.info("telemetry rejected sample: %s", assessment.reason)
                return False
            if assessment.verdict is FixVerdict.PARK_SUSPECT:
                self.stats.position_jumps_logged += 1
                self.stats.suspect_parked += 1
                log.warning("telemetry parking suspect fix: %s", assessment.reason)
                # Mark the parked fix as UNKNOWN quality so any subsystem
                # that reads it cannot mistake it for a trusted GPS fix.
                parked = fix.model_copy(update={"fix_quality": FixQuality.UNKNOWN})
                await self._store.park_suspect_fix(parked)
                return False

        route_id = self._store.state.route_id
        route_distance_m: float | None = None
        if route_id and self._project_progress is not None:
            try:
                route_distance_m = self._project_progress(route_id, fix)
            except Exception:  # projection failure must not lose the fix
                route_distance_m = None
        await self._store.apply_trusted_fix(
            fix,
            route_id=route_id,
            route_distance_m=route_distance_m,
        )
        return True

    def _monotonic_ok(
        self,
        current: VehicleTelemetrySample,
        previous: VehicleTelemetrySample | None,
    ) -> bool:
        if previous is None:
            return True
        return current.time_utc >= previous.time_utc

    def _fresh_enough(self, sample: VehicleTelemetrySample) -> bool:
        """A live sample's own time must be recent and NOT in the future.

        A clock-skewed sender can time-stamp a sample well ahead of real
        time; the old gate accepted anything within |age| <= limit and so
        let such samples through. We now bound it to the real range
        [-grace, +limit]: a touch of future is tolerated for GPS/host
        clock jitter, anything further is a clock bug.
        """
        age = (utc_now() - sample.time_utc).total_seconds()
        return -self.live_future_grace_s <= age <= self.live_freshness_limit_s


async def run_once(reader: TelemetryReader, store: StateStore) -> CoordinatorStats:
    """Convenience: pump a reader to completion with a fresh coordinator."""
    coordinator = TelemetryCoordinator(store)
    return await coordinator.consume(reader)


__all__ = [
    "CoordinatorError",
    "CoordinatorStats",
    "TelemetryCoordinator",
    "run_once",
]
