"""Durable SQLite archive of forecasts and observations (ADR 0007)."""

from rwi.archive.errors import (
    ArchiveConflictError,
    ArchiveError,
    ArchiveReadOnlyError,
    ArchiveUnavailableError,
)
from rwi.archive.schema import LEAD_BUCKETS_S, SCHEMA_VERSION
from rwi.archive.store import (
    DEFAULT_MAX_SEPARATION_M,
    DEFAULT_MAX_TIME_OFFSET_S,
    PairsReport,
    SqliteArchive,
    from_epoch_s,
    open_archive,
    to_epoch_s,
)

__all__ = [
    "DEFAULT_MAX_SEPARATION_M",
    "DEFAULT_MAX_TIME_OFFSET_S",
    "LEAD_BUCKETS_S",
    "SCHEMA_VERSION",
    "ArchiveConflictError",
    "ArchiveError",
    "ArchiveReadOnlyError",
    "ArchiveUnavailableError",
    "PairsReport",
    "SqliteArchive",
    "from_epoch_s",
    "open_archive",
    "to_epoch_s",
]
