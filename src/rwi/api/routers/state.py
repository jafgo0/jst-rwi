"""Active position and time: the single authoritative state, exposed and settable."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from rwi.api.security import require_shared_secret
from rwi.config import Settings, get_settings
from rwi.core.errors import NaiveDatetimeError
from rwi.core.fix_guard import FixVerdict, assess_live_fix, normalise_optional_utc
from rwi.core.geo import GeoPoint, geodesic_distance_m
from rwi.core.position import ActiveState, FixQuality, PositionFix, PositionMode
from rwi.core.state import StateStore, get_state_store
from rwi.core.timebase import local_utc_offset_hours, timezone_name_for, to_local, utc_now

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/state", tags=["state"])

StoreDep = Annotated[StateStore, Depends(get_state_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


class PositionRequest(BaseModel):
    """A new position. The mode is mandatory: the platform never guesses whether
    a coordinate is live, manual or simulated."""

    mode: PositionMode
    latitude_deg: float = Field(ge=-90.0, le=90.0)
    longitude_deg: float = Field(ge=-180.0, le=180.0)
    elevation_m: float | None = None
    heading_deg: float | None = Field(default=None, ge=0.0, lt=360.0)
    speed_mps: float | None = Field(default=None, ge=0.0)
    accuracy_m: float | None = Field(default=None, ge=0.0)
    gps_time_utc: datetime | None = None
    source: str = "operator"
    fix_quality: FixQuality | None = None
    route_id: str | None = None


class TimeRequest(BaseModel):
    """`null` returns the platform to following real time."""

    selected_time_utc: datetime | None = None


class SuspectFixReport(BaseModel):
    """A parked live_gps fix that the implausible-jump gate refused to promote.

    Surfacing it lets the operator see the discrepancy between the last
    trusted position and what telemetry claimed next, decide whether the
    trusted position is still authoritative, and resolve the mismatch
    explicitly (reset by posting a new fix) rather than through silence.
    """

    latitude_deg: float
    longitude_deg: float
    received_at_utc: datetime
    implied_speed_mps: float | None
    jump_m: float | None


class StateResponse(BaseModel):
    state: ActiveState
    effective_time_utc: datetime
    local_time: str | None
    timezone_name: str | None
    utc_offset_hours: float | None
    position_available: bool
    is_simulated: bool
    position_age_s: float | None
    server_time_utc: datetime
    suspect_fix: SuspectFixReport | None = None


def _describe(store: StateStore) -> StateResponse:
    state = store.state
    now = utc_now()
    effective = state.effective_time(now)
    fix = state.fix
    local_time: str | None = None
    tz_name: str | None = None
    offset: float | None = None
    if fix is not None:
        tz_name = timezone_name_for(fix.point.latitude_deg, fix.point.longitude_deg)
        local_time = to_local(effective, fix.point).isoformat()
        offset = local_utc_offset_hours(effective, fix.point)
    suspect_report: SuspectFixReport | None = None
    if state.suspect_fix is not None and fix is not None:
        jump = geodesic_distance_m(fix.point, state.suspect_fix.point)
        suspect_time = state.suspect_fix.gps_time_utc or state.suspect_fix.received_at_utc
        trusted_time = fix.gps_time_utc or fix.received_at_utc
        elapsed = (suspect_time - trusted_time).total_seconds()
        implied = jump / elapsed if elapsed > 0 else None
        suspect_report = SuspectFixReport(
            latitude_deg=state.suspect_fix.point.latitude_deg,
            longitude_deg=state.suspect_fix.point.longitude_deg,
            received_at_utc=state.suspect_fix.received_at_utc,
            implied_speed_mps=implied,
            jump_m=jump,
        )
    return StateResponse(
        state=state,
        effective_time_utc=effective,
        local_time=local_time,
        timezone_name=tz_name,
        utc_offset_hours=offset,
        position_available=fix is not None,
        is_simulated=bool(fix and fix.is_simulated),
        position_age_s=fix.age_s(now) if fix else None,
        server_time_utc=now,
        suspect_fix=suspect_report,
    )


@router.get("", response_model=StateResponse)
async def read_state(store: StoreDep) -> StateResponse:
    return _describe(store)


@router.post("/position", response_model=StateResponse)
async def set_position(
    request: PositionRequest,
    store: StoreDep,
    settings: SettingsDep,
    x_telemetry_secret: Annotated[str | None, Header(alias="X-Telemetry-Secret")] = None,
) -> StateResponse:
    _authorise_mode(request.mode, settings, x_telemetry_secret)
    now = utc_now()
    try:
        gps_time_utc = normalise_optional_utc(request.gps_time_utc)
    except NaiveDatetimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "gps_time_utc must include a UTC offset (e.g. "
                "\"2026-11-07T15:30:00Z\"). A bare local timestamp cannot "
                "be compared to the server clock."
            ),
        ) from exc
    default_quality = {
        PositionMode.LIVE_GPS: FixQuality.GPS,
        PositionMode.MANUAL: FixQuality.MANUAL,
        PositionMode.ROUTE_SIM: FixQuality.SIMULATED,
        PositionMode.HISTORICAL_REPLAY: FixQuality.SIMULATED,
    }[request.mode]
    candidate = PositionFix(
        point=GeoPoint(
            latitude_deg=request.latitude_deg,
            longitude_deg=request.longitude_deg,
            elevation_m=request.elevation_m,
        ),
        mode=request.mode,
        source=request.source,
        received_at_utc=now,
        gps_time_utc=gps_time_utc,
        accuracy_m=request.accuracy_m,
        speed_mps=request.speed_mps,
        course_deg=request.heading_deg,
        fix_quality=request.fix_quality or default_quality,
    )
    # Only live_gps goes through the jump/monotonicity gate. Operator-
    # entered manual fixes are explicitly authoritative and must not be
    # second-guessed against a previous live sample; simulated modes have
    # their own cadence.
    if request.mode is PositionMode.LIVE_GPS:
        assessment = assess_live_fix(
            candidate=candidate,
            previous=store.state.fix,
            now=now,
            jump_speed_threshold_mps=settings.http_live_jump_speed_mps,
            future_grace_s=settings.telemetry_future_grace_s,
            stale_limit_s=None,
        )
        if assessment.verdict is FixVerdict.REJECT:
            log.info(
                "POST /state/position rejected live_gps candidate: %s", assessment.reason
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=assessment.reason or "live_gps candidate rejected",
            )
        if assessment.verdict is FixVerdict.PARK_SUSPECT:
            log.warning("parking suspect live_gps fix: %s", assessment.reason)
            # Mark the parked fix as UNKNOWN quality so any subsystem that
            # accidentally reads it cannot mistake it for a trusted GPS fix.
            suspect = candidate.model_copy(update={"fix_quality": FixQuality.UNKNOWN})
            await store.park_suspect_fix(suspect)
            return _describe(store)
    # ACCEPT (or non-live mode): compute route progress BEFORE the lock, so
    # the critical section only does the write. Project the candidate onto
    # whichever route is active (request.route_id overrides the state).
    route_id = request.route_id or store.state.route_id
    route_distance_m = _project_progress(route_id, candidate) if route_id else None
    await store.apply_trusted_fix(
        candidate,
        heading_deg=request.heading_deg,
        route_id=route_id,
        route_distance_m=route_distance_m,
    )
    return _describe(store)


def _authorise_mode(mode: PositionMode, settings: Settings, secret: str | None) -> None:
    """Only a caller with the shared secret may claim mode=live_gps over HTTP.

    live_gps is the one mode downstream consumers take as "this is a real car
    on a real road". Letting unauthenticated clients set it would let any
    browser or probe lie about being the car. Manual and sim modes are fine
    without a secret: they are explicitly non-authoritative.
    """
    if mode is not PositionMode.LIVE_GPS:
        return
    require_shared_secret(settings, secret, action="mode=live_gps")


def _project_progress(route_id: str, fix: PositionFix) -> float | None:
    """Distance along the active route for a new fix, or None if unobtainable.

    Keeping progress in the authoritative state means every consumer agrees on
    where the vehicle is along the route. Failure is silent on purpose: a
    missing route or an absent route module must not stop a position update,
    and the caller can still read progress from /api/route/{id}/progress.
    """
    try:
        from rwi.api.capability import require_attr
        from rwi.api.deps import get_route_repository

        project = require_attr(
            "rwi.route.geometry", "project_onto_route", capability="route projection"
        )
        route = get_route_repository().all().get(route_id)
        if route is None:
            return None
        return float(project(route, fix.point).distance_along_m)
    except Exception:
        return None


@router.post("/time", response_model=StateResponse)
async def set_time(request: TimeRequest, store: StoreDep) -> StateResponse:
    # patch() takes the store lock across read+write, so a concurrent
    # position update cannot be clobbered by a stale snapshot.
    await store.patch(selected_time_utc=request.selected_time_utc)
    return _describe(store)
