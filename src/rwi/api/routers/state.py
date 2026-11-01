"""Active position and time: the single authoritative state, exposed and settable."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from rwi.core.geo import GeoPoint
from rwi.core.position import ActiveState, FixQuality, PositionFix, PositionMode
from rwi.core.state import StateStore, get_state_store
from rwi.core.timebase import local_utc_offset_hours, timezone_name_for, to_local, utc_now

router = APIRouter(prefix="/api/state", tags=["state"])

StoreDep = Annotated[StateStore, Depends(get_state_store)]


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
    )


@router.get("", response_model=StateResponse)
async def read_state(store: StoreDep) -> StateResponse:
    return _describe(store)


@router.post("/position", response_model=StateResponse)
async def set_position(request: PositionRequest, store: StoreDep) -> StateResponse:
    default_quality = {
        PositionMode.LIVE_GPS: FixQuality.GPS,
        PositionMode.MANUAL: FixQuality.MANUAL,
        PositionMode.ROUTE_SIM: FixQuality.SIMULATED,
        PositionMode.HISTORICAL_REPLAY: FixQuality.SIMULATED,
    }[request.mode]
    fix = PositionFix(
        point=GeoPoint(
            latitude_deg=request.latitude_deg,
            longitude_deg=request.longitude_deg,
            elevation_m=request.elevation_m,
        ),
        mode=request.mode,
        source=request.source,
        received_at_utc=utc_now(),
        gps_time_utc=request.gps_time_utc,
        accuracy_m=request.accuracy_m,
        speed_mps=request.speed_mps,
        course_deg=request.heading_deg,
        fix_quality=request.fix_quality or default_quality,
    )
    await store.update_fix(fix, heading_deg=request.heading_deg)
    if request.route_id is not None:
        current = store.state
        await store.replace(current.model_copy(update={"route_id": request.route_id}))
    return _describe(store)


@router.post("/time", response_model=StateResponse)
async def set_time(request: TimeRequest, store: StoreDep) -> StateResponse:
    current = store.state
    updated: dict[str, Any] = {"selected_time_utc": request.selected_time_utc}
    await store.replace(current.model_copy(update=updated))
    return _describe(store)
