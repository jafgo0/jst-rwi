"""Process-wide holder for the active position/time state.

Deliberately tiny and explicit: a single object, replaced atomically, read by
every subsystem. No module may keep its own copy of "current position".
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from rwi.core.errors import PositionUnavailableError, StaleDataError
from rwi.core.position import ActiveState, PositionFix, PositionMode


class StateStore:
    """Holds the authoritative :class:`ActiveState`."""

    def __init__(self, initial: ActiveState | None = None) -> None:
        self._state = initial or ActiveState(mode=PositionMode.MANUAL, fix=None)
        self._lock = asyncio.Lock()
        self._history: list[PositionFix] = []

    @property
    def state(self) -> ActiveState:
        return self._state

    async def replace(self, state: ActiveState) -> ActiveState:
        async with self._lock:
            self._state = state
            if state.fix is not None:
                self._history.append(state.fix)
                del self._history[:-5000]
            return self._state

    async def update_fix(
        self, fix: PositionFix, *, heading_deg: float | None = None
    ) -> ActiveState:
        async with self._lock:
            current = self._state
            self._state = ActiveState(
                mode=fix.mode,
                fix=fix,
                selected_time_utc=current.selected_time_utc,
                route_id=current.route_id,
                route_distance_m=current.route_distance_m,
                heading_deg=heading_deg if heading_deg is not None else fix.course_deg,
            )
            self._history.append(fix)
            del self._history[:-5000]
            return self._state

    def recent_fixes(self, limit: int = 200) -> list[PositionFix]:
        return self._history[-limit:]

    def require_fix(
        self, *, max_age_s: float | None = None, now: datetime | None = None
    ) -> PositionFix:
        """Return the active fix or fail loudly.

        Never invents a location. ``max_age_s`` additionally rejects a fix that
        exists but is too old to act on.
        """
        fix = self._state.fix
        if fix is None:
            raise PositionUnavailableError(
                f"no position available in mode {self._state.mode.value}; "
                "set a manual coordinate or connect a GPS source"
            )
        if max_age_s is not None and fix.is_stale(max_age_s, now):
            raise StaleDataError(
                f"position fix is {fix.age_s(now):.0f} s old (limit {max_age_s:.0f} s)"
            )
        return fix


_store = StateStore()


def get_state_store() -> StateStore:
    """FastAPI dependency / module accessor for the process-wide store."""
    return _store
