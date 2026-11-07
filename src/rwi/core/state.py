"""Process-wide holder for the active position/time state.

Deliberately tiny and explicit: a single object, replaced atomically, read by
every subsystem. No module may keep its own copy of "current position".

Scope note: this is an *in-process* store. A single ``uvicorn`` worker is the
only supported deployment because every writer and every reader must see the
same object. Running with ``--workers N > 1`` silently splits the state N ways
and must not be done. See ``docs/OPERATIONS.md``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from rwi.core.errors import PositionUnavailableError, StaleDataError
from rwi.core.position import ActiveState, PositionFix, PositionMode


class StateStore:
    """Holds the authoritative :class:`ActiveState`.

    All writes go through ``replace``/``update_fix``/``patch`` under the same
    lock, so reads taken between write steps never leak back into a later
    write: anyone wanting a compound update (new fix AND new route) must use
    ``patch`` with a callable rather than ``state`` + ``replace``.
    """

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

    async def apply_trusted_fix(
        self,
        fix: PositionFix,
        *,
        heading_deg: float | None = None,
        route_id: str | None = None,
        route_distance_m: float | None = None,
    ) -> ActiveState:
        """Promote ``fix`` to the trusted active state in one lock acquisition.

        A trusted fix always clears any previously parked ``suspect_fix``:
        a confirmed later location means the earlier suspect report no
        longer applies. ``route_id`` and ``route_distance_m`` are set
        together with the fix so downstream readers never see a stale
        progress value against a new position. Caller computes progress
        before calling; the store does not import the route layer.
        """
        async with self._lock:
            current = self._state
            resolved_route_id = route_id if route_id is not None else current.route_id
            # route_distance_m MUST be recomputed for each new fix: the
            # previous value belonged to the previous position and silently
            # keeping it would misplace the vehicle along the route. If the
            # caller does not supply a fresh value we show None (unknown
            # progress) rather than reuse a stale one.
            resolved_distance = route_distance_m
            self._state = ActiveState(
                mode=fix.mode,
                fix=fix,
                suspect_fix=None,
                selected_time_utc=current.selected_time_utc,
                route_id=resolved_route_id,
                route_distance_m=resolved_distance,
                heading_deg=heading_deg if heading_deg is not None else fix.course_deg,
            )
            self._history.append(fix)
            del self._history[:-5000]
            return self._state

    async def park_suspect_fix(self, fix: PositionFix) -> ActiveState:
        """Park ``fix`` as suspect without disturbing the trusted active fix.

        When there is no trusted fix to protect (the state's ``fix`` is
        None) parking is silently a no-op: with nothing trusted to compare
        against, a suspect is meaningless and callers should either accept
        the fix or reject it loudly. This method exists so both the HTTP
        endpoint and the telemetry coordinator have one place to call.
        """
        async with self._lock:
            if self._state.fix is None:
                return self._state
            self._state = self._state.model_copy(update={"suspect_fix": fix})
            return self._state

    async def patch(self, **updates: Any) -> ActiveState:
        """Atomically apply ``updates`` on top of the current state.

        Using ``state.model_copy(update=updates)`` outside the lock lets a
        concurrent writer land between read and write, and the compound write
        clobbers that concurrent update. This method takes the lock across
        read and write so no snapshot ever leaks back as a clobber.
        """
        async with self._lock:
            self._state = self._state.model_copy(update=updates)
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
