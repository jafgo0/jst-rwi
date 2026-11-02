"""A broken archive degrades to a typed error naming the path; never a raw sqlite error."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from test_archive_support import forecast_series, observation_series

from rwi.archive import (
    ArchiveError,
    ArchiveReadOnlyError,
    ArchiveUnavailableError,
    SqliteArchive,
    open_archive,
)
from rwi.config import Settings
from rwi.core.errors import RwiError

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def test_error_hierarchy() -> None:
    assert issubclass(ArchiveUnavailableError, ArchiveError)
    assert issubclass(ArchiveError, RwiError)


def test_construction_is_lazy_and_touches_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "rwi.sqlite3"
    archive = SqliteArchive(missing)  # must not raise, must not create anything
    assert not missing.parent.exists()
    archive.close()


def test_corrupt_file_raises_typed_error_naming_the_path(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"this is definitely not a sqlite database" * 100)
    archive = SqliteArchive(path)
    with pytest.raises(ArchiveUnavailableError) as info:
        archive.coverage()
    assert str(path) in str(info.value)
    with pytest.raises(ArchiveUnavailableError, match=r"corrupt\.sqlite3"):
        archive.record_series(forecast_series())
    with pytest.raises(ArchiveUnavailableError, match=r"corrupt\.sqlite3"):
        archive.pairs(variable="wind_speed_10m")


def test_missing_directory_raises_typed_error_naming_the_path(tmp_path: Path) -> None:
    path = tmp_path / "does-not-exist" / "rwi.sqlite3"
    archive = SqliteArchive(path)
    with pytest.raises(ArchiveUnavailableError) as info:
        archive.record_series(forecast_series())
    assert str(path) in str(info.value)
    assert not path.parent.exists()  # never silently created by the class itself


def test_open_archive_creates_its_directory_on_first_use(tmp_path: Path) -> None:
    path = tmp_path / "data" / "archive" / "rwi.sqlite3"
    archive = open_archive(Settings(archive_path=path))
    assert not path.parent.exists()  # lazy
    try:
        assert archive.coverage()["value_count"] == 0
        assert path.is_file()
    finally:
        archive.close()


@pytest.mark.skipif(IS_ROOT, reason="root bypasses file permissions")
def test_unwritable_archive_raises_typed_error_naming_the_path(tmp_path: Path) -> None:
    folder = tmp_path / "locked"
    folder.mkdir()
    path = folder / "rwi.sqlite3"
    with SqliteArchive(path) as archive:
        archive.record_series(forecast_series())
    path.chmod(stat.S_IRUSR)
    folder.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        archive = SqliteArchive(path)
        with pytest.raises(ArchiveUnavailableError) as info:
            archive.record_series(observation_series())
        assert str(path) in str(info.value)
        archive.close()
    finally:
        folder.chmod(stat.S_IRWXU)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_read_only_mode_reads_but_refuses_writes(tmp_path: Path) -> None:
    path = tmp_path / "rwi.sqlite3"
    with SqliteArchive(path) as writer:
        writer.record_series(forecast_series())
    with SqliteArchive(path, read_only=True) as reader:
        assert reader.coverage()["value_count"] == 1
        with pytest.raises(ArchiveReadOnlyError, match="read-only"):
            reader.record_series(observation_series())
        with pytest.raises(ArchiveReadOnlyError):
            reader.prune(older_than_utc=forecast_series().retrieved_at_utc)


def test_read_only_on_missing_file_is_unavailable_not_created(tmp_path: Path) -> None:
    path = tmp_path / "absent.sqlite3"
    with pytest.raises(ArchiveUnavailableError, match=r"absent\.sqlite3"):
        SqliteArchive(path, read_only=True).coverage()
    assert not path.exists()


def test_archive_from_a_newer_schema_is_refused(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "future.sqlite3"
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 999")
    raw.execute("CREATE TABLE x (a)")
    raw.commit()
    raw.close()
    with pytest.raises(ArchiveUnavailableError, match="newer"):
        SqliteArchive(path).coverage()


def test_close_is_idempotent_and_reopens_lazily(tmp_path: Path) -> None:
    archive = SqliteArchive(tmp_path / "rwi.sqlite3")
    archive.record_series(forecast_series())
    archive.close()
    archive.close()
    assert archive.coverage()["value_count"] == 1
    archive.close()
