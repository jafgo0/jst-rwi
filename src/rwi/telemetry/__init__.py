"""Vehicle telemetry readers and replay.

The team's drive-system repo already produces live telemetry; this package is
the ingest side only. Phase 3 ships two readers:

* :class:`rwi.telemetry.csv_replay.CsvReplayReader` reads the team's own
  race-day CSV exports (one file per CAN frame type, ``_id,...,timestamp``
  with a Mongo extended-JSON date). This is the first reader because it needs
  no running database and lets every one of this platform's models - PV,
  consumption, battery, speed - be checked against the car that actually
  raced in 2025.

* A live MongoDB adapter is deliberately not included yet. The CSV path
  demonstrates the TelemetryReader protocol works end to end against real
  data, and anything that reads MongoDB adds a dependency that is harder to
  test.

Nothing here advances the active position by itself. A separate coordinator
reads a stream, applies freshness and quality rules, and feeds StateStore;
that way a bug in a reader cannot silently drive the car on screen.
"""

from rwi.telemetry.csv_replay import CsvReplayReader, ReplayConfig, ReplayError
from rwi.telemetry.frames import (
    KNOWN_FRAMES,
    UNMAPPED,
    ColumnMap,
    FrameSpec,
    SignalKind,
)

__all__ = [
    "KNOWN_FRAMES",
    "UNMAPPED",
    "ColumnMap",
    "CsvReplayReader",
    "FrameSpec",
    "ReplayConfig",
    "ReplayError",
    "SignalKind",
]
