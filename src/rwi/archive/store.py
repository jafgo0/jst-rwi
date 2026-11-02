"""SQLite implementation of the :class:`rwi.contracts.ForecastArchive` protocol.

See ``docs/adr/0007-sqlite-forecast-archive.md`` and ``docs/science/archive.md``.

Guarantees
----------
* **Immutable.** A recorded value is never updated (the database refuses ``UPDATE``
  through triggers). A later model run for the same valid time is a new ``series``
  and therefore new rows. Replacing an old forecast with a better-informed one
  would silently inflate measured skill - data leakage.
* **Idempotent.** Re-recording the same retrieval writes nothing; see
  :meth:`SqliteArchive.record_series` for the exact identity.
* **No leakage into pairs.** A forecast value is only paired when its
  ``retrieved_at_utc`` is strictly before its ``valid_time_utc``; the rule is
  applied in SQL and the number rejected is reported in :class:`PairsReport`.
* **Lazy and non-fatal.** Nothing touches the disk until first use, and every
  failure surfaces as an :class:`~rwi.archive.errors.ArchiveError`; callers wrap
  archive use in ``except ArchiveError`` so a broken archive never breaks a
  weather request.

All instants are stored as INTEGER epoch seconds (UTC, floored) and converted to
aware UTC ``datetime`` at this module's boundary - see :mod:`rwi.archive.schema`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import sqlite3
import threading
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

from rwi.archive.errors import (
    ArchiveConflictError,
    ArchiveError,
    ArchiveReadOnlyError,
    ArchiveUnavailableError,
)
from rwi.archive.schema import LEAD_BUCKETS_S, MIGRATIONS, SCHEMA_VERSION, USABLE_QUALITY
from rwi.contracts import ForecastObservationPair, WeatherSeries
from rwi.core.geo import GeoPoint, geodesic_distance_m
from rwi.core.provenance import DataKind, QualityFlag
from rwi.core.timebase import TimeWindow, ensure_utc

logger = logging.getLogger("rwi.archive")

DEFAULT_MAX_SEPARATION_M: Final[float] = 50_000.0
DEFAULT_MAX_TIME_OFFSET_S: Final[float] = 1800.0

_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_SECOND: Final[timedelta] = timedelta(seconds=1)
_T_MIN: Final[int] = -(2**60)
_T_MAX: Final[int] = 2**60
_DAY_S: Final[int] = 86_400
_QUALITY_SQL: Final[str] = ", ".join(f"'{q}'" for q in USABLE_QUALITY)
_BUSY_TIMEOUT_S: Final[float] = 5.0


# ---------------------------------------------------------------------------
# Boundary conversions
# ---------------------------------------------------------------------------


def to_epoch_s(when: datetime) -> int:
    """Aware datetime -> INTEGER epoch seconds (UTC), floored. Naive input is rejected."""
    return (ensure_utc(when) - _EPOCH) // _ONE_SECOND


def from_epoch_s(seconds: int) -> datetime:
    """INTEGER epoch seconds -> aware UTC datetime."""
    return _EPOCH + timedelta(seconds=int(seconds))


def _iso(seconds: int | None) -> str | None:
    return None if seconds is None else from_epoch_s(seconds).isoformat()


# ---------------------------------------------------------------------------
# Reporting types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairsReport:
    """What one pair query considered, dropped and emitted (counts of forecast values).

    ``candidates`` is every forecast value in scope (variable, period, provider,
    model, usable quality). It splits into the three ``dropped_*`` buckets, the
    ``eligible`` values, and - within ``eligible`` - ``unmatched`` (no observation
    inside the tolerances) and ``emitted``.
    """

    variable: str
    candidates: int = 0
    dropped_recorded_after_valid: int = 0
    """Forecast "recorded" at or after its valid time: not a forecast; never scored."""
    dropped_no_init_time: int = 0
    """No model initialisation time, so lead time is undefined."""
    dropped_negative_lead: int = 0
    """Model initialisation time after the valid time: inconsistent provenance."""
    eligible: int = 0
    unmatched: int = 0
    emitted: int = 0


@dataclass(frozen=True)
class RecordReport:
    """Outcome of one :meth:`SqliteArchive.record_series` call, in cells."""

    written: int = 0
    already_present: int = 0
    """Identical cell already archived: nothing written (idempotency)."""
    conflicting: int = 0
    """Cell already archived with a different value: the archived one was kept."""


@dataclass(frozen=True)
class _Match:
    valid_s: int
    forecast_value: float
    observed_value: float
    lead_s: int
    provider: str
    model_name: str | None
    retrieved_s: int
    forecast_series_id: int
    observation_series_id: int
    forecast_xy: tuple[float, float, float | None]
    observation_xy: tuple[float, float, float | None]
    separation_m: float
    offset_s: int


# ---------------------------------------------------------------------------
# The archive
# ---------------------------------------------------------------------------


class SqliteArchive:
    """Durable forecast/observation archive in one SQLite file (WAL mode).

    The constructor does no I/O. The file is opened (and created and migrated,
    unless ``read_only``) on first use. A directory that does not exist is an
    :class:`ArchiveUnavailableError` unless ``create_dirs=True`` (which
    :func:`open_archive` sets); the archive never invents a location silently.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        read_only: bool = False,
        create_dirs: bool = False,
    ) -> None:
        self._path = Path(path)
        self._read_only = read_only
        self._create_dirs = create_dirs
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self.last_pairs_report: PairsReport | None = None
        """Report of the most recent :meth:`pairs` call on this object."""
        self.last_record_report: RecordReport = RecordReport()
        """Report of the most recent :meth:`record_series` call on this object."""

    # -- lifecycle ---------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def read_only(self) -> bool:
        return self._read_only

    def __enter__(self) -> SqliteArchive:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the connection. Idempotent; a later call re-opens lazily."""
        with self._lock:
            conn, self._conn = self._conn, None
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error as exc:  # pragma: no cover - close rarely fails
                    logger.warning("closing archive %s failed: %s", self._path, exc)

    def _unavailable(
        self, reason: str, exc: BaseException | None = None
    ) -> ArchiveUnavailableError:
        err = ArchiveUnavailableError(f"forecast archive {self._path} unavailable: {reason}")
        if exc is not None:
            err.__cause__ = exc
        return err

    def _connection(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                self._conn = self._open()
            return self._conn

    def _open(self) -> sqlite3.Connection:
        path = self._path
        if self._read_only:
            if not path.is_file():
                raise self._unavailable("file does not exist (opened read-only)")
        else:
            parent = path.parent
            if not parent.is_dir():
                if not self._create_dirs:
                    raise self._unavailable(f"directory {parent} does not exist")
                try:
                    parent.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise self._unavailable(
                        f"cannot create directory {parent}: {exc}", exc
                    ) from exc
            if path.is_dir():
                raise self._unavailable("path is a directory, not a file")
        conn: sqlite3.Connection | None = None
        try:
            if self._read_only:
                target = f"file:{quote(str(path.resolve()))}?mode=ro"
                conn = sqlite3.connect(
                    target, uri=True, timeout=_BUSY_TIMEOUT_S, isolation_level=None,
                    check_same_thread=False,
                )
            else:
                conn = sqlite3.connect(
                    str(path), timeout=_BUSY_TIMEOUT_S, isolation_level=None,
                    check_same_thread=False,
                )
            conn.execute(f"PRAGMA busy_timeout = {int(_BUSY_TIMEOUT_S * 1000)}")
            if not self._read_only:
                mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
                if mode != "wal":
                    logger.warning("archive %s: WAL unavailable, journal_mode=%s", path, mode)
                conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            self._migrate(conn)
        except ArchiveError:
            if conn is not None:
                conn.close()
            raise
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            raise self._unavailable(f"{type(exc).__name__}: {exc}", exc) from exc
        return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Bring the schema to :data:`SCHEMA_VERSION`; forward-only."""
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise self._unavailable(
                f"schema version {version} is newer than this software understands "
                f"({SCHEMA_VERSION}); upgrade the software, do not downgrade the archive"
            )
        if version == SCHEMA_VERSION:
            return
        if self._read_only:
            raise self._unavailable(
                f"schema version {version} needs migration to {SCHEMA_VERSION}; "
                "open it read-write once"
            )
        for target in range(version + 1, SCHEMA_VERSION + 1):
            logger.info("archive %s: migrating schema %d -> %d", self._path, target - 1, target)
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in MIGRATIONS[target]:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {target}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    @contextmanager
    def _txn(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        """One transaction on the shared connection; sqlite errors become ArchiveErrors."""
        if write and self._read_only:
            raise ArchiveReadOnlyError(f"forecast archive {self._path} was opened read-only")
        with self._lock:
            conn = self._connection()
            try:
                conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                try:
                    yield conn
                    conn.execute("COMMIT")
                except BaseException:
                    with contextlib.suppress(sqlite3.Error):
                        conn.execute("ROLLBACK")
                    raise
            except (
                sqlite3.IntegrityError,
                sqlite3.ProgrammingError,
                sqlite3.InterfaceError,
            ) as exc:
                raise ArchiveError(f"forecast archive {self._path}: {exc}") from exc
            except sqlite3.Error as exc:
                raise self._unavailable(f"{type(exc).__name__}: {exc}", exc) from exc

    # -- writing -----------------------------------------------------------

    @staticmethod
    def _identity_key(series: WeatherSeries) -> str:
        """Hash of the provenance identity of a series (see :meth:`record_series`)."""
        point = series.point
        retrieval_distinguishes = (
            series.kind is DataKind.FORECAST and series.model_init_time_utc is None
        )
        fields = [
            series.provider,
            series.dataset,
            series.kind.value,
            series.model_name,
            series.model_version,
            None if series.model_init_time_utc is None else to_epoch_s(series.model_init_time_utc),
            to_epoch_s(series.retrieved_at_utc) if retrieval_distinguishes else None,
            round(point.latitude_deg, 6),
            round(point.longitude_deg, 6),
            None if point.elevation_m is None else round(point.elevation_m, 1),
        ]
        return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _rows(series: WeatherSeries) -> dict[tuple[str, int], tuple[float | None, str, str]]:
        """Flatten a series to {(variable, valid_epoch): (value, unit, quality)}.

        Non-finite values are stored as NULL (SQLite cannot hold NaN) and flagged
        ``missing``. Two different values for one (variable, instant) inside a
        single series is a data error, not something to resolve silently.
        """
        rows: dict[tuple[str, int], tuple[float | None, str, str]] = {}
        for step in series.steps:
            t = to_epoch_s(step.valid_time_utc)
            for variable, item in step.values.items():
                value = item.value
                quality = item.quality.value
                if value is not None and not math.isfinite(value):
                    value, quality = None, QualityFlag.MISSING.value
                row = (value, item.unit, quality)
                previous = rows.setdefault((variable, t), row)
                if previous != row:
                    raise ArchiveConflictError(
                        f"series from {series.provider}/{series.dataset} has two different "
                        f"values for {variable} at {from_epoch_s(t).isoformat()}"
                    )
        return rows

    def record_series(self, series: WeatherSeries) -> int:
        """Archive a series; return the number of ``value`` rows newly written.

        **Idempotency identity** (``series.identity_key``, UNIQUE) is the
        provenance of the *model run or station*, not of the download:

        * forecast with a known ``model_init_time_utc`` -
          ``(provider, dataset, kind, model_name, model_version, model_init_time_utc,
          grid latitude, longitude, elevation)``. Fetching the same run again an
          hour later is the same forecast, not a second one, so it must not become
          a second row (it would count one forecast several times in every score);
        * forecast with no init time - the same plus ``retrieved_at_utc`` (runs
          cannot be told apart, so each retrieval stands alone);
        * observation and every other kind - ``(provider, dataset, kind,
          model_name, model_version, point)``: one series per station/source that
          grows as new readings arrive.

        A *different* model run (different init time) is a new series with new
        rows; nothing already stored is ever changed. Within a known identity each
        (variable, valid time) cell is written at most once, **with its own
        ``retrieved_at_utc``** - the leakage guard reads the cell's retrieval time,
        so a cell added by a later fetch is judged by when it was really fetched,
        never by the first fetch of the run. Re-recording the same series writes 0
        rows. A cell that already exists with a *different* value, unit or quality
        is kept as it was (immutable), counted, and logged at WARNING; see
        :attr:`last_record_report`.
        """
        rows = self._rows(series)
        self.last_record_report = RecordReport()
        if not rows:
            return 0
        key = self._identity_key(series)
        retrieved = to_epoch_s(series.retrieved_at_utc)
        with self._txn(write=True) as conn:
            found = conn.execute("SELECT id FROM series WHERE identity_key = ?", (key,)).fetchone()
            existing: dict[tuple[str, int], tuple[float | None, str, str]] = {}
            if found is None:
                series_id = self._insert_series(conn, series, key)
            else:
                series_id = int(found[0])
                span: dict[str, list[int]] = {}
                for variable, t in rows:
                    lo_hi = span.setdefault(variable, [t, t])
                    lo_hi[0], lo_hi[1] = min(lo_hi[0], t), max(lo_hi[1], t)
                for variable, (lo, hi) in span.items():
                    for t, val, unit, qual in conn.execute(
                        "SELECT valid_time_utc, value, unit, quality FROM value "
                        "WHERE series_id = ? AND variable = ? AND valid_time_utc BETWEEN ? AND ?",
                        (series_id, variable, lo, hi),
                    ):
                        existing[(variable, t)] = (val, unit, qual)
            new_rows = []
            same = differing = 0
            for (variable, t), cell in rows.items():
                stored = existing.get((variable, t))
                if stored is None:
                    new_rows.append((series_id, variable, t, *cell, retrieved))
                elif stored == cell:
                    same += 1
                else:
                    differing += 1
            conn.executemany(
                "INSERT INTO value (series_id, variable, valid_time_utc, value, unit, quality, "
                "retrieved_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?)",
                new_rows,
            )
        self.last_record_report = RecordReport(
            written=len(new_rows), already_present=same, conflicting=differing
        )
        if differing:
            logger.warning(
                "archive record_series %s/%s init=%s: %d cell(s) already archived with a "
                "different value; the archived values were kept (immutable)",
                series.provider,
                series.model_name,
                None if series.model_init_time_utc is None
                else series.model_init_time_utc.isoformat(),
                differing,
            )
        return len(new_rows)

    @staticmethod
    def _insert_series(conn: sqlite3.Connection, s: WeatherSeries, key: str) -> int:
        req = s.requested_point
        cur = conn.execute(
            "INSERT INTO series (identity_key, provider, dataset, kind, model_name, "
            "model_version, model_init_time_utc, retrieved_at_utc, latitude_deg, longitude_deg, "
            "elevation_m, requested_latitude_deg, requested_longitude_deg, requested_elevation_m, "
            "spatial_resolution_km, temporal_resolution_s, license, source_url, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                s.provider,
                s.dataset,
                s.kind.value,
                s.model_name,
                s.model_version,
                None if s.model_init_time_utc is None else to_epoch_s(s.model_init_time_utc),
                to_epoch_s(s.retrieved_at_utc),
                s.point.latitude_deg,
                s.point.longitude_deg,
                s.point.elevation_m,
                None if req is None else req.latitude_deg,
                None if req is None else req.longitude_deg,
                None if req is None else req.elevation_m,
                s.spatial_resolution_km,
                s.temporal_resolution_s,
                s.license,
                s.source_url,
                to_epoch_s(datetime.now(tz=UTC)),
            ),
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    # -- reading: pairs ----------------------------------------------------

    def pairs(
        self,
        *,
        variable: str,
        period: TimeWindow | None = None,
        provider: str | None = None,
        model_name: str | None = None,
        max_separation_m: float = DEFAULT_MAX_SEPARATION_M,
        max_time_offset_s: float = DEFAULT_MAX_TIME_OFFSET_S,
        lead_time_range_s: tuple[float, float] | None = None,
    ) -> list[ForecastObservationPair]:
        """Forecast/observation pairs for ``variable``, ordered by valid time.

        * One pair per (model run = forecast series, valid time); runs are never
          collapsed and never cross-multiplied, so ``lead_time_s`` is meaningful.
        * The observation is the one inside ``max_time_offset_s`` and
          ``max_separation_m`` with the smallest ``|time offset|``, then the
          smallest separation, then the lowest series id (deterministic).
        * Only forecasts with ``retrieved_at_utc < valid_time_utc`` (strictly) are
          considered - enforced in SQL; the number rejected is in
          :attr:`last_pairs_report` and logged. :meth:`pairs_with_report` returns
          it directly.
        * ``period`` is half-open on valid time; ``lead_time_range_s`` is an
          optional half-open ``[lo, hi)`` lead-time filter (a lead-time bucket).
        """
        found, _ = self.pairs_with_report(
            variable=variable,
            period=period,
            provider=provider,
            model_name=model_name,
            max_separation_m=max_separation_m,
            max_time_offset_s=max_time_offset_s,
            lead_time_range_s=lead_time_range_s,
        )
        return found

    def pairs_with_report(
        self,
        *,
        variable: str,
        period: TimeWindow | None = None,
        provider: str | None = None,
        model_name: str | None = None,
        max_separation_m: float = DEFAULT_MAX_SEPARATION_M,
        max_time_offset_s: float = DEFAULT_MAX_TIME_OFFSET_S,
        lead_time_range_s: tuple[float, float] | None = None,
    ) -> tuple[list[ForecastObservationPair], PairsReport]:
        """As :meth:`pairs`, also returning the :class:`PairsReport`."""
        if max_separation_m < 0 or max_time_offset_s < 0:
            raise ValueError("tolerances must be non-negative")
        t0, t1 = self._bounds(period)
        with self._txn(write=False) as conn:
            matches, report = self._match(
                conn,
                variable=variable,
                t0=t0,
                t1=t1,
                provider=provider,
                model_name=model_name,
                max_separation_m=max_separation_m,
                max_time_offset_s=max_time_offset_s,
                lead_range=lead_time_range_s,
            )
        out = self._build_pairs(variable, matches)
        self.last_pairs_report = report
        if report.dropped_recorded_after_valid:
            logger.warning(
                "archive pairs(%s): rejected %d forecast value(s) recorded at or after their "
                "valid time (not forecasts; never scored)",
                variable,
                report.dropped_recorded_after_valid,
            )
        return out, report

    @staticmethod
    def _bounds(period: TimeWindow | None) -> tuple[int, int]:
        if period is None:
            return _T_MIN, _T_MAX
        return to_epoch_s(period.start_utc), to_epoch_s(period.end_utc)

    # SQL shared by the pair query. The planner is forced onto the (variable,
    # valid_time_utc, series_id) index: this is the indexed range scan the module
    # exists to provide, and INDEXED BY makes a regression fail loudly.
    _FORECAST_SELECT: Final[str] = (
        "SELECT v.series_id, v.valid_time_utc, v.value, s.provider, s.model_name, "
        "s.model_init_time_utc, v.retrieved_at_utc, s.latitude_deg, s.longitude_deg, "
        "s.elevation_m "
    )
    _FORECAST_FROM: Final[str] = (
        "FROM value AS v INDEXED BY idx_value_variable_time "
        "JOIN series AS s ON s.id = v.series_id "
        "WHERE v.variable = :variable AND v.valid_time_utc >= :t0 AND v.valid_time_utc < :t1 "
        "AND s.kind = 'forecast' AND v.value IS NOT NULL "
        f"AND v.quality IN ({_QUALITY_SQL}) "
        "AND (:provider IS NULL OR s.provider = :provider) "
        "AND (:model_name IS NULL OR s.model_name = :model_name) "
    )
    _FORECAST_ELIGIBLE: Final[str] = (
        # THE LEAKAGE GUARD: strictly before the valid time, in SQL.
        "AND v.retrieved_at_utc < v.valid_time_utc "
        "AND s.model_init_time_utc IS NOT NULL "
        "AND v.valid_time_utc >= s.model_init_time_utc "
        "AND (v.valid_time_utc - s.model_init_time_utc) >= :lead_lo "
        "AND (v.valid_time_utc - s.model_init_time_utc) < :lead_hi "
    )
    _OBS_SQL: Final[str] = (
        "SELECT v.valid_time_utc, v.series_id, v.value, s.latitude_deg, s.longitude_deg, "
        "s.elevation_m "
        "FROM value AS v INDEXED BY idx_value_variable_time "
        "JOIN series AS s ON s.id = v.series_id "
        "WHERE v.variable = :variable AND v.valid_time_utc >= :o0 AND v.valid_time_utc < :o1 "
        "AND s.kind = 'observation' AND v.value IS NOT NULL "
        f"AND v.quality IN ({_QUALITY_SQL}) "
        "ORDER BY v.valid_time_utc, v.series_id"
    )
    _DROPPED_SQL: Final[str] = (
        "SELECT COUNT(*), "
        "COALESCE(SUM(v.retrieved_at_utc >= v.valid_time_utc), 0), "
        "COALESCE(SUM(v.retrieved_at_utc < v.valid_time_utc "
        "AND s.model_init_time_utc IS NULL), 0), "
        "COALESCE(SUM(v.retrieved_at_utc < v.valid_time_utc "
        "AND s.model_init_time_utc IS NOT NULL "
        "AND v.valid_time_utc < s.model_init_time_utc), 0) "
    )

    @classmethod
    def _forecast_query(cls) -> str:
        return (
            cls._FORECAST_SELECT
            + cls._FORECAST_FROM
            + cls._FORECAST_ELIGIBLE
            + "ORDER BY v.valid_time_utc, v.series_id"
        )

    @staticmethod
    def _query_params(
        variable: str,
        t0: int,
        t1: int,
        provider: str | None,
        model_name: str | None,
        lead_range: tuple[float, float] | None,
    ) -> dict[str, Any]:
        lo, hi = (0, _T_MAX) if lead_range is None else lead_range
        return {
            "variable": variable,
            "t0": t0,
            "t1": t1,
            "provider": provider,
            "model_name": model_name,
            "lead_lo": lo,
            "lead_hi": hi,
        }

    def explain_pairs(
        self,
        *,
        variable: str,
        period: TimeWindow | None = None,
        provider: str | None = None,
        model_name: str | None = None,
    ) -> list[str]:
        """``EXPLAIN QUERY PLAN`` detail lines for the forecast and observation queries.

        For operators and tests: proves the pair query is an indexed range scan.
        """
        t0, t1 = self._bounds(period)
        params = self._query_params(variable, t0, t1, provider, model_name, None)
        obs_params = {"variable": variable, "o0": t0, "o1": t1}
        lines: list[str] = []
        with self._txn(write=False) as conn:
            for sql, p in ((self._forecast_query(), params), (self._OBS_SQL, obs_params)):
                for row in conn.execute("EXPLAIN QUERY PLAN " + sql, p):
                    lines.append(str(row[3]))
        return lines

    def _match(
        self,
        conn: sqlite3.Connection,
        *,
        variable: str,
        t0: int,
        t1: int,
        provider: str | None,
        model_name: str | None,
        max_separation_m: float,
        max_time_offset_s: float,
        lead_range: tuple[float, float] | None,
    ) -> tuple[list[_Match], PairsReport]:
        params = self._query_params(variable, t0, t1, provider, model_name, lead_range)

        total, late, no_init, neg = conn.execute(
            self._DROPPED_SQL + self._FORECAST_FROM, params
        ).fetchone()

        tol = math.ceil(max_time_offset_s)
        o0 = t0 - tol if t0 > _T_MIN else _T_MIN
        o1 = t1 + tol if t1 < _T_MAX else _T_MAX
        obs_times: list[int] = []
        obs_rows: list[tuple[int, float, tuple[float, float, float | None]]] = []
        for ot, osid, oval, olat, olon, oelev in conn.execute(
            self._OBS_SQL, {"variable": variable, "o0": o0, "o1": o1}
        ):
            obs_times.append(ot)
            obs_rows.append((osid, oval, (olat, olon, oelev)))

        distance_cache: dict[tuple[float, ...], float] = {}
        matches: list[_Match] = []
        eligible = 0
        unmatched = 0
        for fsid, t, fval, prov, mname, init, retrieved, flat, flon, felev in conn.execute(
            self._forecast_query(), params
        ):
            eligible += 1
            lo = bisect_left(obs_times, t - max_time_offset_s)
            hi = bisect_right(obs_times, t + max_time_offset_s)
            best: tuple[tuple[int, float, int], int, float] | None = None
            for i in range(lo, hi):
                osid, _oval, (olat, olon, _oelev) = obs_rows[i]
                dkey = (flat, flon, olat, olon)
                sep = distance_cache.get(dkey)
                if sep is None:
                    sep = geodesic_distance_m(
                        GeoPoint(latitude_deg=flat, longitude_deg=flon),
                        GeoPoint(latitude_deg=olat, longitude_deg=olon),
                    )
                    distance_cache[dkey] = sep
                if sep > max_separation_m:
                    continue
                rank = (abs(obs_times[i] - t), sep, osid)
                if best is None or rank < best[0]:
                    best = (rank, i, sep)
            if best is None:
                unmatched += 1
                continue
            _rank, i, sep = best
            osid, oval, oxy = obs_rows[i]
            matches.append(
                _Match(
                    valid_s=t,
                    forecast_value=fval,
                    observed_value=oval,
                    lead_s=t - init,
                    provider=prov,
                    model_name=mname,
                    retrieved_s=retrieved,
                    forecast_series_id=fsid,
                    observation_series_id=osid,
                    forecast_xy=(flat, flon, felev),
                    observation_xy=oxy,
                    separation_m=sep,
                    offset_s=obs_times[i] - t,
                )
            )
        report = PairsReport(
            variable=variable,
            candidates=int(total),
            dropped_recorded_after_valid=int(late),
            dropped_no_init_time=int(no_init),
            dropped_negative_lead=int(neg),
            eligible=eligible,
            unmatched=unmatched,
            emitted=len(matches),
        )
        return matches, report

    @staticmethod
    def _build_pairs(variable: str, matches: Sequence[_Match]) -> list[ForecastObservationPair]:
        points: dict[tuple[float, float, float | None], GeoPoint] = {}

        def point(xy: tuple[float, float, float | None]) -> GeoPoint:
            cached = points.get(xy)
            if cached is None:
                cached = points[xy] = GeoPoint(
                    latitude_deg=xy[0], longitude_deg=xy[1], elevation_m=xy[2]
                )
            return cached

        times: dict[int, datetime] = {}

        def when(seconds: int) -> datetime:
            cached = times.get(seconds)
            if cached is None:
                cached = times[seconds] = from_epoch_s(seconds)
            return cached

        return [
            ForecastObservationPair(
                variable=variable,
                valid_time_utc=when(m.valid_s),
                forecast_value=m.forecast_value,
                observed_value=m.observed_value,
                lead_time_s=float(m.lead_s),
                provider=m.provider,
                model_name=m.model_name,
                forecast_point=point(m.forecast_xy),
                observation_point=point(m.observation_xy),
                separation_m=m.separation_m,
                time_offset_s=float(m.offset_s),
                forecast_recorded_at_utc=when(m.retrieved_s),
            )
            for m in matches
        ]

    # -- reading: coverage -------------------------------------------------

    def coverage(self, *, include_pair_counts: bool = True) -> dict[str, object]:
        """What the archive holds, in a form an operator can act on (JSON-friendly).

        Keys: ``path``, ``schema_version``, ``file_size_bytes`` (database + WAL),
        ``series_count``, ``value_count``, ``forecast_value_count``,
        ``observation_value_count``, ``earliest_valid_time_utc`` and
        ``latest_valid_time_utc`` (ISO strings, ``None`` only when empty),
        ``variables`` (sorted names), ``providers`` and ``by_variable`` (lists of
        dicts with counts), ``daily_aggregate_rows`` and ``pairs`` (pair totals at
        default tolerances plus what the leakage guard rejected; ``None`` if
        ``include_pair_counts=False``). Empty archive: zeros and empty lists.

        Cost is linear in the number of rows (seconds at tens of millions); it is
        an operator query, not a per-request one.
        """
        with self._txn(write=False) as conn:
            series_count = int(conn.execute("SELECT COUNT(*) FROM series").fetchone()[0])
            agg_rows = int(conn.execute("SELECT COUNT(*) FROM daily_aggregate").fetchone()[0])
            per_var: dict[str, dict[str, Any]] = {}
            for var, kind, n, lo, hi in conn.execute(
                "SELECT v.variable, s.kind, COUNT(*), MIN(v.valid_time_utc), "
                "MAX(v.valid_time_utc) FROM value AS v JOIN series AS s ON s.id = v.series_id "
                "GROUP BY v.variable, s.kind"
            ):
                d = per_var.setdefault(
                    var,
                    {"variable": var, "value_count": 0, "forecast_value_count": 0,
                     "observation_value_count": 0, "_lo": None, "_hi": None},
                )
                d["value_count"] += n
                if kind == DataKind.FORECAST.value:
                    d["forecast_value_count"] += n
                elif kind == DataKind.OBSERVATION.value:
                    d["observation_value_count"] += n
                d["_lo"] = lo if d["_lo"] is None else min(d["_lo"], lo)
                d["_hi"] = hi if d["_hi"] is None else max(d["_hi"], hi)
            series_by_provider = dict(
                conn.execute("SELECT provider, COUNT(*) FROM series GROUP BY provider").fetchall()
            )
            values_by_provider = dict(
                conn.execute(
                    "SELECT s.provider, COUNT(*) FROM value AS v "
                    "JOIN series AS s ON s.id = v.series_id GROUP BY s.provider"
                ).fetchall()
            )
            pair_info: dict[str, object] | None = None
            if include_pair_counts:
                pair_info = self._pair_totals(conn, per_var)

        by_variable: list[dict[str, Any]] = []
        for var in sorted(per_var):
            d = per_var[var]
            lo, hi = d.pop("_lo"), d.pop("_hi")
            d["earliest_valid_time_utc"] = _iso(lo)
            d["latest_valid_time_utc"] = _iso(hi)
            if pair_info is not None:
                d["pair_count"] = pair_info["_per_variable"][var]["emitted"]  # type: ignore[index]
            by_variable.append(d)
        if pair_info is not None:
            pair_info.pop("_per_variable")
        los = [v["earliest_valid_time_utc"] for v in by_variable]
        his = [v["latest_valid_time_utc"] for v in by_variable]
        return {
            "path": str(self._path),
            "schema_version": SCHEMA_VERSION,
            "file_size_bytes": self._file_size(),
            "series_count": series_count,
            "value_count": sum(int(v["value_count"]) for v in by_variable),
            "forecast_value_count": sum(int(v["forecast_value_count"]) for v in by_variable),
            "observation_value_count": sum(int(v["observation_value_count"]) for v in by_variable),
            "earliest_valid_time_utc": min(los) if los else None,
            "latest_valid_time_utc": max(his) if his else None,
            "variables": [v["variable"] for v in by_variable],
            "providers": [
                {
                    "provider": p,
                    "series_count": int(series_by_provider[p]),
                    "value_count": int(values_by_provider.get(p, 0)),
                }
                for p in sorted(series_by_provider)
            ],
            "by_variable": by_variable,
            "daily_aggregate_rows": agg_rows,
            "pairs": pair_info,
        }

    def _pair_totals(
        self, conn: sqlite3.Connection, per_var: dict[str, dict[str, Any]]
    ) -> dict[str, object]:
        totals = {
            "pair_count": 0,
            "forecast_values_considered": 0,
            "dropped_recorded_after_valid": 0,
            "dropped_no_init_time": 0,
            "dropped_negative_lead": 0,
            "unmatched_forecasts": 0,
        }
        per: dict[str, dict[str, int]] = {}
        for var in per_var:
            _m, rep = self._match(
                conn,
                variable=var,
                t0=_T_MIN,
                t1=_T_MAX,
                provider=None,
                model_name=None,
                max_separation_m=DEFAULT_MAX_SEPARATION_M,
                max_time_offset_s=DEFAULT_MAX_TIME_OFFSET_S,
                lead_range=None,
            )
            per[var] = {"emitted": rep.emitted}
            totals["pair_count"] += rep.emitted
            totals["forecast_values_considered"] += rep.candidates
            totals["dropped_recorded_after_valid"] += rep.dropped_recorded_after_valid
            totals["dropped_no_init_time"] += rep.dropped_no_init_time
            totals["dropped_negative_lead"] += rep.dropped_negative_lead
            totals["unmatched_forecasts"] += rep.unmatched
        return {
            **totals,
            "max_separation_m": DEFAULT_MAX_SEPARATION_M,
            "max_time_offset_s": DEFAULT_MAX_TIME_OFFSET_S,
            "_per_variable": per,
        }

    def _file_size(self) -> int:
        size = 0
        for suffix in ("", "-wal"):
            try:
                size += os.stat(str(self._path) + suffix).st_size
            except OSError:
                continue
        return size

    # -- retention ---------------------------------------------------------

    def prune(
        self,
        *,
        older_than_utc: datetime,
        dry_run: bool = False,
        only_above_bytes: int | None = None,
    ) -> int:
        """Apply the ADR 0007 retention rule; return the raw ``value`` rows deleted.

        Raw rows whose valid time is before ``older_than_utc`` are folded into
        ``daily_aggregate`` (pair count and the sums of error, |error| and
        error^2 per UTC day, variable, provider, model and lead-time bucket, at
        default tolerances) and then deleted, in one transaction. Observation rows
        are kept for an extra ``DEFAULT_MAX_TIME_OFFSET_S`` so forecasts valid just
        after the cut-off can still be matched later. Series left without values
        are removed. ``dry_run=True`` changes nothing and returns the count that
        would be deleted. ``only_above_bytes`` makes the call a no-op (returns 0)
        while the archive file is smaller than that - the ADR keeps raw rows
        "while the archive stays under a configured size".
        Every non-dry deletion is logged at INFO.
        """
        cutoff = to_epoch_s(older_than_utc)
        obs_cutoff = cutoff - math.ceil(DEFAULT_MAX_TIME_OFFSET_S)
        if only_above_bytes is not None and self._file_size() < only_above_bytes:
            logger.info("archive prune skipped: %s below %d bytes", self._path, only_above_bytes)
            return 0
        count_sql = (
            "SELECT s.kind = 'observation', COUNT(*) FROM value AS v "
            "JOIN series AS s ON s.id = v.series_id "
            "WHERE v.valid_time_utc < CASE WHEN s.kind = 'observation' THEN :o ELSE :c END "
            "GROUP BY s.kind = 'observation'"
        )
        with self._txn(write=not dry_run) as conn:
            counts = {
                int(k): int(n)
                for k, n in conn.execute(count_sql, {"o": obs_cutoff, "c": cutoff})
            }
            n_obs, n_other = counts.get(1, 0), counts.get(0, 0)
            total = n_obs + n_other
            if dry_run:
                logger.info(
                    "archive prune (dry run) %s: would delete %d value rows (%d forecast/other, "
                    "%d observation) older than %s",
                    self._path, total, n_other, n_obs, from_epoch_s(cutoff).isoformat(),
                )
                return total
            if total == 0:
                return 0
            variables = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT variable FROM value WHERE valid_time_utc < ?", (cutoff,)
                )
            ]
            folded = self._fold_into_aggregates(conn, variables, cutoff)
            for var in variables:
                conn.execute(
                    "DELETE FROM value WHERE variable = ? AND valid_time_utc < "
                    "CASE WHEN series_id IN (SELECT id FROM series WHERE kind = 'observation') "
                    "THEN ? ELSE ? END",
                    (var, obs_cutoff, cutoff),
                )
            removed_series = conn.execute(
                "DELETE FROM series WHERE NOT EXISTS "
                "(SELECT 1 FROM value WHERE value.series_id = series.id)"
            ).rowcount
        logger.info(
            "archive prune %s: deleted %d value rows (%d forecast/other, %d observation) and %d "
            "empty series older than %s; folded %d forecast/observation pairs into daily "
            "aggregates first",
            self._path, total, n_other, n_obs, removed_series,
            from_epoch_s(cutoff).isoformat(), folded,
        )
        return total

    def _fold_into_aggregates(
        self, conn: sqlite3.Connection, variables: Sequence[str], cutoff: int
    ) -> int:
        acc: dict[tuple[int, str, str, str, int], list[float]] = defaultdict(
            lambda: [0.0, 0.0, 0.0, 0.0]
        )
        folded = 0
        for var in variables:
            matches, _ = self._match(
                conn,
                variable=var,
                t0=_T_MIN,
                t1=cutoff,
                provider=None,
                model_name=None,
                max_separation_m=DEFAULT_MAX_SEPARATION_M,
                max_time_offset_s=DEFAULT_MAX_TIME_OFFSET_S,
                lead_range=None,
            )
            for m in matches:
                lower, _upper = _lead_bucket(m.lead_s)
                err = m.forecast_value - m.observed_value
                key = (m.valid_s - m.valid_s % _DAY_S, var, m.provider, m.model_name or "", lower)
                cell = acc[key]
                cell[0] += 1
                cell[1] += err
                cell[2] += abs(err)
                cell[3] += err * err
                folded += 1
        for (day, var, prov, mname, lower), (n, s1, sa, s2) in acc.items():
            upper = dict(LEAD_BUCKETS_S)[lower]
            conn.execute(
                "INSERT INTO daily_aggregate (day_utc, variable, provider, model_name, "
                "lead_lower_s, lead_upper_s, pair_count, sum_error, sum_abs_error, sum_sq_error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (day_utc, variable, provider, model_name, lead_lower_s) DO UPDATE SET "
                "pair_count = pair_count + excluded.pair_count, "
                "sum_error = sum_error + excluded.sum_error, "
                "sum_abs_error = sum_abs_error + excluded.sum_abs_error, "
                "sum_sq_error = sum_sq_error + excluded.sum_sq_error",
                (day, var, prov, mname, lower, upper, int(n), s1, sa, s2),
            )
        return folded

    def daily_aggregates(self, *, variable: str | None = None) -> list[dict[str, Any]]:
        """Rows of ``daily_aggregate`` (what retention preserved), oldest first."""
        sql = (
            "SELECT day_utc, variable, provider, model_name, lead_lower_s, lead_upper_s, "
            "pair_count, sum_error, sum_abs_error, sum_sq_error FROM daily_aggregate "
            "WHERE (:variable IS NULL OR variable = :variable) "
            "ORDER BY day_utc, variable, provider, model_name, lead_lower_s"
        )
        with self._txn(write=False) as conn:
            rows = conn.execute(sql, {"variable": variable}).fetchall()
        return [
            {
                "day_utc": from_epoch_s(r[0]),
                "variable": r[1],
                "provider": r[2],
                "model_name": r[3] or None,
                "lead_bucket_s": (r[4], r[5]),
                "pair_count": r[6],
                "sum_error": r[7],
                "sum_abs_error": r[8],
                "sum_sq_error": r[9],
            }
            for r in rows
        ]


def _lead_bucket(lead_s: int) -> tuple[int, int | None]:
    for lower, upper in LEAD_BUCKETS_S:
        if lead_s >= lower and (upper is None or lead_s < upper):
            return lower, upper
    return LEAD_BUCKETS_S[0]  # unreachable: matches have lead >= 0


def open_archive(settings: object | None = None) -> SqliteArchive:
    """Build the archive configured in ``settings`` (``archive_path``).

    Lazy: no file is touched until first use. The parent directory is created on
    first use (``create_dirs=True``) so a fresh checkout works; a path that
    cannot be created or opened surfaces as :class:`ArchiveUnavailableError` at
    that point, never here.
    """
    if settings is None:
        from rwi.config import get_settings

        settings = get_settings()
    path = getattr(settings, "archive_path", None)
    if path is None:
        path = os.environ.get("RWI_ARCHIVE_PATH", "data/archive/rwi.sqlite3")
    return SqliteArchive(path, create_dirs=True)
