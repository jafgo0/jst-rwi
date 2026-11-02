"""Typed errors for the forecast archive.

The archive is optional infrastructure: a broken archive must degrade to
"verification unavailable" and must never break a weather request. Every failure
that can come out of this package is therefore an :class:`ArchiveError`, so a
caller can guard the whole archive with one ``except ArchiveError``. Raw
``sqlite3`` exceptions never escape.
"""

from __future__ import annotations

from rwi.core.errors import RwiError


class ArchiveError(RwiError):
    """Base class for every archive failure."""


class ArchiveUnavailableError(ArchiveError):
    """The archive file cannot be opened or used.

    Missing directory, corrupt file, unreadable or read-only location, a schema
    written by a newer version of the software, or a database that is locked for
    longer than the busy timeout. The message always names the path.
    """


class ArchiveReadOnlyError(ArchiveError):
    """A write was attempted on an archive opened with ``read_only=True``."""


class ArchiveConflictError(ArchiveError):
    """A series with an already-recorded identity carries different values.

    Recorded forecasts are immutable, so the archive refuses to overwrite and
    refuses to silently ignore: the caller learns that the same retrieval was
    reported with two different contents.
    """
