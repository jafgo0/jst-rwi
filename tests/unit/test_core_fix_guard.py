"""The shared live_gps verdict used by HTTP and telemetry inflows.

Keeping these verdicts in a tested dataclass is what lets us promise the two
code paths behave the same way. If a new inflow lands, it uses the same
assessor and gets the same guarantees by construction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rwi.core.errors import NaiveDatetimeError
from rwi.core.fix_guard import (
    FixVerdict,
    assess_live_fix,
    normalise_optional_utc,
)
from rwi.core.geo import GeoPoint
from rwi.core.position import FixQuality, PositionFix, PositionMode


def _fix(
    *,
    lat: float = -14.46,
    lon: float = 132.26,
    gps_time: datetime | None = None,
    received: datetime | None = None,
    mode: PositionMode = PositionMode.LIVE_GPS,
) -> PositionFix:
    if received is None:
        received = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    return PositionFix(
        point=GeoPoint(latitude_deg=lat, longitude_deg=lon),
        mode=mode,
        source="test",
        received_at_utc=received,
        gps_time_utc=gps_time,
        fix_quality=FixQuality.GPS,
    )


BASE = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def test_first_live_fix_with_no_previous_is_always_accepted() -> None:
    v = assess_live_fix(
        candidate=_fix(gps_time=BASE),
        previous=None,
        now=BASE,
        jump_speed_threshold_mps=110.0,
        future_grace_s=5.0,
    )
    assert v.verdict is FixVerdict.ACCEPT


def test_first_live_after_manual_baseline_is_accepted_because_no_like_for_like() -> None:
    manual = _fix(gps_time=BASE, mode=PositionMode.MANUAL)
    v = assess_live_fix(
        candidate=_fix(gps_time=BASE + timedelta(seconds=1), lat=-14.00),
        previous=manual,
        now=BASE + timedelta(seconds=1),
        jump_speed_threshold_mps=110.0,
        future_grace_s=5.0,
    )
    assert v.verdict is FixVerdict.ACCEPT


def test_a_plausible_step_is_accepted() -> None:
    previous = _fix(gps_time=BASE, lat=-14.46)
    candidate = _fix(gps_time=BASE + timedelta(seconds=60), lat=-14.4555)  # ~500 m
    v = assess_live_fix(
        candidate=candidate,
        previous=previous,
        now=BASE + timedelta(seconds=60),
        jump_speed_threshold_mps=110.0,
        future_grace_s=5.0,
    )
    assert v.verdict is FixVerdict.ACCEPT
    assert v.implied_speed_mps is not None and v.implied_speed_mps < 15.0


def test_an_implausible_jump_is_parked() -> None:
    previous = _fix(gps_time=BASE, lat=-14.46)
    candidate = _fix(gps_time=BASE + timedelta(seconds=1), lat=-13.46)  # ~111 km in 1 s
    v = assess_live_fix(
        candidate=candidate,
        previous=previous,
        now=BASE + timedelta(seconds=1),
        jump_speed_threshold_mps=110.0,
        future_grace_s=5.0,
    )
    assert v.verdict is FixVerdict.PARK_SUSPECT
    assert v.implied_speed_mps is not None and v.implied_speed_mps > 110.0


def test_a_duplicate_retry_is_rejected() -> None:
    previous = _fix(gps_time=BASE, lat=-14.46)
    candidate = _fix(gps_time=BASE, lat=-14.46)  # same place, same time
    v = assess_live_fix(
        candidate=candidate,
        previous=previous,
        now=BASE,
        jump_speed_threshold_mps=110.0,
        future_grace_s=5.0,
    )
    assert v.verdict is FixVerdict.REJECT
    assert v.reason is not None and "duplicate" in v.reason


def test_two_positions_sharing_a_timestamp_are_parked() -> None:
    previous = _fix(gps_time=BASE, lat=-14.46)
    candidate = _fix(gps_time=BASE, lat=-14.50)  # different place, same time
    v = assess_live_fix(
        candidate=candidate,
        previous=previous,
        now=BASE,
        jump_speed_threshold_mps=110.0,
        future_grace_s=5.0,
    )
    assert v.verdict is FixVerdict.PARK_SUSPECT
    assert v.implied_speed_mps is None  # cannot compute


def test_a_candidate_dated_before_the_previous_is_rejected_as_monotonicity() -> None:
    previous = _fix(gps_time=BASE + timedelta(seconds=10), lat=-14.46)
    candidate = _fix(gps_time=BASE, lat=-14.47)
    v = assess_live_fix(
        candidate=candidate,
        previous=previous,
        now=BASE + timedelta(seconds=10),
        jump_speed_threshold_mps=110.0,
        future_grace_s=5.0,
    )
    assert v.verdict is FixVerdict.REJECT
    assert v.reason is not None and "monotonicity" in v.reason


def test_a_candidate_far_in_the_future_is_rejected() -> None:
    v = assess_live_fix(
        candidate=_fix(gps_time=BASE + timedelta(minutes=10)),
        previous=_fix(gps_time=BASE),
        now=BASE,
        jump_speed_threshold_mps=110.0,
        future_grace_s=5.0,
    )
    assert v.verdict is FixVerdict.REJECT
    assert v.reason is not None and "future" in v.reason


def test_a_stale_candidate_is_rejected_when_the_caller_supplies_a_stale_limit() -> None:
    v = assess_live_fix(
        candidate=_fix(gps_time=BASE - timedelta(minutes=5)),
        previous=None,
        now=BASE,
        jump_speed_threshold_mps=110.0,
        future_grace_s=5.0,
        stale_limit_s=60.0,
    )
    assert v.verdict is FixVerdict.REJECT
    assert v.reason is not None and "old" in v.reason


def test_normalise_optional_utc_rejects_naive_and_passes_aware() -> None:
    assert normalise_optional_utc(None) is None
    aware = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    assert normalise_optional_utc(aware) == aware
    naive = datetime(2026, 11, 7, 12, 0)
    try:
        normalise_optional_utc(naive)
    except NaiveDatetimeError:
        return
    raise AssertionError("expected NaiveDatetimeError on naive input")
