"""Single verdict for 'is this live_gps fix trustworthy enough to promote?'.

Both inflows -- the HTTP ``POST /api/state/position`` endpoint and
:class:`rwi.telemetry.coordinator.TelemetryCoordinator` -- route every
live_gps candidate through :func:`assess_live_fix` so an implausible jump,
an out-of-order sample or a stale clock produces the same verdict regardless
of transport. The ``previous`` argument is the last trusted (not suspect)
live_gps fix; the previous mode is checked so a manual-to-live switch does
not trip the jump gate.

Outcomes:

* ``ACCEPT`` - promote to the active trusted fix.
* ``PARK_SUSPECT`` - do NOT promote; park as ``suspect_fix``. Downstream
  weather / route / strategy keep reading the last trusted fix, and the
  alert layer surfaces the discrepancy.
* ``REJECT`` - do not promote, do not park, do not count against suspect.
  Used for duplicate retries and clear monotonicity violations where the
  telemetry rules already say "drop silently".

The module owns neither storage nor logging: callers decide what to do with
the verdict. That is what lets the same decision drive two very different
code paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from rwi.core.geo import geodesic_distance_m
from rwi.core.position import PositionFix, PositionMode
from rwi.core.timebase import ensure_utc


class FixVerdict(StrEnum):
    ACCEPT = "accept"
    PARK_SUSPECT = "park_suspect"
    REJECT = "reject"


@dataclass(frozen=True)
class FixAssessment:
    """Verdict plus the numbers that justified it.

    ``reason`` is operator-facing short text. ``jump_m``, ``elapsed_s`` and
    ``implied_speed_mps`` are None when the candidate is the first fix seen
    or when a non-jump reason triggered the verdict.
    """

    verdict: FixVerdict
    reason: str | None = None
    jump_m: float | None = None
    elapsed_s: float | None = None
    implied_speed_mps: float | None = None


def _candidate_time(candidate: PositionFix) -> datetime:
    """Prefer the GPS-stamp instant; fall back to receipt time.

    PositionFix._normalise has already made both tz-aware UTC.
    """
    return candidate.gps_time_utc or candidate.received_at_utc


def _previous_time(previous: PositionFix) -> datetime:
    return previous.gps_time_utc or previous.received_at_utc


def assess_live_fix(
    *,
    candidate: PositionFix,
    previous: PositionFix | None,
    now: datetime,
    jump_speed_threshold_mps: float,
    future_grace_s: float,
    stale_limit_s: float | None = None,
) -> FixAssessment:
    """Decide what the trusted-state layer should do with ``candidate``.

    The rules here mirror what both inflows previously tried to express
    separately. They are intentionally conservative: when we are unsure we
    park rather than promote, because silently promoting a glitch is worse
    than showing one.

    * A candidate stamped far in the future is REJECTED: we cannot compare
      it to ``previous`` and accepting it would freeze the UI "ahead of time"
      the next time a real fix arrives.
    * A candidate whose own clock is older than the stale limit is REJECTED
      when ``stale_limit_s`` is supplied. Callers that do not care about
      staleness (operator HTTP entry) pass ``None``.
    * With no previous trusted fix, the candidate is ACCEPTED.
    * A candidate whose GPS instant equals the previous one but whose
      position is the same is REJECTED as a duplicate retry.
    * A candidate whose GPS instant equals the previous one with a
      different position is PARKED: we cannot compute an implied speed, but
      two different positions "at the same instant" is itself a bug.
    * A candidate whose GPS instant is earlier than the previous one is
      REJECTED (monotonicity).
    * Otherwise compute implied speed. Above the threshold -> PARK, else
      ACCEPT.

    The previous mode must itself be live_gps for the jump gate to fire;
    the first live_gps after a manual or simulated session is always
    ACCEPT (we have no like-for-like baseline).
    """

    candidate_time = _candidate_time(candidate)
    ahead_s = (candidate_time - now).total_seconds()
    if ahead_s > future_grace_s:
        return FixAssessment(
            verdict=FixVerdict.REJECT,
            reason=(
                f"candidate time is {ahead_s:.0f} s in the future "
                f"(grace {future_grace_s:.0f} s): sender clock skew"
            ),
        )

    if stale_limit_s is not None and -ahead_s > stale_limit_s:
        return FixAssessment(
            verdict=FixVerdict.REJECT,
            reason=(
                f"candidate is {-ahead_s:.0f} s old (limit {stale_limit_s:.0f} s)"
            ),
        )

    if previous is None or previous.mode is not PositionMode.LIVE_GPS:
        return FixAssessment(verdict=FixVerdict.ACCEPT)

    previous_time = _previous_time(previous)
    elapsed_s = (candidate_time - previous_time).total_seconds()
    jump_m = geodesic_distance_m(previous.point, candidate.point)

    if elapsed_s < 0:
        return FixAssessment(
            verdict=FixVerdict.REJECT,
            reason=(
                f"candidate time precedes previous by {-elapsed_s:.1f} s "
                "(monotonicity violation)"
            ),
            jump_m=jump_m,
            elapsed_s=elapsed_s,
        )

    if elapsed_s == 0:
        # geodesic_distance_m uses floating-point arithmetic; "same point"
        # can come out as a nanometre rather than exact zero. One metre is
        # well under any GPS accuracy, so treat it as the same place.
        if jump_m < 1.0:
            return FixAssessment(
                verdict=FixVerdict.REJECT,
                reason="duplicate of the previous fix",
                jump_m=jump_m,
                elapsed_s=0.0,
            )
        return FixAssessment(
            verdict=FixVerdict.PARK_SUSPECT,
            reason=(
                f"two different positions share the same timestamp "
                f"({jump_m:.0f} m apart)"
            ),
            jump_m=jump_m,
            elapsed_s=0.0,
            implied_speed_mps=None,
        )

    implied_speed = jump_m / elapsed_s
    if implied_speed > jump_speed_threshold_mps:
        return FixAssessment(
            verdict=FixVerdict.PARK_SUSPECT,
            reason=(
                f"jump {jump_m / 1000:.1f} km in {elapsed_s:.1f} s implies "
                f"{implied_speed:.0f} m/s (threshold {jump_speed_threshold_mps:.0f} m/s)"
            ),
            jump_m=jump_m,
            elapsed_s=elapsed_s,
            implied_speed_mps=implied_speed,
        )

    return FixAssessment(
        verdict=FixVerdict.ACCEPT,
        jump_m=jump_m,
        elapsed_s=elapsed_s,
        implied_speed_mps=implied_speed,
    )


def normalise_optional_utc(value: datetime | None) -> datetime | None:
    """Convert a user-supplied timestamp to aware UTC, or return None.

    Pydantic accepts tz-naive datetimes on some clients; this converts them
    using :func:`rwi.core.timebase.ensure_utc` so callers never do
    ``naive - aware`` arithmetic and crash with a TypeError. A truly naive
    datetime raises :class:`rwi.core.errors.NaiveDatetimeError`, which the
    HTTP layer turns into 400.
    """
    if value is None:
        return None
    return ensure_utc(value)


__all__ = [
    "FixAssessment",
    "FixVerdict",
    "assess_live_fix",
    "normalise_optional_utc",
]
