"""Edge spool for on-board sensor windows (ADR 0010 §5).

One SQLite file on the machine next to the sensors. Every closed 1 s
:class:`~rwi.telemetry.onboard.SensorWindow` is written here before any sink
sees it, so a Starlink outage or a process restart loses nothing.

* ``windows(stream, start_s, payload)``: idempotent on (stream, window start).
  Writing the same window twice keeps the first copy.
* ``forwarded(stream, start_s, sink, outcome, at_utc)``: one row per sink that
  has dealt with a window. ``outcome`` says how (``archived``, ``live``,
  ``stale_for_live``, ``no_position`` ...). A window with no row for a sink is
  pending for that sink; a sink that raises leaves it pending, and the next
  drain retries it oldest-first.

Windows are never deleted or edited here. Nothing is interpolated.
"""

import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from rwi.core.timebase import ensure_utc, utc_now
from rwi.telemetry.onboard import SensorWindow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS windows (
    stream   TEXT    NOT NULL,
    start_s  INTEGER NOT NULL,
    payload  TEXT    NOT NULL,
    PRIMARY KEY (stream, start_s)
);
CREATE TABLE IF NOT EXISTS forwarded (
    stream   TEXT    NOT NULL,
    start_s  INTEGER NOT NULL,
    sink     TEXT    NOT NULL,
    outcome  TEXT    NOT NULL,
    at_utc   TEXT    NOT NULL,
    PRIMARY KEY (stream, start_s, sink)
);
"""


def window_key(window: SensorWindow) -> tuple[str, int]:
    """(stream, start as integer UTC epoch seconds) - the spool's identity of a window."""
    return window.stream, int(ensure_utc(window.start_utc).timestamp())


class SensorSpool:
    """SQLite spool of sensor windows with per-sink forwarded flags."""

    def __init__(self, path: Path | str, *, create_dirs: bool = True) -> None:
        self.path = Path(path)
        if create_dirs and str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._txn() as conn:
            if str(self.path) != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def put(self, windows: Iterable[SensorWindow]) -> int:
        """Write windows; return how many were new (a repeat of a stored window is ignored)."""
        rows = [(*window_key(w), w.model_dump_json()) for w in windows]
        if not rows:
            return 0
        with self._txn() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO windows (stream, start_s, payload) VALUES (?, ?, ?)", rows
            )
            return conn.total_changes - before

    def get(self, stream: str, start_utc: datetime) -> SensorWindow | None:
        """The stored window of ``stream`` starting at ``start_utc``, if any."""
        start_s = int(ensure_utc(start_utc).timestamp())
        with self._txn() as conn:
            row = conn.execute(
                "SELECT payload FROM windows WHERE stream = ? AND start_s = ?", (stream, start_s)
            ).fetchone()
        return SensorWindow.model_validate_json(row[0]) if row else None

    def windows(self, stream: str, start_utc: datetime, end_utc: datetime) -> list[SensorWindow]:
        """Stored windows of ``stream`` with start in [start_utc, end_utc), oldest first."""
        t0 = int(ensure_utc(start_utc).timestamp())
        t1 = int(ensure_utc(end_utc).timestamp())
        with self._txn() as conn:
            rows = conn.execute(
                "SELECT payload FROM windows WHERE stream = ? AND start_s >= ? AND start_s < ? "
                "ORDER BY start_s",
                (stream, t0, t1),
            ).fetchall()
        return [SensorWindow.model_validate_json(r[0]) for r in rows]

    def pending(
        self, sink: str, *, stream: str | None = None, limit: int | None = None
    ) -> list[SensorWindow]:
        """Windows ``sink`` has not dealt with yet, oldest first (by start, then stream)."""
        sql = (
            "SELECT w.payload FROM windows w WHERE NOT EXISTS ("
            "SELECT 1 FROM forwarded f WHERE f.stream = w.stream AND f.start_s = w.start_s "
            "AND f.sink = ?)"
        )
        params: list[object] = [sink]
        if stream is not None:
            sql += " AND w.stream = ?"
            params.append(stream)
        sql += " ORDER BY w.start_s, w.stream"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._txn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [SensorWindow.model_validate_json(r[0]) for r in rows]

    def mark_forwarded(
        self,
        sink: str,
        windows: Iterable[SensorWindow],
        *,
        outcome: str,
        at_utc: datetime | None = None,
    ) -> int:
        """Record that ``sink`` dealt with ``windows`` and how. Returns rows newly marked."""
        at = ensure_utc(at_utc or utc_now()).isoformat()
        rows = [(*window_key(w), sink, outcome, at) for w in windows]
        if not rows:
            return 0
        with self._txn() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO forwarded (stream, start_s, sink, outcome, at_utc) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            return conn.total_changes - before

    def forwarded_between(
        self, sink: str, stream: str, start_utc: datetime, end_utc: datetime
    ) -> int:
        """How many windows of ``stream`` starting in [start, end) ``sink`` already dealt with."""
        t0 = int(ensure_utc(start_utc).timestamp())
        t1 = int(ensure_utc(end_utc).timestamp())
        with self._txn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM forwarded WHERE sink = ? AND stream = ? "
                "AND start_s >= ? AND start_s < ?",
                (sink, stream, t0, t1),
            ).fetchone()
        return int(row[0])

    def outcomes(self, sink: str) -> dict[str, int]:
        """How many windows ``sink`` marked with each outcome."""
        with self._txn() as conn:
            rows = conn.execute(
                "SELECT outcome, COUNT(*) FROM forwarded WHERE sink = ? GROUP BY outcome", (sink,)
            ).fetchall()
        return {str(o): int(n) for o, n in rows}

    def count(self, stream: str | None = None) -> int:
        """Number of stored windows, optionally for one stream."""
        with self._txn() as conn:
            if stream is None:
                row = conn.execute("SELECT COUNT(*) FROM windows").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM windows WHERE stream = ?", (stream,)
                ).fetchone()
        return int(row[0])

    def newest_end(self, stream: str) -> datetime | None:
        """End of the newest stored window of ``stream`` (1 s windows), or None."""
        with self._txn() as conn:
            row = conn.execute(
                "SELECT MAX(start_s) FROM windows WHERE stream = ?", (stream,)
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return datetime.fromtimestamp(int(row[0]) + 1, tz=UTC)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = ["SensorSpool", "window_key"]
