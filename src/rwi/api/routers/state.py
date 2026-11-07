"""Active position and time: the single authoritative state, exposed and settable."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from rwi.config import Settings, get_settings
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
    _reject_future_gps_time(request.gps_time_utc, now, settings)
    default_quality = {
        PositionMode.LIVE_GPS: FixQuality.GPS,
        PositionMode.MANUAL: FixQuality.MANUAL,
        PositionMode.ROUTE_SIM: FixQuality.SIMULATED,
        PositionMode.HISTORICAL_REPLAY: FixQuality.SIMULATED,
    }[request.mode]
    quality_override = _demote_on_implausible_jump(request, store.state.fix, now, settings)
    fix = PositionFix(
        point=GeoPoint(
            latitude_deg=request.latitude_deg,
            longitude_deg=request.longitude_deg,
            elevation_m=request.elevation_m,
        ),
        mode=request.mode,
        source=request.source,
        received_at_utc=now,
        gps_time_utc=request.gps_time_utc,
        accuracy_m=request.accuracy_m,
        speed_mps=request.speed_mps,
        course_deg=request.heading_deg,
        fix_quality=quality_override or request.fix_quality or default_quality,
    )
    # A fix the implausible-jump gate demoted to UNKNOWN does NOT become the
    # active position. It is parked as suspect_fix and the last trusted fix
    # remains authoritative for weather, route and strategy; the alerts
    # layer surfaces the discrepancy. The old behaviour (update_fix with a
    # suspect fix) let a GPS glitch silently teleport the car on screen.
    if quality_override is FixQuality.UNKNOWN and store.state.fix is not None:
        await store.patch(suspect_fix=fix)
        return _describe(store)
    # Compute route progress BEFORE acquiring the store lock, so the critical
    # section only does the write. Route projection is deterministic in fix.
    route_id = request.route_id or store.state.route_id
    updates: dict[str, Any] = {}
    if route_id is not None:
        updates["route_id"] = route_id
        progress = _project_progress(route_id, fix)
        if progress is not None:
            updates["route_distance_m"] = progress
    # A trusted fix clears any previously parked suspect fix: if the
    # operator or a fresh telemetry sample confirms the new location,
    # the earlier warning no longer applies.
    updates["suspect_fix"] = None
    # Single atomic write: fix + route updates together. The previous code
    # called update_fix() and then a separate replace() with a model_copy of
    # store.state read outside the lock; a telemetry fix landing between the
    # two would be clobbered.
    await store.update_fix(fix, heading_deg=request.heading_deg)
    await store.patch(**updates)
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
    expected = settings.telemetry_shared_secret
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "mode=live_gps is not accepted on this deployment because "
                "RWI_TELEMETRY_SHARED_SECRET is not set. Use mode=manual for "
                "operator entry, mode=route_sim for simulation, or configure "
                "the secret and resend with the X-Telemetry-Secret header."
            ),
        )
    if not secret or secret != expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="mode=live_gps requires a correct X-Telemetry-Secret header.",
        )


def _reject_future_gps_time(
    gps_time_utc: datetime | None, now: datetime, settings: Settings
) -> None:
    if gps_time_utc is None:
        return
    # Pydantic has already parsed the datetime; it may still be naive on some
    # clients. Treat it as UTC and compare.
    ahead_s = (gps_time_utc - now).total_seconds()
    if ahead_s > settings.telemetry_future_grace_s:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"gps_time_utc is {ahead_s:.0f} s in the future "
                f"(grace is {settings.telemetry_future_grace_s:.0f} s). "
                "This usually means the sending device has a clock skew."
            ),
        )


def _demote_on_implausible_jump(
    request: PositionRequest,
    previous: PositionFix | None,
    now: datetime,
    settings: Settings,
) -> FixQuality | None:
    """If a live_gps fix implies an impossible speed, mark it suspect.

    Not dropping it: silently freezing the dashboard at the previous fix is
    more dangerous than a flagged one. The demotion to UNKNOWN surfaces the
    problem to any consumer that reads fix_quality.
    """
    if request.mode is not PositionMode.LIVE_GPS or previous is None:
        return None
    if previous.mode is not PositionMode.LIVE_GPS:
        return None
    previous_time = previous.gps_time_utc or previous.received_at_utc
    reference_time = request.gps_time_utc or now
    elapsed_s = (reference_time - previous_time).total_seconds()
    if elapsed_s <= 0:
        return None
    jump_m = geodesic_distance_m(
        previous.point,
        GeoPoint(
            latitude_deg=request.latitude_deg,
            longitude_deg=request.longitude_deg,
            elevation_m=request.elevation_m,
        ),
    )
    implied_speed = jump_m / elapsed_s
    if implied_speed > settings.http_live_jump_speed_mps:
        log.warning(
            "implausible jump on POST /state/position: %.0f m in %.1f s "
            "(%.0f m/s, previous at %s); marking fix_quality=UNKNOWN",
            jump_m, elapsed_s, implied_speed, previous_time.isoformat(),
        )
        return FixQuality.UNKNOWN
    return None


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
