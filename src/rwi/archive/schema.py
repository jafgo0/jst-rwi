"""SQLite schema and forward-only migrations for the forecast archive (ADR 0007).

Storage conventions
-------------------
* **All instants are INTEGER epoch seconds, UTC** (``valid_time_utc``,
  ``retrieved_at_utc``, ``model_init_time_utc``, ``created_at_utc``, ...), floored
  to whole seconds. Integers make range scans and comparisons exact and fast, and
  sort identically to time. Conversion happens only at the boundary in
  :mod:`rwi.archive.store`, so callers always see aware UTC ``datetime`` objects.
  Flooring is conservative for the leakage rule: it can turn "retrieved 0.4 s
  before" into "retrieved in the same second" (rejected), never the reverse.
* Latitude/longitude are REAL degrees (WGS84), elevation REAL metres or NULL
  (unknown, which is not 0 m). Resolutions are REAL km / seconds.
* Values are REAL in the canonical unit of the variable
  (:mod:`rwi.core.variables`); ``unit`` is stored with every value anyway so a row
  is self-describing.

Layout
------
``series``  one row per *retrieved series* - the provenance stored once.
``value``   one row per (series, variable, valid time) - the numbers.
``daily_aggregate``  what retention keeps after raw rows are pruned.

Migrations are forward-only: ``MIGRATIONS[n]`` upgrades user_version ``n-1`` to
``n``. Never edit an existing entry; append a new one and record it in
``docs/science/archive.md``.
"""

from __future__ import annotations

from typing import Final

SCHEMA_VERSION: Final[int] = 1
"""Highest schema version this code understands (``PRAGMA user_version``)."""

LEAD_BUCKETS_S: Final[tuple[tuple[int, int | None], ...]] = (
    (0, 3 * 3600),
    (3 * 3600, 6 * 3600),
    (6 * 3600, 12 * 3600),
    (12 * 3600, 24 * 3600),
    (24 * 3600, 48 * 3600),
    (48 * 3600, None),
)
"""Half-open lead-time buckets [lower, upper) in seconds used by retention
aggregates. ``None`` upper means unbounded. Changing these is a data migration."""

USABLE_QUALITY: Final[tuple[str, ...]] = ("ok", "interpolated", "unvalidated")
"""Quality flags (``rwi.core.provenance.USABLE_FLAGS``) whose values may be scored."""

_V1: Final[tuple[str, ...]] = (
    """
    CREATE TABLE series (
        id                      INTEGER PRIMARY KEY,
        identity_key            TEXT    NOT NULL UNIQUE,
        provider                TEXT    NOT NULL,
        dataset                 TEXT    NOT NULL,
        kind                    TEXT    NOT NULL,
        model_name              TEXT,
        model_version           TEXT,
        model_init_time_utc     INTEGER,
        retrieved_at_utc        INTEGER NOT NULL,
        latitude_deg            REAL    NOT NULL,
        longitude_deg           REAL    NOT NULL,
        elevation_m             REAL,
        requested_latitude_deg  REAL,
        requested_longitude_deg REAL,
        requested_elevation_m   REAL,
        spatial_resolution_km   REAL,
        temporal_resolution_s   REAL,
        license                 TEXT,
        source_url              TEXT,
        created_at_utc          INTEGER NOT NULL
    )
    """,
    # Selects the forecast/observation side and the model without touching value.
    "CREATE INDEX idx_series_kind_model ON series "
    "(kind, provider, model_name, model_init_time_utc)",
    """
    CREATE TABLE value (
        series_id      INTEGER NOT NULL REFERENCES series(id),
        variable       TEXT    NOT NULL,
        valid_time_utc INTEGER NOT NULL,
        value          REAL,
        unit           TEXT    NOT NULL,
        quality        TEXT    NOT NULL,
        PRIMARY KEY (series_id, variable, valid_time_utc)
    ) WITHOUT ROWID
    """,
    # THE pair-query index: variable equality, then a valid-time range, then the
    # series to join. Serves both the forecast side and the observation side.
    "CREATE INDEX idx_value_variable_time ON value (variable, valid_time_utc, series_id)",
    # Immutability enforced by the database itself, not only by convention.
    """
    CREATE TRIGGER value_is_immutable BEFORE UPDATE ON value
    BEGIN
        SELECT RAISE(ABORT, 'archive values are immutable: record a new series instead');
    END
    """,
    """
    CREATE TRIGGER series_is_immutable BEFORE UPDATE ON series
    BEGIN
        SELECT RAISE(ABORT, 'archive series are immutable: record a new series instead');
    END
    """,
    # Retention output: sufficient statistics of (forecast - observation) so MAE,
    # RMSE and bias per lead bucket survive after the raw rows are deleted.
    """
    CREATE TABLE daily_aggregate (
        day_utc        INTEGER NOT NULL,
        variable       TEXT    NOT NULL,
        provider       TEXT    NOT NULL,
        model_name     TEXT    NOT NULL,
        lead_lower_s   INTEGER NOT NULL,
        lead_upper_s   INTEGER,
        pair_count     INTEGER NOT NULL,
        sum_error      REAL    NOT NULL,
        sum_abs_error  REAL    NOT NULL,
        sum_sq_error   REAL    NOT NULL,
        PRIMARY KEY (day_utc, variable, provider, model_name, lead_lower_s)
    ) WITHOUT ROWID
    """,
)

MIGRATIONS: Final[dict[int, tuple[str, ...]]] = {1: _V1}
"""Forward-only migrations keyed by the version they produce."""
