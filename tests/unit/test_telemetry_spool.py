"""Sensor spool: idempotent store, per-sink forwarding state, backfill order (ADR 0010)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from rwi.contracts import GeoPoint
from rwi.telemetry.onboard import SensorWindow
from rwi.telemetry.spool import SensorSpool, window_key

T0 = datetime(2027, 8, 22, 3, 15, tzinfo=UTC)


def _w(stream: str, s: int, value: float = 1.0) -> SensorWindow:
    return SensorWindow(
        stream=stream,
        start_utc=T0 + timedelta(seconds=s),
        values={"x": value},
        position=GeoPoint(latitude_deg=-16.25, longitude_deg=133.37),
        sample_count=1,
    )


def test_put_is_idempotent_and_round_trips(tmp_path: Path) -> None:
    spool = SensorSpool(tmp_path / "sub" / "spool.sqlite")
    assert spool.put([_w("a", 0), _w("a", 1), _w("b", 0)]) == 3
    assert spool.put([_w("a", 0, value=99.0), _w("a", 2)]) == 1  # first write wins
    assert spool.count() == 4 and spool.count("a") == 3
    got = spool.get("a", T0)
    assert got == _w("a", 0)
    assert spool.get("a", T0 + timedelta(seconds=9)) is None
    assert [w.start_utc for w in spool.windows("a", T0, T0 + timedelta(seconds=2))] == [
        T0,
        T0 + timedelta(seconds=1),
    ]
    assert window_key(got) == ("a", int(T0.timestamp()))
    spool.close()


def test_pending_and_forwarding_are_per_sink(tmp_path: Path) -> None:
    spool = SensorSpool(tmp_path / "spool.sqlite")
    spool.put([_w("b", 1), _w("a", 1), _w("a", 0)])
    assert [window_key(w) for w in spool.pending("archive")] == [
        ("a", int(T0.timestamp())),
        ("a", int(T0.timestamp()) + 1),
        ("b", int(T0.timestamp()) + 1),
    ]
    assert len(spool.pending("archive", stream="a", limit=1)) == 1
    assert spool.mark_forwarded("archive", [_w("a", 0), _w("a", 1)], outcome="archived") == 2
    assert spool.mark_forwarded("archive", [_w("a", 0)], outcome="again") == 0
    assert spool.mark_forwarded("archive", [], outcome="x") == 0
    assert [w.stream for w in spool.pending("archive")] == ["b"]
    assert len(spool.pending("live")) == 3
    assert spool.outcomes("archive") == {"archived": 2}
    assert spool.forwarded_between("archive", "a", T0, T0 + timedelta(seconds=1)) == 1


def test_state_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "spool.sqlite"
    spool = SensorSpool(path)
    spool.put([_w("a", 0), _w("a", 5)])
    spool.mark_forwarded("archive", [_w("a", 0)], outcome="archived")
    spool.close()
    again = SensorSpool(path)
    assert [w.start_utc for w in again.pending("archive")] == [T0 + timedelta(seconds=5)]
    assert again.newest_end("a") == T0 + timedelta(seconds=6)
    assert again.newest_end("missing") is None
    again.close()
