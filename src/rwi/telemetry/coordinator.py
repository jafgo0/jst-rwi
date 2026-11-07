"""Coordinator between a TelemetryReader and the authoritative StateStore.

Why this exists as its own thing: a reader converts bytes to typed samples.
Writing to StateStore - the single source of truth the whole platform reads
from - is a different responsibility. Decoupling them means a bug in the
decoder cannot silently drive the car on screen, and a replay can be sanity-
checked (gaps logged, out-of-order samples refused) before the dashboard sees
anything.

The coordinator applies three gates to each sample before updating state:

* Monotonicity. A sample timestamped before the previous one is a reader bug
  or an export glitch and must not rewind the state clock.
* Freshness. A live reader with a 30-second-old sample should not pretend the
  car is where the sample says it is now. Replay bypasses this gate because
  "age" of a replayed historical sample is meaningless.
* Plausibility. A sample whose position jumps unrealistically far from the
  previous one is suspicious. We log it as a gap event but still apply it,
  because the alternative - freezing the state at the old position - is more
  dangerous than a visible outlier.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rwi.contracts import TelemetrySource, VehicleTelemetrySample
from rwi.core.errors import RwiError
from rwi.core.geo import geodesic_distance_m
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
    last_sample_time_utc: object | None = None
    gap_events: list[str] = field(default_factory=list)

    @property
    def dropped(self) -> int:
        return self.out_of_order_dropped + self.stale_dropped


class TelemetryCoordinator:
    """Pump samples from a reader into the StateStore under the guardrails.

    Live telemetry (``TelemetrySource.LIVE_RADIO`` or ``LIVE_CELLULAR``) and
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

    def __init__(self, store: StateStore) -> None:
        self._store = store
        self.stats = CoordinatorStats()

    async def consume(self, reader: TelemetryReader) -> CoordinatorStats:
        """Pump the reader to end-of-stream, updating state as we go.

        Returns the final :class:`CoordinatorStats`. Never raises on a dropped
        sample - decisions about what to do with a bad feed belong to a
        higher-level operator alert, not to this layer.
        """
        last_sample: VehicleTelemetrySample | None = None
        is_live = reader.source in {
            TelemetrySource.LIVE_RADIO,
            TelemetrySource.LIVE_CELLULAR,
            TelemetrySource.SIMULATED,
        }

        try:
            async for sample in reader.stream():
                self.stats.samples_received += 1
                if not self._monotonic_ok(sample, last_sample):
                    self.stats.out_of_order_dropped += 1
                    continue
                if is_live and not self._fresh_enough(sample):
                    self.stats.stale_dropped += 1
                    continue
                if last_sample is not None and sample.position is not None \
                        and last_sample.position is not None:
                    jump = geodesic_distance_m(
                        last_sample.position.point, sample.position.point
                    )
                    elapsed = (sample.time_utc - last_sample.time_utc).total_seconds()
                    implied_speed = jump / elapsed if elapsed > 0 else float("inf")
                    if implied_speed > self.position_jump_speed_threshold_mps:
                        self.stats.position_jumps_logged += 1
                        log.warning(
                            "position jump %.0f m in %.1f s (%.0f m/s) at %s "
                            "from previous sample at %s",
                            jump, elapsed, implied_speed,
                            sample.time_utc.isoformat(),
                            last_sample.time_utc.isoformat(),
                        )
                if sample.position is not None:
                    await self._store.update_fix(sample.position)
                self.stats.samples_applied += 1
                self.stats.last_sample_time_utc = sample.time_utc
                last_sample = sample

            for gap in await reader.gaps_so_far():
                self.stats.gap_events.append(
                    f"{gap.duration_s:.1f} s silence from "
                    f"{gap.start_utc.isoformat()} ({gap.reason or 'no reason given'})"
                )
        finally:
            await reader.close()
        return self.stats

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
